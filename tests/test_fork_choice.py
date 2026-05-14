import sqlite3
import tempfile
import unittest
from pathlib import Path

from blockchaincoin import ConsensusNode, Wallet
from blockchaincoin.consensus import (
    COINBASE_PREV_TXID,
    ConsensusBlock,
    ConsensusState,
    ConsensusTransaction,
    OutPoint,
    TxInput,
    TxOutput,
)
from blockchaincoin.fork_choice import ForkChoice, ForkChoiceError
from blockchaincoin.storage import SQLiteForkStore


class ForkChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Wallet.create()

    def make_child(
        self,
        parent: ConsensusBlock,
        difficulty: int = 0,
        nonce: int = 0,
    ) -> ConsensusBlock:
        coinbase = ConsensusState().create_coinbase(
            self.alice.address,
            height=parent.height + 1,
        )
        return ConsensusNode._mine(
            ConsensusBlock(
                height=parent.height + 1,
                previous_hash=parent.hash,
                transactions=(coinbase,),
                difficulty=difficulty,
                nonce=nonce,
            )
        )

    def test_empty_store_returns_empty_active_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteForkStore(Path(tmp) / "forks.sqlite3")
            choice = ForkChoice(store)
            active = choice.active_chain()

            self.assertEqual(active.blocks, ())
            self.assertEqual(active.cumulative_work, 0)
            self.assertIsNone(active.tip)
            self.assertEqual(active.state.utxos.total(), 0)
            self.assertIsNone(choice.best_tip())
            choice.validate_all_known_branches()
            store.close()

    def test_heavier_side_branch_becomes_active(self) -> None:
        genesis = ConsensusNode._build_genesis_block({self.alice.address: 10}, difficulty=0)
        low_a = self.make_child(genesis, difficulty=0, nonce=1)
        low_b = self.make_child(low_a, difficulty=0, nonce=2)
        high_a = self.make_child(genesis, difficulty=2)

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteForkStore(Path(tmp) / "forks.sqlite3")
            choice = ForkChoice(store)
            tip = choice.add_block(genesis).tip
            self.assertIsNotNone(tip)
            assert tip is not None
            self.assertEqual(tip.hash, genesis.hash)
            tip = choice.add_block(low_a).tip
            self.assertIsNotNone(tip)
            assert tip is not None
            self.assertEqual(tip.hash, low_a.hash)
            tip = choice.add_block(low_b).tip
            self.assertIsNotNone(tip)
            assert tip is not None
            self.assertEqual(tip.hash, low_b.hash)

            active = choice.add_block(high_a)
            self.assertIsNotNone(active.tip)
            assert active.tip is not None
            self.assertEqual(active.tip.hash, high_a.hash)
            self.assertEqual([block.hash for block in active.blocks], [genesis.hash, high_a.hash])
            self.assertEqual(active.cumulative_work, 5)
            self.assertEqual(active.state.utxos.total(), 60)

            low_branch = choice.chain_to_tip(low_b.hash)
            self.assertEqual(
                [block.hash for block in low_branch.blocks], [genesis.hash, low_a.hash, low_b.hash]
            )
            self.assertEqual(low_branch.cumulative_work, 3)
            choice.validate_all_known_branches()
            store.close()

    def test_invalid_stored_branch_is_rejected(self) -> None:
        genesis = ConsensusNode._build_genesis_block({self.alice.address: 10}, difficulty=0)
        child = self.make_child(genesis)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forks.sqlite3"
            store = SQLiteForkStore(path)
            choice = ForkChoice(store)
            choice.add_block(genesis)
            choice.add_block(child)
            store.close()

            connection = sqlite3.connect(path)
            with connection:
                corrupted_child = ConsensusBlock(
                    height=child.height,
                    previous_hash="f" * 64,
                    transactions=child.transactions,
                    difficulty=child.difficulty,
                    timestamp=child.timestamp,
                    nonce=child.nonce,
                    version=child.version,
                )
                connection.execute(
                    "UPDATE fork_blocks SET data = ? WHERE hash = ?",
                    (corrupted_child.to_bytes().hex(), child.hash),
                )
            connection.close()

            corrupted = SQLiteForkStore(path)
            corrupted_choice = ForkChoice(corrupted)
            with self.assertRaises(ForkChoiceError):
                corrupted_choice.active_chain()
            with self.assertRaises(ForkChoiceError):
                corrupted_choice.validate_all_known_branches()
            corrupted.close()

    def test_connected_but_consensus_invalid_branch_is_rejected(self) -> None:
        genesis = ConsensusNode._build_genesis_block({self.alice.address: 10}, difficulty=0)
        bad_reward = ConsensusBlock(
            height=1,
            previous_hash=genesis.hash,
            transactions=(
                ConsensusTransaction(
                    inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -2)),),
                    outputs=(TxOutput(51, self.alice.address),),
                ),
            ),
            difficulty=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteForkStore(Path(tmp) / "forks.sqlite3")
            choice = ForkChoice(store)
            store.put_block(genesis)
            store.put_block(bad_reward)

            with self.assertRaises(ForkChoiceError):
                choice.active_chain()
            store.close()


if __name__ == "__main__":
    unittest.main()
