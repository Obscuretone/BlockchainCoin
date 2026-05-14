"""Compatibility full-node facade for the legacy account ledger."""

from __future__ import annotations

from pathlib import Path

from .chain import Blockchain, BlockchainError
from .models import Block, Transaction
from .storage import SQLiteBlockStore, StoredBlock


class NodeError(RuntimeError):
    """Raised when the compatibility node cannot open or create local state."""

    pass


class BlockchainNode:
    """A local full-node facade backed by durable compatibility storage.

    This facade predates the UTXO node and remains available for compatibility
    tests and local account-ledger workflows. New academic consensus evaluation
    should use ``ConsensusNode`` from ``utxo_node``.
    """

    def __init__(self, store: SQLiteBlockStore, chain: Blockchain) -> None:
        self.store = store
        self.chain = chain

    @classmethod
    def create(
        cls,
        db_path: str | Path,
        genesis_allocations: dict[str, int],
        difficulty: int = 3,
        mining_reward: int = 50,
    ) -> BlockchainNode:
        """Create a compatibility node database with a mined genesis block."""

        store = SQLiteBlockStore(db_path)
        if store.tip() is not None:
            store.close()
            raise NodeError("node database already has a chain")
        chain = Blockchain.create(
            genesis_allocations=genesis_allocations,
            difficulty=difficulty,
            mining_reward=mining_reward,
        )
        store.put_block(chain.last_block)
        return cls(store=store, chain=chain)

    @classmethod
    def open(
        cls,
        db_path: str | Path,
        difficulty: int = 3,
        mining_reward: int = 50,
    ) -> BlockchainNode:
        """Open and validate a compatibility node database."""

        store = SQLiteBlockStore(db_path)
        blocks = [stored.block for stored in store.iter_blocks()]
        chain = Blockchain(
            difficulty=difficulty,
            mining_reward=mining_reward,
            blocks=blocks,
        )
        if not chain.is_valid():
            store.close()
            raise NodeError("stored chain failed validation")
        return cls(store=store, chain=chain)

    def close(self) -> None:
        self.store.close()

    def submit_transaction(self, transaction: Transaction) -> str:
        """Queue a compatibility transaction in memory."""

        return self.chain.add_transaction(transaction)

    def mine(self, miner_address: str) -> StoredBlock:
        """Mine pending compatibility transactions and persist the block."""

        block = self.chain.mine_pending_transactions(miner_address)
        return self.store.put_block(block)

    def import_block(self, block: Block) -> StoredBlock:
        """Validate and append one compatibility block."""

        candidate = Blockchain(
            difficulty=self.chain.difficulty,
            mining_reward=self.chain.mining_reward,
            blocks=[*self.chain.blocks, block],
        )
        if not candidate.is_valid():
            raise BlockchainError("imported block failed validation")
        stored = self.store.put_block(block)
        self.chain = candidate
        return stored
