import sqlite3
import tempfile
import unittest
from pathlib import Path

from blockchaincoin import Blockchain, BlockchainNode, Wallet
from blockchaincoin.chain import BlockchainError
from blockchaincoin.models import COINBASE, Block, Transaction
from blockchaincoin.storage import SQLiteBlockStore, StorageError


class StorageTests(unittest.TestCase):
    def test_store_persists_blocks_and_reopens(self) -> None:
        alice = Wallet.create()
        chain = Blockchain.create({alice.address: 10}, difficulty=0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            store = SQLiteBlockStore(path)
            stored = store.put_block(chain.last_block)
            self.assertEqual(stored.cumulative_work, 1)
            self.assertTrue(store.has_block(chain.last_block.hash))
            tip = store.tip()
            self.assertIsNotNone(tip)
            assert tip is not None
            self.assertEqual(tip.block.hash, chain.last_block.hash)
            self.assertEqual(store.block_at_height(0).block.hash, chain.last_block.hash)
            self.assertEqual(
                [item.block.hash for item in store.iter_blocks()], [chain.last_block.hash]
            )
            self.assertEqual(store.put_block(chain.last_block).cumulative_work, 1)
            store.close()

            reopened = SQLiteBlockStore(path)
            reopened_tip = reopened.tip()
            self.assertIsNotNone(reopened_tip)
            assert reopened_tip is not None
            self.assertEqual(reopened_tip.block.hash, chain.last_block.hash)
            reopened.close()

    def test_store_rejects_invalid_append_order_and_unknown_blocks(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        chain = Blockchain.create({alice.address: 10}, difficulty=0)

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteBlockStore(Path(tmp) / "node.sqlite3")
            bad_first = Block(
                1, chain.last_block.hash, [Transaction(COINBASE, alice.address, 1)], 0
            )
            with self.assertRaises(StorageError):
                store.put_block(bad_first)
            store.put_block(chain.last_block)

            bad_height = Block(
                2, chain.last_block.hash, [Transaction(COINBASE, alice.address, 1)], 0
            )
            with self.assertRaises(StorageError):
                store.put_block(bad_height)

            bad_previous = Block(1, "1" * 64, [Transaction(COINBASE, bob.address, 1)], 0)
            with self.assertRaises(StorageError):
                store.put_block(bad_previous)

            with self.assertRaises(StorageError):
                store.get_block("missing")
            with self.assertRaises(StorageError):
                store.block_at_height(99)
            store.close()

    def test_store_rejects_unsupported_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            store = SQLiteBlockStore(path)
            store.close()
            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()

            with self.assertRaises(StorageError):
                SQLiteBlockStore(path)


class NodeTests(unittest.TestCase):
    def test_node_mines_persists_and_reopens(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = BlockchainNode.create(
                path,
                {alice.address: 50},
                difficulty=0,
                mining_reward=5,
            )
            tx = node.chain.create_transaction(alice, bob.address, 10, 1)
            self.assertEqual(node.submit_transaction(tx), tx.txid)
            stored = node.mine(alice.address)
            self.assertEqual(stored.block.index, 1)
            self.assertEqual(stored.cumulative_work, 2)
            node.close()

            reopened = BlockchainNode.open(path, difficulty=0, mining_reward=5)
            self.assertEqual(reopened.chain.balance_of(bob.address), 10)
            reopened_tip = reopened.store.tip()
            self.assertIsNotNone(reopened_tip)
            assert reopened_tip is not None
            self.assertEqual(reopened_tip.block.index, 1)
            reopened.close()

    def test_node_create_rejects_existing_database(self) -> None:
        alice = Wallet.create()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = BlockchainNode.create(path, {alice.address: 1}, difficulty=0)
            node.close()

            with self.assertRaises(Exception) as context:
                BlockchainNode.create(path, {alice.address: 1}, difficulty=0)
            self.assertIn("already has a chain", str(context.exception))

    def test_node_open_rejects_invalid_stored_chain_and_imports_valid_block(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = BlockchainNode.create(path, {alice.address: 50}, difficulty=0)
            tx = node.chain.create_transaction(alice, bob.address, 5, 0)
            node.chain.add_transaction(tx)
            block = node.chain.mine_pending_transactions(alice.address)
            imported = BlockchainNode.open(path, difficulty=0)
            imported.import_block(block)
            self.assertEqual(imported.chain.balance_of(bob.address), 5)

            tampered = Block(
                block.index + 1,
                "1" * 64,
                [Transaction(COINBASE, alice.address, 50)],
                difficulty=0,
            )
            with self.assertRaises(BlockchainError):
                imported.import_block(tampered)
            imported.close()
            node.close()

            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE blocks SET previous_hash = 'bad' WHERE height = 0")
                row = connection.execute("SELECT data FROM blocks WHERE height = 0").fetchone()
                data = row[0].replace(
                    '"previous_hash":"0000000000000000000000000000000000000000000000000000000000000000"',
                    '"previous_hash":"bad"',
                )
                connection.execute("UPDATE blocks SET data = ? WHERE height = 0", (data,))
            connection.close()

            with self.assertRaises(Exception) as context:
                BlockchainNode.open(path, difficulty=0)
            self.assertIn("failed validation", str(context.exception))


if __name__ == "__main__":
    unittest.main()
