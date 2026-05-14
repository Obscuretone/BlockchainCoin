import sqlite3
import tempfile
import unittest
from pathlib import Path

from blockchaincoin import ConsensusNode, Wallet
from blockchaincoin.consensus import ConsensusBlock, ConsensusState
from blockchaincoin.storage import ChainParameters, SQLiteForkStore, StorageError


class ForkStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Wallet.create()

    def make_child(
        self,
        parent: ConsensusBlock,
        difficulty: int = 0,
        nonce: int = 0,
    ) -> ConsensusBlock:
        state = ConsensusState()
        coinbase = state.create_coinbase(
            self.alice.address,
            height=parent.height + 1,
        )
        block = ConsensusBlock(
            height=parent.height + 1,
            previous_hash=parent.hash,
            transactions=(coinbase,),
            difficulty=difficulty,
            nonce=nonce,
        )
        return ConsensusNode._mine(block)

    def test_fork_store_selects_best_tip_by_cumulative_work(self) -> None:
        genesis = ConsensusNode._build_genesis_block({self.alice.address: 10}, difficulty=0)
        low_a = self.make_child(genesis, difficulty=0, nonce=1)
        low_b = self.make_child(low_a, difficulty=0, nonce=2)
        high_a = self.make_child(genesis, difficulty=2)

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteForkStore(Path(tmp) / "forks.sqlite3")
            self.assertEqual(store.active_chain(), [])
            self.assertIsNone(store.best_tip())

            self.assertEqual(store.put_block(genesis).cumulative_work, 1)
            self.assertEqual(store.put_block(low_a).cumulative_work, 2)
            self.assertEqual(store.put_block(low_b).cumulative_work, 3)
            self.assertEqual(store.put_block(high_a).cumulative_work, 5)
            self.assertEqual(store.put_block(high_a).block.hash, high_a.hash)

            self.assertTrue(store.has_block(low_b.hash))
            tip = store.best_tip()
            self.assertIsNotNone(tip)
            assert tip is not None
            self.assertEqual(tip.block.hash, high_a.hash)
            self.assertEqual(
                [stored.block.hash for stored in store.active_chain()],
                [genesis.hash, high_a.hash],
            )
            self.assertEqual(
                [stored.block.hash for stored in store.chain_to_tip(low_b.hash)],
                [genesis.hash, low_a.hash, low_b.hash],
            )
            self.assertEqual(
                [stored.block.hash for stored in store.children_of(genesis.hash)],
                sorted([low_a.hash, high_a.hash]),
            )
            self.assertEqual(
                [stored.block.hash for stored in store.iter_blocks()],
                [genesis.hash, *sorted([low_a.hash, high_a.hash]), low_b.hash],
            )
            parameters = ChainParameters(difficulty=0, block_subsidy=50, max_money=21_000_000)
            store.save_chain_parameters(parameters)
            self.assertEqual(store.load_chain_parameters(), parameters)
            store.require_chain_parameters(parameters)
            self.assertEqual(len(parameters.chain_id), 64)
            with self.assertRaises(StorageError):
                store.require_chain_parameters(
                    ChainParameters(difficulty=1, block_subsidy=50, max_money=21_000_000)
                )
            row = store.connection.execute(
                "SELECT data FROM fork_blocks WHERE hash = ?",
                (high_a.hash,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(bytes.fromhex(str(row["data"])), high_a.to_bytes())
            store.close()

            reopened = SQLiteForkStore(Path(tmp) / "forks.sqlite3")
            reopened_tip = reopened.best_tip()
            self.assertIsNotNone(reopened_tip)
            assert reopened_tip is not None
            self.assertEqual(reopened_tip.block.hash, high_a.hash)
            reopened.close()

            missing_params = SQLiteForkStore(Path(tmp) / "missing-params.sqlite3")
            missing_params.put_block(genesis)
            with self.assertRaises(StorageError):
                missing_params.require_chain_parameters(
                    ChainParameters(difficulty=0, block_subsidy=50, max_money=21_000_000)
                )
            missing_params.close()

    def test_fork_store_rejects_orphans_bad_genesis_and_schema(self) -> None:
        genesis = ConsensusNode._build_genesis_block({self.alice.address: 10}, difficulty=0)
        child = self.make_child(genesis)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forks.sqlite3"
            store = SQLiteForkStore(path)
            with self.assertRaises(StorageError):
                store.put_block(child)
            with self.assertRaises(StorageError):
                store.put_block(
                    ConsensusBlock(
                        height=0,
                        previous_hash="f" * 64,
                        transactions=genesis.transactions,
                        difficulty=0,
                    )
                )
            store.put_block(genesis)
            with self.assertRaises(StorageError):
                store.put_block(
                    ConsensusBlock(
                        height=3,
                        previous_hash=genesis.hash,
                        transactions=child.transactions,
                        difficulty=0,
                    )
                )
            with self.assertRaises(StorageError):
                store.get_block("missing")
            store.close()

            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()
            with self.assertRaises(StorageError):
                SQLiteForkStore(path)


if __name__ == "__main__":
    unittest.main()
