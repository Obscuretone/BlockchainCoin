"""Durable UTXO node orchestration.

``ConsensusNode`` composes consensus validation, fork-aware storage, mempool
policy, UTXO indexing, and invalid-block caching into the local full-node API
used by the CLI, runtime, and peer service. It is deliberately narrower than a
network daemon: networking decides when data arrives, while this module decides
how local state changes when transactions or blocks are accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .consensus import (
    UTXO,
    BlockProcessor,
    ConsensusBlock,
    ConsensusState,
    ConsensusTransaction,
    OutPoint,
    TxInput,
    TxOutput,
)
from .constants import MAX_MONEY
from .crypto import Wallet, is_valid_address
from .fork_choice import ForkChoice
from .storage import (
    ChainParameters,
    SQLiteForkStore,
    SQLiteInvalidBlockCache,
    SQLiteMempoolStore,
    SQLiteUTXOIndex,
    StorageError,
    StoredConsensusBlock,
)


class ConsensusNodeError(RuntimeError):
    """Raised when local node state or operator input is invalid."""

    pass


class MempoolPolicyError(ConsensusNodeError):
    """Raised when a transaction is consensus-valid but rejected by local policy."""


@dataclass(frozen=True)
class ReorgEvent:
    """Description of an active-chain tip change that is not a simple append."""

    old_tip: str
    new_tip: str
    old_height: int
    new_height: int


@dataclass(frozen=True)
class MempoolPolicy:
    """Local transaction relay and block-assembly policy.

    These limits are intentionally not consensus rules. A transaction rejected
    here may still be valid inside a block produced elsewhere, but this node will
    not keep or relay it from the mempool unless it meets these local thresholds.
    """

    max_transactions: int = 5_000
    max_transaction_bytes: int = 100_000
    max_inputs: int = 128
    max_outputs: int = 128
    min_relay_fee: int = 0
    min_relay_fee_rate: int = 0
    dynamic_fee_rate_step: int = 0
    allow_replacement: bool = True
    replacement_fee_bump: int = 1
    allow_eviction: bool = True
    eviction_fee_rate_bump: int = 1

    def __post_init__(self) -> None:
        if self.max_transactions <= 0:
            raise MempoolPolicyError("max mempool transactions must be positive")
        if self.max_transaction_bytes <= 0:
            raise MempoolPolicyError("max transaction bytes must be positive")
        if self.max_inputs <= 0:
            raise MempoolPolicyError("max transaction inputs must be positive")
        if self.max_outputs <= 0:
            raise MempoolPolicyError("max transaction outputs must be positive")
        if self.min_relay_fee < 0:
            raise MempoolPolicyError("minimum relay fee cannot be negative")
        if self.min_relay_fee_rate < 0:
            raise MempoolPolicyError("minimum relay fee rate cannot be negative")
        if self.dynamic_fee_rate_step < 0:
            raise MempoolPolicyError("dynamic fee rate step cannot be negative")
        if self.replacement_fee_bump < 0:
            raise MempoolPolicyError("replacement fee bump cannot be negative")
        if self.eviction_fee_rate_bump < 0:
            raise MempoolPolicyError("eviction fee rate bump cannot be negative")

    def validate(
        self,
        transaction: ConsensusTransaction,
        *,
        fee: int,
        current_count: int,
    ) -> None:
        """Reject transactions that exceed local mempool admission limits."""

        if current_count >= self.max_transactions:
            raise MempoolPolicyError("mempool transaction limit reached")
        if len(transaction.inputs) > self.max_inputs:
            raise MempoolPolicyError("transaction has too many inputs for mempool policy")
        if len(transaction.outputs) > self.max_outputs:
            raise MempoolPolicyError("transaction has too many outputs for mempool policy")
        size = self.serialized_size(transaction)
        if size > self.max_transaction_bytes:
            raise MempoolPolicyError("transaction exceeds mempool size policy")
        if fee < self.minimum_fee(size, current_count=current_count):
            raise MempoolPolicyError("transaction fee is below mempool relay minimum")

    @staticmethod
    def serialized_size(transaction: ConsensusTransaction) -> int:
        return len(transaction.to_bytes())

    def effective_min_relay_fee_rate(self, current_count: int) -> int:
        """Return the occupancy-adjusted minimum fee rate in coins per kB."""

        if current_count < 0:
            raise MempoolPolicyError("mempool transaction count cannot be negative")
        return self.min_relay_fee_rate + (current_count * self.dynamic_fee_rate_step)

    def minimum_fee(self, transaction_bytes: int, *, current_count: int = 0) -> int:
        if transaction_bytes < 0:
            raise MempoolPolicyError("transaction byte size cannot be negative")
        fee_rate = self.effective_min_relay_fee_rate(current_count)
        fee_rate_minimum = (transaction_bytes * fee_rate + 999) // 1000
        return max(self.min_relay_fee, fee_rate_minimum)

    def fee_rate(self, fee: int, transaction_bytes: int) -> int:
        if fee < 0:
            raise MempoolPolicyError("transaction fee cannot be negative")
        if transaction_bytes <= 0:
            raise MempoolPolicyError("transaction byte size must be positive")
        return (fee * 1000) // transaction_bytes


class ConsensusNode:
    """UTXO-native local node backed by durable fork-aware storage.

    The node owns the active ``ConsensusState`` and persists enough auxiliary
    data to reopen safely: chain parameters, known blocks, mempool entries,
    invalid-block decisions, and UTXO snapshots. Reopening a node verifies the
    stored chain parameters and replays known branches before trusting cached
    index state.
    """

    def __init__(
        self,
        store: SQLiteForkStore,
        state: ConsensusState,
        difficulty: int = 3,
        mempool: list[ConsensusTransaction] | None = None,
        fork_choice: ForkChoice | None = None,
        mempool_store: SQLiteMempoolStore | None = None,
        utxo_index: SQLiteUTXOIndex | None = None,
        invalid_blocks: SQLiteInvalidBlockCache | None = None,
        mempool_policy: MempoolPolicy | None = None,
    ) -> None:
        if difficulty < 0:
            raise ConsensusNodeError("difficulty cannot be negative")
        self.store = store
        self.state = state
        self.difficulty = difficulty
        self.mempool_store = mempool_store
        self.invalid_blocks = invalid_blocks
        self.mempool_policy = mempool_policy or MempoolPolicy()
        self.last_reorg: ReorgEvent | None = None
        self.mempool = mempool if mempool is not None else self._load_mempool()
        self.fork_choice = fork_choice or ForkChoice(
            store,
            block_subsidy=state.block_subsidy,
            max_money=state.max_money,
        )
        self.utxo_index = utxo_index

    @classmethod
    def create(
        cls,
        db_path: str | Path,
        genesis_allocations: dict[str, int],
        difficulty: int = 3,
        block_subsidy: int = 50,
        max_money: int = MAX_MONEY,
    ) -> ConsensusNode:
        """Create a new node database with a validated genesis allocation."""

        store = SQLiteForkStore(db_path)
        mempool_store = SQLiteMempoolStore(cls._mempool_path(db_path))
        utxo_index = SQLiteUTXOIndex(cls._utxo_index_path(db_path))
        invalid_blocks = SQLiteInvalidBlockCache(cls._invalid_blocks_path(db_path))
        try:
            if store.best_tip() is not None:
                raise ConsensusNodeError("node database already has a chain")
            store.save_chain_parameters(
                ChainParameters(
                    difficulty=difficulty,
                    block_subsidy=block_subsidy,
                    max_money=max_money,
                )
            )
            state = ConsensusState(block_subsidy=block_subsidy, max_money=max_money)
            genesis = cls._build_genesis_block(genesis_allocations, difficulty)
            BlockProcessor(state).apply_block(genesis, 0, "0" * 64)
            store.put_block(genesis)
            mempool_store.replace_all([])
            utxo_index.save_snapshot(genesis.hash, state.utxos)
        except Exception:
            store.close()
            mempool_store.close()
            utxo_index.close()
            invalid_blocks.close()
            raise
        fork_choice = ForkChoice(store, block_subsidy=block_subsidy, max_money=max_money)
        active = fork_choice.active_chain()
        return cls(
            store=store,
            state=active.state,
            difficulty=difficulty,
            fork_choice=fork_choice,
            mempool_store=mempool_store,
            utxo_index=utxo_index,
            invalid_blocks=invalid_blocks,
        )

    @classmethod
    def open(
        cls,
        db_path: str | Path,
        difficulty: int = 3,
        block_subsidy: int = 50,
        max_money: int = MAX_MONEY,
    ) -> ConsensusNode:
        """Open an existing node, validating stored parameters and branches."""

        store = SQLiteForkStore(db_path)
        mempool_store = SQLiteMempoolStore(cls._mempool_path(db_path))
        utxo_index = SQLiteUTXOIndex(cls._utxo_index_path(db_path))
        invalid_blocks = SQLiteInvalidBlockCache(cls._invalid_blocks_path(db_path))
        fork_choice = ForkChoice(store, block_subsidy=block_subsidy, max_money=max_money)
        try:
            store.require_chain_parameters(
                ChainParameters(
                    difficulty=difficulty,
                    block_subsidy=block_subsidy,
                    max_money=max_money,
                )
            )
            active = fork_choice.active_chain()
            fork_choice.validate_all_known_branches()
            tip = active.tip
            indexed_utxos = utxo_index.load_snapshot(tip.hash) if tip else None
            if (
                indexed_utxos is not None
                and indexed_utxos.entries() == active.state.utxos.entries()
            ):
                active_state = ConsensusState(
                    utxos=indexed_utxos,
                    block_subsidy=block_subsidy,
                    max_money=max_money,
                )
            else:
                active_state = active.state
                if tip is not None:
                    utxo_index.save_snapshot(tip.hash, active_state.utxos)
        except Exception as exc:
            store.close()
            mempool_store.close()
            utxo_index.close()
            invalid_blocks.close()
            raise ConsensusNodeError("stored consensus chain failed validation") from exc
        return cls(
            store=store,
            state=active_state,
            difficulty=difficulty,
            fork_choice=fork_choice,
            mempool_store=mempool_store,
            utxo_index=utxo_index,
            invalid_blocks=invalid_blocks,
        )

    def close(self) -> None:
        self.store.close()
        if self.mempool_store is not None:
            self.mempool_store.close()
        if self.utxo_index is not None:
            self.utxo_index.close()
        if self.invalid_blocks is not None:
            self.invalid_blocks.close()

    @property
    def tip_hash(self) -> str:
        tip = self.store.best_tip()
        return tip.block.hash if tip else "0" * 64

    @property
    def height(self) -> int:
        tip = self.store.best_tip()
        return tip.block.height if tip else -1

    def submit_transaction(self, transaction: ConsensusTransaction) -> str:
        """Validate and persist a transaction in the local mempool."""

        txid = transaction.txid
        if transaction.is_coinbase:
            raise MempoolPolicyError("coinbase transactions cannot enter the mempool")
        if any(existing.txid == txid for existing in self.mempool):
            raise MempoolPolicyError("transaction already in mempool")
        replacement = self._replacement_context(transaction)
        candidate = self._candidate_after(replacement.remaining)
        fee = candidate.apply_transaction(transaction)
        if replacement.replaced:
            self._validate_replacement_fee(fee, replacement.replaced_fee)
        remaining = replacement.remaining
        if not replacement.replaced:
            remaining, fee = self._evict_for_policy(transaction, remaining, fee)
        self.mempool_policy.validate(
            transaction,
            fee=fee,
            current_count=len(remaining),
        )
        self.mempool = [*remaining, transaction]
        if self.mempool_store is not None:
            self.mempool_store.replace_all(self.mempool)
        return txid

    def mempool_summary(self) -> list[dict[str, object]]:
        """Return operator-facing mempool rows with fee and validity details."""

        candidate = ConsensusState(
            utxos=self.state.utxos.copy(),
            block_subsidy=self.state.block_subsidy,
            max_money=self.state.max_money,
        )
        summary: list[dict[str, object]] = []
        for transaction in self.mempool:
            output_value = sum(output.amount for output in transaction.outputs)
            size = self.mempool_policy.serialized_size(transaction)
            try:
                fee = candidate.apply_transaction(transaction)
                fee_rate = self.mempool_policy.fee_rate(fee, size)
                valid = True
            except Exception:
                fee = None
                fee_rate = None
                valid = False
            summary.append(
                {
                    "txid": transaction.txid,
                    "inputs": len(transaction.inputs),
                    "outputs": len(transaction.outputs),
                    "output_value": output_value,
                    "fee": fee,
                    "fee_rate": fee_rate,
                    "bytes": size,
                    "valid": valid,
                }
            )
        return summary

    def prune_mempool(self) -> int:
        """Drop entries that no longer validate against the active chain."""

        original_count = len(self.mempool)
        self._prune_invalid_mempool()
        if self.mempool_store is not None:
            self.mempool_store.replace_all(self.mempool)
        return original_count - len(self.mempool)

    def clear_mempool(self) -> int:
        removed = len(self.mempool)
        self.mempool = []
        if self.mempool_store is not None:
            self.mempool_store.replace_all([])
        return removed

    def spendable_outputs(self, address: str) -> tuple[UTXO, ...]:
        return self.state.utxos.entries_for_address(address)

    def spendable_summary(self, address: str) -> dict[str, object]:
        """Return balance and UTXO details for an address."""

        if not is_valid_address(address):
            raise ConsensusNodeError("address is invalid")
        entries = self.spendable_outputs(address)
        utxos = [
            {
                "txid": entry.outpoint.txid,
                "index": entry.outpoint.index,
                "amount": entry.output.amount,
                "address": entry.output.address,
            }
            for entry in entries
        ]
        return {
            "address": address,
            "balance": sum(entry.output.amount for entry in entries),
            "count": len(entries),
            "utxos": utxos,
        }

    def block_summary(
        self,
        *,
        block_hash: str | None = None,
        height: int | None = None,
    ) -> dict[str, object]:
        """Return a block summary by hash or active-chain height."""

        if (block_hash is None) == (height is None):
            raise ConsensusNodeError("provide exactly one of block_hash or height")
        active = self.fork_choice.active_chain()
        active_hashes = {block.hash for block in active.blocks}
        if height is not None:
            if height < 0:
                raise ConsensusNodeError("height cannot be negative")
            try:
                block = active.blocks[height]
            except IndexError as exc:
                raise ConsensusNodeError(f"unknown active-chain block height: {height}") from exc
            stored = self.store.get_block(block.hash)
        else:
            try:
                stored = self.store.get_block(str(block_hash))
            except StorageError as exc:
                raise ConsensusNodeError(f"unknown block: {block_hash}") from exc
            block = stored.block
        return {
            "hash": block.hash,
            "height": block.height,
            "previous_hash": block.previous_hash,
            "difficulty": block.difficulty,
            "timestamp": block.timestamp,
            "nonce": block.nonce,
            "transaction_root": block.transaction_root,
            "transactions": [transaction.txid for transaction in block.transactions],
            "transaction_count": len(block.transactions),
            "cumulative_work": stored.cumulative_work,
            "active": block.hash in active_hashes,
        }

    def transaction_summary(self, txid: str) -> dict[str, object]:
        """Return confirmed or mempool transaction details."""

        for block in self.fork_choice.active_chain().blocks:
            for transaction in block.transactions:
                if transaction.txid == txid:
                    return self._transaction_summary(
                        transaction,
                        status="confirmed",
                        block_hash=block.hash,
                        height=block.height,
                    )
        for transaction in self.mempool:
            if transaction.txid == txid:
                return self._transaction_summary(transaction, status="mempool")
        raise ConsensusNodeError(f"unknown transaction: {txid}")

    def create_transaction(
        self,
        wallet: Wallet,
        recipient: str,
        amount: int,
        fee: int = 1,
    ) -> ConsensusTransaction:
        """Construct and sign a spend from a wallet's available UTXOs."""

        if amount <= 0:
            raise ConsensusNodeError("amount must be positive")
        if fee < 0:
            raise ConsensusNodeError("fee cannot be negative")
        if not is_valid_address(recipient):
            raise ConsensusNodeError("recipient address is invalid")
        needed = amount + fee
        selected: list[UTXO] = []
        total = 0
        for utxo in self.spendable_outputs(wallet.address):
            selected.append(utxo)
            total += utxo.output.amount
            if total >= needed:
                break
        if total < needed:
            raise ConsensusNodeError("insufficient funds")
        outputs = [TxOutput(amount, recipient)]
        change = total - needed
        if change:
            outputs.append(TxOutput(change, wallet.address))
        transaction = ConsensusTransaction(
            inputs=tuple(TxInput(utxo.outpoint) for utxo in selected),
            outputs=tuple(outputs),
        )
        for index in range(len(transaction.inputs)):
            transaction = transaction.sign_input(index, wallet)
        return transaction

    def build_candidate_block(self, miner_address: str) -> ConsensusBlock:
        """Build an unmined block from fee-prioritized mempool transactions."""

        if not is_valid_address(miner_address):
            raise ConsensusNodeError("invalid miner address")
        candidate = ConsensusState(
            utxos=self.state.utxos.copy(),
            block_subsidy=self.state.block_subsidy,
            max_money=self.state.max_money,
        )
        transactions, fees = self._prioritized_mempool_transactions(candidate)
        next_height = self.height + 1
        coinbase = candidate.create_coinbase(
            miner_address,
            fees=fees,
            height=next_height,
        )
        return ConsensusBlock(
            height=next_height,
            previous_hash=self.tip_hash,
            transactions=(coinbase, *transactions),
            difficulty=self.difficulty,
        )

    def mine_block(self, miner_address: str) -> StoredConsensusBlock:
        """Mine and import a block paying the reward to ``miner_address``."""

        block = self.build_candidate_block(miner_address)
        block = self._mine(block)
        return self.import_block(block)

    def import_block(self, block: ConsensusBlock) -> StoredConsensusBlock:
        """Validate, store, and activate a block if it wins fork choice."""

        old_tip = self.store.best_tip()
        try:
            block_hash = block.hash
        except Exception as exc:
            raise ConsensusNodeError("block header is invalid") from exc
        if self.invalid_blocks is not None and self.invalid_blocks.is_invalid(block_hash):
            reason = self.invalid_blocks.reason(block_hash) or "previously rejected block"
            raise ConsensusNodeError(f"block is cached as invalid: {reason}")
        try:
            self._validate_import_candidate(block)
            stored = self.store.put_block(block)
        except Exception as exc:
            if self.invalid_blocks is not None:
                self.invalid_blocks.mark_invalid(block_hash, str(exc))
            raise
        active = self.fork_choice.active_chain()
        self.state = active.state
        new_tip = active.tip
        self.last_reorg = self._detect_reorg(old_tip.block if old_tip else None, new_tip)
        if self.utxo_index is not None:
            imported = self.fork_choice.chain_to_tip(stored.block.hash)
            self.utxo_index.save_tip_snapshot(stored.block.hash, imported.state.utxos)
            self.utxo_index.save_tip_spends(stored.block.hash, imported.blocks)
            assert active.tip is not None
            self.utxo_index.save_snapshot(active.tip.hash, self.state.utxos)
            self.utxo_index.save_tip_spends(active.tip.hash, active.blocks)
        included_txids = {transaction.txid for transaction in block.transactions}
        self.mempool = [
            transaction for transaction in self.mempool if transaction.txid not in included_txids
        ]
        self._prune_invalid_mempool()
        if self.mempool_store is not None:
            self.mempool_store.replace_all(self.mempool)
        return stored

    def _load_mempool(self) -> list[ConsensusTransaction]:
        if self.mempool_store is None:
            return []
        valid: list[ConsensusTransaction] = []
        candidate = ConsensusState(
            utxos=self.state.utxos.copy(),
            block_subsidy=self.state.block_subsidy,
            max_money=self.state.max_money,
        )
        for transaction in self.mempool_store.transactions():
            try:
                fee = candidate.apply_transaction(transaction)
                self.mempool_policy.validate(
                    transaction,
                    fee=fee,
                    current_count=len(valid),
                )
            except Exception:
                continue
            valid.append(transaction)
        if len(valid) != self.mempool_store.count():
            self.mempool_store.replace_all(valid)
        return valid

    @dataclass(frozen=True)
    class _ReplacementContext:
        remaining: list[ConsensusTransaction]
        replaced: list[ConsensusTransaction]
        replaced_fee: int

    def _replacement_context(self, transaction: ConsensusTransaction) -> _ReplacementContext:
        new_inputs = self._input_outpoints(transaction)
        replaced = [
            existing
            for existing in self.mempool
            if new_inputs.intersection(self._input_outpoints(existing))
        ]
        if not replaced:
            return self._ReplacementContext(
                remaining=list(self.mempool),
                replaced=[],
                replaced_fee=0,
            )
        if not self.mempool_policy.allow_replacement:
            raise MempoolPolicyError("transaction conflicts with mempool policy")
        replaced_txids = {transaction.txid for transaction in replaced}
        remaining = [
            transaction for transaction in self.mempool if transaction.txid not in replaced_txids
        ]
        replacement_candidate = self._candidate_after(remaining)
        replaced_fee = 0
        for replaced_transaction in replaced:
            replaced_fee += replacement_candidate.apply_transaction(replaced_transaction)
        return self._ReplacementContext(
            remaining=remaining,
            replaced=replaced,
            replaced_fee=replaced_fee,
        )

    def _candidate_after(self, transactions: list[ConsensusTransaction]) -> ConsensusState:
        candidate = ConsensusState(
            utxos=self.state.utxos.copy(),
            block_subsidy=self.state.block_subsidy,
            max_money=self.state.max_money,
        )
        for transaction in transactions:
            candidate.apply_transaction(transaction)
        return candidate

    def _prune_invalid_mempool(self) -> None:
        valid: list[ConsensusTransaction] = []
        candidate = ConsensusState(
            utxos=self.state.utxos.copy(),
            block_subsidy=self.state.block_subsidy,
            max_money=self.state.max_money,
        )
        for transaction in self.mempool:
            try:
                fee = candidate.apply_transaction(transaction)
                self.mempool_policy.validate(
                    transaction,
                    fee=fee,
                    current_count=len(valid),
                )
            except Exception:
                continue
            valid.append(transaction)
        self.mempool = valid

    @staticmethod
    def _detect_reorg(
        old_tip: ConsensusBlock | None,
        new_tip: ConsensusBlock | None,
    ) -> ReorgEvent | None:
        if old_tip is None or new_tip is None:
            return None
        if old_tip.hash == new_tip.hash:
            return None
        if new_tip.previous_hash == old_tip.hash and new_tip.height == old_tip.height + 1:
            return None
        return ReorgEvent(
            old_tip=old_tip.hash,
            new_tip=new_tip.hash,
            old_height=old_tip.height,
            new_height=new_tip.height,
        )

    def _prioritized_mempool_transactions(
        self,
        candidate: ConsensusState,
    ) -> tuple[list[ConsensusTransaction], int]:
        pending = list(self.mempool)
        selected: list[ConsensusTransaction] = []
        fees = 0
        while pending:
            best_index: int | None = None
            best_score: tuple[int, int, str] | None = None
            for index, transaction in enumerate(pending):
                trial = ConsensusState(
                    utxos=candidate.utxos.copy(),
                    block_subsidy=candidate.block_subsidy,
                    max_money=candidate.max_money,
                )
                try:
                    fee = trial.apply_transaction(transaction)
                except Exception:
                    continue
                size = self.mempool_policy.serialized_size(transaction)
                score = (
                    self.mempool_policy.fee_rate(fee, size),
                    fee,
                    transaction.txid,
                )
                if best_score is None or score > best_score:
                    best_index = index
                    best_score = score
            if best_index is None:
                raise ConsensusNodeError("mempool contains transactions that cannot be mined")
            transaction = pending.pop(best_index)
            fees += candidate.apply_transaction(transaction)
            selected.append(transaction)
        return selected, fees

    def _evict_for_policy(
        self,
        transaction: ConsensusTransaction,
        remaining: list[ConsensusTransaction],
        fee: int,
    ) -> tuple[list[ConsensusTransaction], int]:
        if len(remaining) < self.mempool_policy.max_transactions:
            return remaining, fee
        if not self.mempool_policy.allow_eviction:
            return remaining, fee
        new_score = self._transaction_policy_score(transaction, fee)
        existing_fees = self._transaction_fees(remaining)
        best_remaining: list[ConsensusTransaction] | None = None
        best_fee: int | None = None
        best_score: tuple[int, int, str] | None = None
        for index, existing in enumerate(remaining):
            existing_fee = existing_fees[existing.txid]
            existing_score = self._transaction_policy_score(existing, existing_fee)
            candidate_remaining = [*remaining[:index], *remaining[index + 1 :]]
            try:
                candidate = self._candidate_after(candidate_remaining)
                candidate_fee = candidate.apply_transaction(transaction)
            except Exception:
                continue
            if best_score is None or existing_score < best_score:
                best_remaining = candidate_remaining
                best_fee = candidate_fee
                best_score = existing_score
        if best_remaining is None or best_fee is None or best_score is None:
            return remaining, fee
        required_fee_rate = best_score[0] + self.mempool_policy.eviction_fee_rate_bump
        if new_score[0] < required_fee_rate:
            raise MempoolPolicyError("transaction fee rate is below mempool eviction minimum")
        return best_remaining, best_fee

    def _transaction_fees(
        self,
        transactions: list[ConsensusTransaction],
    ) -> dict[str, int]:
        candidate = ConsensusState(
            utxos=self.state.utxos.copy(),
            block_subsidy=self.state.block_subsidy,
            max_money=self.state.max_money,
        )
        fees: dict[str, int] = {}
        for transaction in transactions:
            fees[transaction.txid] = candidate.apply_transaction(transaction)
        return fees

    def _transaction_policy_score(
        self,
        transaction: ConsensusTransaction,
        fee: int,
    ) -> tuple[int, int, str]:
        size = self.mempool_policy.serialized_size(transaction)
        return (self.mempool_policy.fee_rate(fee, size), fee, transaction.txid)

    def _validate_replacement_fee(self, fee: int, replaced_fee: int) -> None:
        required_fee = replaced_fee + self.mempool_policy.replacement_fee_bump
        if fee < required_fee:
            raise MempoolPolicyError("replacement fee is below mempool replacement minimum")

    @staticmethod
    def _input_outpoints(transaction: ConsensusTransaction) -> set[OutPoint]:
        return {tx_input.previous_output for tx_input in transaction.inputs}

    def active_chain_hashes(self) -> list[str]:
        return [block.hash for block in self.fork_choice.active_chain().blocks]

    def _validate_import_candidate(self, block: ConsensusBlock) -> None:
        if block.height == 0:
            if self.store.best_tip() is not None:
                raise ConsensusNodeError("genesis block already exists")
            BlockProcessor(
                ConsensusState(
                    block_subsidy=self.state.block_subsidy,
                    max_money=self.state.max_money,
                )
            ).apply_block(block, 0, "0" * 64)
            return
        try:
            parent_branch = self.fork_choice.chain_to_tip(block.previous_hash)
        except StorageError as exc:
            raise ConsensusNodeError("block parent is unknown") from exc
        processor = BlockProcessor(parent_branch.state)
        processor.apply_block(block, block.height, block.previous_hash)

    @staticmethod
    def _transaction_summary(
        transaction: ConsensusTransaction,
        *,
        status: str,
        block_hash: str | None = None,
        height: int | None = None,
    ) -> dict[str, object]:
        payload = {
            "txid": transaction.txid,
            "status": status,
            "coinbase": transaction.is_coinbase,
            "inputs": [tx_input.to_dict() for tx_input in transaction.inputs],
            "outputs": [output.to_dict() for output in transaction.outputs],
            "input_count": len(transaction.inputs),
            "output_count": len(transaction.outputs),
            "output_value": sum(output.amount for output in transaction.outputs),
        }
        if block_hash is not None:
            payload["block_hash"] = block_hash
        if height is not None:
            payload["height"] = height
        return payload

    @staticmethod
    def _mine(block: ConsensusBlock) -> ConsensusBlock:
        nonce = block.nonce
        mined = block
        target = "0" * block.difficulty
        while not mined.hash.startswith(target):
            nonce += 1
            mined = ConsensusBlock(
                height=block.height,
                previous_hash=block.previous_hash,
                transactions=block.transactions,
                difficulty=block.difficulty,
                timestamp=block.timestamp,
                nonce=nonce,
                version=block.version,
            )
        return mined

    @staticmethod
    def _build_genesis_block(
        allocations: dict[str, int],
        difficulty: int,
    ) -> ConsensusBlock:
        if difficulty < 0:
            raise ConsensusNodeError("difficulty cannot be negative")
        if not allocations:
            raise ConsensusNodeError("genesis allocations are required")
        outputs: list[TxOutput] = []
        total = 0
        for address, amount in allocations.items():
            if not is_valid_address(address):
                raise ConsensusNodeError(f"invalid genesis address: {address}")
            if amount <= 0:
                raise ConsensusNodeError("genesis allocations must be positive")
            total += amount
            outputs.append(TxOutput(amount=amount, address=address))
        if total > MAX_MONEY:
            raise ConsensusNodeError("genesis allocations exceed maximum supply")
        coinbase = ConsensusTransaction(
            inputs=(TxInput(OutPoint("0" * 64, -1)),),
            outputs=tuple(outputs),
        )
        return ConsensusNode._mine(
            ConsensusBlock(
                height=0,
                previous_hash="0" * 64,
                transactions=(coinbase,),
                difficulty=difficulty,
            )
        )

    @staticmethod
    def _utxo_index_path(db_path: str | Path) -> Path:
        path = Path(db_path)
        return path.with_suffix(f"{path.suffix}.utxos.sqlite3" if path.suffix else ".utxos.sqlite3")

    @staticmethod
    def _mempool_path(db_path: str | Path) -> Path:
        path = Path(db_path)
        return path.with_suffix(
            f"{path.suffix}.mempool.sqlite3" if path.suffix else ".mempool.sqlite3"
        )

    @staticmethod
    def _invalid_blocks_path(db_path: str | Path) -> Path:
        path = Path(db_path)
        return path.with_suffix(
            f"{path.suffix}.invalid.sqlite3" if path.suffix else ".invalid.sqlite3"
        )
