"""Legacy account-ledger blockchain.

This module is retained for compatibility with earlier CLI workflows. The
academic release documents the UTXO node as the consensus-relevant path, but the
legacy chain still receives explicit documentation so readers can identify its
nonce-based account model, storage format, and limits.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from .crypto import atomic_write_json, is_valid_address
from .models import COINBASE, Block, Transaction, calculate_merkle_root

if TYPE_CHECKING:
    from .crypto import Wallet


class BlockchainError(ValueError):
    """Raised when the compatibility account ledger violates its rules."""

    pass


CHAIN_SCHEMA_VERSION = 1
MAX_MONEY = 21_000_000_000_000_000


class Blockchain:
    """In-memory compatibility chain with JSON persistence helpers.

    The class validates account balances, nonces, signatures, coinbase rewards,
    proof of work, and maximum supply for the legacy account model. It is not
    fork-aware; the UTXO node provides the durable fork-choice architecture.
    """

    def __init__(
        self,
        difficulty: int = 3,
        mining_reward: int = 50,
        blocks: list[Block] | None = None,
        mempool: list[Transaction] | None = None,
    ) -> None:
        if difficulty < 0:
            raise BlockchainError("difficulty cannot be negative")
        if mining_reward < 0:
            raise BlockchainError("mining reward cannot be negative")
        self.difficulty = difficulty
        self.mining_reward = mining_reward
        self.blocks = blocks or []
        self.mempool = mempool or []

    @classmethod
    def create(
        cls,
        genesis_allocations: dict[str, int] | None = None,
        difficulty: int = 3,
        mining_reward: int = 50,
    ) -> Blockchain:
        """Create a new compatibility chain and mine its genesis block."""

        chain = cls(difficulty=difficulty, mining_reward=mining_reward)
        allocations = genesis_allocations or {}
        for address, amount in allocations.items():
            if not is_valid_address(address):
                raise BlockchainError(f"invalid genesis address: {address}")
            if amount <= 0:
                raise BlockchainError("genesis allocations must be positive")
        if sum(allocations.values()) > MAX_MONEY:
            raise BlockchainError("genesis allocations exceed maximum supply")
        transactions = [
            Transaction(sender=COINBASE, recipient=address, amount=amount)
            for address, amount in allocations.items()
        ]
        genesis = Block(
            index=0,
            previous_hash="0" * 64,
            transactions=transactions,
            difficulty=difficulty,
        )
        chain._mine_block(genesis)
        chain.blocks.append(genesis)
        return chain

    @property
    def last_block(self) -> Block:
        if not self.blocks:
            raise BlockchainError("chain has no genesis block")
        return self.blocks[-1]

    def balances(self, include_mempool: bool = False) -> dict[str, int]:
        """Return account balances after confirmed blocks and optional mempool."""

        balances: dict[str, int] = defaultdict(int)
        for block in self.blocks:
            self._apply_transactions_to_balances(block.transactions, balances)
        if include_mempool:
            self._apply_transactions_to_balances(self.mempool, balances)
        return dict(balances)

    def balance_of(self, address: str, include_mempool: bool = False) -> int:
        return self.balances(include_mempool=include_mempool).get(address, 0)

    def circulating_supply(self) -> int:
        return sum(self.balances().values())

    def next_nonce(self, address: str, include_mempool: bool = True) -> int:
        nonce = 0
        for block in self.blocks:
            for tx in block.transactions:
                if tx.sender == address:
                    nonce = max(nonce, tx.nonce + 1)
        if include_mempool:
            for tx in self.mempool:
                if tx.sender == address:
                    nonce = max(nonce, tx.nonce + 1)
        return nonce

    def create_transaction(
        self,
        sender_wallet: Wallet,
        recipient: str,
        amount: int,
        fee: int = 1,
    ) -> Transaction:
        """Build and sign a nonce-based compatibility transaction."""

        from .crypto import Wallet

        if not isinstance(sender_wallet, Wallet):
            raise TypeError("sender_wallet must be a Wallet")
        tx = Transaction(
            sender=sender_wallet.address,
            recipient=recipient,
            amount=amount,
            fee=fee,
            nonce=self.next_nonce(sender_wallet.address),
        )
        tx.sign_with(sender_wallet)
        return tx

    def add_transaction(self, transaction: Transaction) -> str:
        """Validate and queue a transaction in the compatibility mempool."""

        txid = transaction.txid
        if any(existing.txid == txid for existing in self.mempool):
            raise BlockchainError("transaction is already in the mempool")
        self._validate_transaction(
            transaction,
            balances=self.balances(include_mempool=True),
            expected_nonce=self.next_nonce(transaction.sender),
            allow_coinbase=False,
        )
        self.mempool.append(transaction)
        return txid

    def mine_pending_transactions(self, miner_address: str) -> Block:
        """Mine a block containing all pending compatibility transactions."""

        if not is_valid_address(miner_address):
            raise BlockchainError("invalid miner address")
        if self.circulating_supply() + self.mining_reward > MAX_MONEY:
            raise BlockchainError("mining reward would exceed maximum supply")
        fees = sum(tx.fee for tx in self.mempool)
        coinbase = Transaction(
            sender=COINBASE,
            recipient=miner_address,
            amount=self.mining_reward + fees,
        )
        block = Block(
            index=len(self.blocks),
            previous_hash=self.last_block.hash,
            transactions=[coinbase, *self.mempool],
            difficulty=self.difficulty,
        )
        self._validate_block(block)
        self._mine_block(block)
        self.blocks.append(block)
        self.mempool = []
        return block

    def is_valid(self) -> bool:
        """Return whether all compatibility-chain blocks satisfy local rules."""

        try:
            if not self.blocks:
                return False
            for index, block in enumerate(self.blocks):
                if block.index != index:
                    return False
                if not block.hash.startswith("0" * block.difficulty):
                    return False
                if block.merkle_root != calculate_merkle_root(
                    [tx.txid for tx in block.transactions]
                ):
                    return False
                if index == 0:
                    if block.previous_hash != "0" * 64:
                        return False
                elif block.previous_hash != self.blocks[index - 1].hash:
                    return False
                self._validate_block(block, historical=True)
        except BlockchainError:
            return False
        return True

    def save(self, path: str | Path) -> None:
        """Persist the compatibility chain as schema-versioned JSON."""

        payload = {
            "schema_version": CHAIN_SCHEMA_VERSION,
            "difficulty": self.difficulty,
            "mining_reward": self.mining_reward,
            "blocks": [block.to_dict() for block in self.blocks],
            "mempool": [tx.to_dict() for tx in self.mempool],
        }
        atomic_write_json(path, payload)

    @classmethod
    def load(cls, path: str | Path) -> Blockchain:
        """Load and validate a schema-versioned compatibility chain file."""

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        schema_version = int(data.get("schema_version", 0))
        if schema_version != CHAIN_SCHEMA_VERSION:
            raise BlockchainError(f"unsupported chain schema version: {schema_version}")
        chain = cls(
            difficulty=int(data["difficulty"]),
            mining_reward=int(data["mining_reward"]),
            blocks=[Block.from_dict(block) for block in data["blocks"]],
            mempool=[Transaction.from_dict(tx) for tx in data.get("mempool", [])],
        )
        if not chain.is_valid():
            raise BlockchainError("chain file failed validation")
        chain._validate_mempool()
        return chain

    def _mine_block(self, block: Block) -> None:
        target = "0" * block.difficulty
        while not block.hash.startswith(target):
            block.nonce += 1

    def _validate_block(self, block: Block, historical: bool = False) -> None:
        if block.merkle_root != calculate_merkle_root([tx.txid for tx in block.transactions]):
            raise BlockchainError("invalid merkle root")
        if not block.transactions:
            raise BlockchainError("block has no transactions")

        coinbase_count = sum(1 for tx in block.transactions if tx.is_coinbase)
        if block.index > 0 and coinbase_count > 1:
            raise BlockchainError("block has more than one coinbase transaction")
        if block.index > 0 and not block.transactions[0].is_coinbase:
            raise BlockchainError("first transaction must be coinbase")

        balances: dict[str, int] = defaultdict(int)
        expected_nonces: dict[str, int] = defaultdict(int)
        for existing in self.blocks[: block.index if historical else len(self.blocks)]:
            for tx in existing.transactions:
                if tx.sender != COINBASE:
                    expected_nonces[tx.sender] = max(expected_nonces[tx.sender], tx.nonce + 1)
            self._apply_transactions_to_balances(existing.transactions, balances)

        block_fees = sum(tx.fee for tx in block.transactions if not tx.is_coinbase)
        for tx in block.transactions:
            if tx.is_coinbase:
                expected_reward = self.mining_reward + block_fees if block.index > 0 else tx.amount
                if tx.amount != expected_reward or tx.fee != 0:
                    raise BlockchainError("invalid coinbase reward")
                self._validate_coinbase(tx)
                balances[tx.recipient] += tx.amount
                continue

            self._validate_transaction(
                tx,
                balances=balances,
                expected_nonce=expected_nonces[tx.sender],
                allow_coinbase=False,
            )
            expected_nonces[tx.sender] += 1
            balances[tx.sender] -= tx.amount + tx.fee
            balances[tx.recipient] += tx.amount

        if sum(balances.values()) > MAX_MONEY:
            raise BlockchainError("block exceeds maximum supply")

    def _validate_transaction(
        self,
        transaction: Transaction,
        balances: dict[str, int],
        expected_nonce: int,
        allow_coinbase: bool,
    ) -> None:
        if transaction.is_coinbase and allow_coinbase:
            return
        if transaction.is_coinbase:
            raise BlockchainError("coinbase transactions can only be created by mining")
        if transaction.amount <= 0:
            raise BlockchainError("transaction amount must be positive")
        if transaction.fee < 0:
            raise BlockchainError("transaction fee cannot be negative")
        if transaction.amount > MAX_MONEY or transaction.fee > MAX_MONEY:
            raise BlockchainError("transaction amount or fee exceeds maximum supply")
        if not is_valid_address(transaction.sender):
            raise BlockchainError("invalid sender address")
        if not is_valid_address(transaction.recipient):
            raise BlockchainError("invalid recipient address")
        if transaction.sender == transaction.recipient:
            raise BlockchainError("sender and recipient must differ")
        if transaction.nonce < 0:
            raise BlockchainError("transaction nonce cannot be negative")
        if transaction.nonce != expected_nonce:
            raise BlockchainError(
                f"invalid nonce: expected {expected_nonce}, got {transaction.nonce}"
            )
        if balances.get(transaction.sender, 0) < transaction.amount + transaction.fee:
            raise BlockchainError("insufficient funds")
        if not transaction.has_valid_signature():
            raise BlockchainError("invalid transaction signature")

    @staticmethod
    def _apply_transactions_to_balances(
        transactions: list[Transaction],
        balances: dict[str, int],
    ) -> None:
        for tx in transactions:
            if tx.sender != COINBASE:
                balances[tx.sender] -= tx.amount + tx.fee
            balances[tx.recipient] += tx.amount

    def _validate_coinbase(self, transaction: Transaction) -> None:
        if transaction.sender != COINBASE:
            raise BlockchainError("invalid coinbase sender")
        if not is_valid_address(transaction.recipient):
            raise BlockchainError("invalid coinbase recipient")
        if transaction.amount < 0:
            raise BlockchainError("coinbase amount cannot be negative")
        if transaction.amount > MAX_MONEY:
            raise BlockchainError("coinbase amount exceeds maximum supply")
        if transaction.signature is not None or transaction.public_key is not None:
            raise BlockchainError("coinbase transactions must not be signed")

    def _validate_mempool(self) -> None:
        seen: set[str] = set()
        balances: dict[str, int] = defaultdict(int)
        balances.update(self.balances(include_mempool=False))
        nonces: dict[str, int] = defaultdict(int)
        for block in self.blocks:
            for tx in block.transactions:
                if tx.sender != COINBASE:
                    nonces[tx.sender] = max(nonces[tx.sender], tx.nonce + 1)
        for tx in self.mempool:
            txid = tx.txid
            if txid in seen:
                raise BlockchainError("duplicate transaction in mempool")
            seen.add(txid)
            self._validate_transaction(
                tx,
                balances=balances,
                expected_nonce=nonces[tx.sender],
                allow_coinbase=False,
            )
            balances[tx.sender] -= tx.amount + tx.fee
            balances[tx.recipient] += tx.amount
            nonces[tx.sender] += 1
