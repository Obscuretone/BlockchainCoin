import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import cast

from blockchaincoin import ConsensusNode, Wallet
from blockchaincoin.chain import MAX_MONEY
from blockchaincoin.consensus import (
    UTXO,
    BlockProcessor,
    ConsensusBlock,
    ConsensusError,
    ConsensusState,
    ConsensusTransaction,
    OutPoint,
    TxInput,
    TxOutput,
    UTXOSet,
)
from blockchaincoin.storage import (
    IndexedSpend,
    SQLiteConsensusStore,
    SQLiteForkStore,
    SQLiteInvalidBlockCache,
    SQLiteMempoolStore,
    SQLiteUTXOIndex,
    StorageError,
)
from blockchaincoin.utxo_node import ConsensusNodeError, MempoolPolicy, MempoolPolicyError


class ConsensusStoreTests(unittest.TestCase):
    def test_consensus_store_persists_and_reopens(self) -> None:
        alice = Wallet.create()
        block = ConsensusNode._build_genesis_block({alice.address: 10}, difficulty=0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "consensus.sqlite3"
            store = SQLiteConsensusStore(path)
            stored = store.put_block(block)
            self.assertEqual(stored.cumulative_work, 1)
            self.assertTrue(store.has_block(block.hash))
            tip = store.tip()
            self.assertIsNotNone(tip)
            assert tip is not None
            self.assertEqual(tip.block.hash, block.hash)
            self.assertEqual(store.block_at_height(0).block.hash, block.hash)
            row = store.connection.execute(
                "SELECT data FROM consensus_blocks WHERE hash = ?",
                (block.hash,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(bytes.fromhex(str(row["data"])), block.to_bytes())
            self.assertEqual(store.put_block(block).block.hash, block.hash)
            store.close()

            reopened = SQLiteConsensusStore(path)
            self.assertEqual([item.block.hash for item in reopened.iter_blocks()], [block.hash])
            reopened.close()

            legacy_path = Path(tmp) / "legacy-consensus.sqlite3"
            legacy_store = SQLiteConsensusStore(legacy_path)
            with legacy_store.connection:
                legacy_store.connection.execute(
                    """
                    INSERT INTO consensus_blocks (
                        hash,
                        height,
                        previous_hash,
                        difficulty,
                        cumulative_work,
                        data
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        block.hash,
                        block.height,
                        block.previous_hash,
                        block.difficulty,
                        1,
                        json.dumps(block.to_dict(), sort_keys=True, separators=(",", ":")),
                    ),
                )
            self.assertEqual(legacy_store.get_block(block.hash).block.hash, block.hash)
            legacy_store.close()

            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()
            with self.assertRaises(StorageError):
                SQLiteConsensusStore(path)

    def test_utxo_index_persists_snapshots_and_rejects_bad_schema(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        outpoint = OutPoint("a" * 64, 0)
        utxos = UTXOSet([UTXO(outpoint, TxOutput(7, alice.address))])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utxos.sqlite3"
            index = SQLiteUTXOIndex(path)
            self.assertIsNone(index.active_tip())
            self.assertIsNone(index.load_snapshot("missing"))
            index.save_snapshot("tip", utxos)
            self.assertEqual(index.active_tip(), "tip")
            self.assertEqual(index.count(), 1)
            index.save_tip_snapshot(
                "other",
                UTXOSet([UTXO(OutPoint("b" * 64, 1), TxOutput(3, alice.address))]),
            )
            self.assertEqual(index.active_tip(), "tip")
            self.assertEqual(index.tip_count("tip"), 1)
            self.assertEqual(index.tip_count("other"), 1)
            self.assertEqual(index.indexed_tips(), ("other", "tip"))
            loaded = index.load_snapshot("tip")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertTrue(loaded.contains(outpoint))
            self.assertEqual(loaded.total(), 7)
            other = index.load_tip_snapshot("other")
            self.assertIsNotNone(other)
            assert other is not None
            self.assertEqual(other.total(), 3)
            other_via_load = index.load_snapshot("other")
            self.assertIsNotNone(other_via_load)
            assert other_via_load is not None
            self.assertEqual(other_via_load.total(), 3)
            self.assertEqual(
                [
                    entry.outpoint
                    for entry in index.utxos_for_address_at_tip("other", alice.address)
                ],
                [OutPoint("b" * 64, 1)],
            )
            self.assertEqual(index.utxos_for_address_at_tip("other", bob.address), ())
            self.assertIsNone(index.load_snapshot("missing"))
            with index.connection:
                index.connection.execute("DELETE FROM utxo_snapshots")
            active_fallback = index.load_snapshot("tip")
            self.assertIsNotNone(active_fallback)
            assert active_fallback is not None
            self.assertEqual(active_fallback.total(), 7)
            spend = IndexedSpend(
                outpoint=outpoint,
                spending_txid="c" * 64,
                input_index=0,
            )
            spending_tx = ConsensusTransaction(
                inputs=(TxInput(outpoint),),
                outputs=(TxOutput(1, alice.address),),
            )
            block = ConsensusBlock(
                height=1,
                previous_hash="d" * 64,
                transactions=(
                    ConsensusState().create_coinbase(alice.address, height=1),
                    spending_tx,
                ),
                difficulty=0,
            )
            index.save_tip_spends("tip", (block,))
            self.assertEqual(index.spent_count("tip"), 1)
            self.assertEqual(
                index.load_tip_spends("tip"),
                (
                    IndexedSpend(
                        outpoint=spend.outpoint,
                        spending_txid=spending_tx.txid,
                        input_index=spend.input_index,
                    ),
                ),
            )
            self.assertEqual(
                index.spent_by_tip("tip", outpoint),
                IndexedSpend(
                    outpoint=spend.outpoint,
                    spending_txid=spending_tx.txid,
                    input_index=spend.input_index,
                ),
            )
            self.assertTrue(index.is_spent_at_tip("tip", outpoint))
            self.assertIsNone(index.spent_by_tip("tip", OutPoint("e" * 64, 0)))
            self.assertFalse(index.is_spent_at_tip("tip", OutPoint("e" * 64, 0)))
            self.assertEqual(index.load_tip_spends("missing"), ())
            index.close()

            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()
            with self.assertRaises(StorageError):
                SQLiteUTXOIndex(path)

    def test_mempool_store_persists_and_rejects_bad_schema(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        transaction = ConsensusTransaction(
            inputs=(TxInput(OutPoint("a" * 64, 0)),),
            outputs=(TxOutput(3, bob.address), TxOutput(4, alice.address)),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mempool.sqlite3"
            store = SQLiteMempoolStore(path)
            store.add_transaction(transaction)
            store.add_transaction(transaction)
            self.assertEqual(store.count(), 1)
            self.assertEqual(store.transactions()[0].txid, transaction.txid)
            row = store.connection.execute(
                "SELECT data FROM mempool_transactions WHERE txid = ?",
                (transaction.txid,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(bytes.fromhex(str(row["data"])), transaction.to_bytes())
            store.replace_all([])
            self.assertEqual(store.count(), 0)
            with store.connection:
                store.connection.execute(
                    """
                    INSERT INTO mempool_transactions (txid, data)
                    VALUES (?, ?)
                    """,
                    (
                        transaction.txid,
                        json.dumps(transaction.to_dict(), sort_keys=True, separators=(",", ":")),
                    ),
                )
            self.assertEqual(store.transactions()[0].txid, transaction.txid)
            store.close()

            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()
            with self.assertRaises(StorageError):
                SQLiteMempoolStore(path)

    def test_invalid_block_cache_persists_and_rejects_bad_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.sqlite3"
            cache = SQLiteInvalidBlockCache(path)
            self.assertFalse(cache.is_invalid("a" * 64))
            self.assertIsNone(cache.reason("a" * 64))
            cache.mark_invalid("a" * 64, "bad parent")
            self.assertTrue(cache.is_invalid("a" * 64))
            self.assertEqual(cache.reason("a" * 64), "bad parent")
            self.assertEqual(cache.count(), 1)
            cache.mark_invalid("a" * 64, "bad proof")
            self.assertEqual(cache.reason("a" * 64), "bad proof")
            self.assertEqual(cache.count(), 1)
            cache.close()

            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()
            with self.assertRaises(StorageError):
                SQLiteInvalidBlockCache(path)

    def test_consensus_store_rejects_bad_order_and_unknown_blocks(self) -> None:
        alice = Wallet.create()
        genesis = ConsensusNode._build_genesis_block({alice.address: 10}, difficulty=0)
        orphan = ConsensusBlock(
            1,
            genesis.hash,
            (ConsensusState().create_coinbase(alice.address, height=1),),
            difficulty=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteConsensusStore(Path(tmp) / "consensus.sqlite3")
            with self.assertRaises(StorageError):
                store.put_block(orphan)
            store.put_block(genesis)
            with self.assertRaises(StorageError):
                store.put_block(ConsensusBlock(2, genesis.hash, orphan.transactions, 0))
            with self.assertRaises(StorageError):
                store.put_block(ConsensusBlock(1, "1" * 64, orphan.transactions, 0))
            valid_child = ConsensusBlock(1, genesis.hash, orphan.transactions, 0)
            self.assertEqual(store.put_block(valid_child).cumulative_work, 2)
            with self.assertRaises(StorageError):
                store.get_block("missing")
            with self.assertRaises(StorageError):
                store.block_at_height(99)
            store.close()


class ConsensusNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Wallet.create()
        self.bob = Wallet.create()

    def test_node_mines_fee_block_and_reopens_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path, {self.alice.address: 50}, difficulty=0, block_subsidy=5
            )
            self.assertEqual(node.height, 0)

            tip = node.store.best_tip()
            self.assertIsNotNone(tip)
            assert tip is not None
            genesis = tip.block.transactions[0]
            spend = ConsensusTransaction(
                inputs=(TxInput(OutPoint(genesis.txid, 0)),),
                outputs=(
                    TxOutput(20, self.bob.address),
                    TxOutput(25, self.alice.address),
                ),
            ).sign_input(0, self.alice)
            self.assertEqual(node.submit_transaction(spend), spend.txid)
            stored = node.mine_block(self.alice.address)

            self.assertEqual(stored.block.height, 1)
            self.assertEqual(stored.cumulative_work, 2)
            self.assertEqual(node.mempool, [])
            self.assertEqual(node.state.utxos.total(), 55)
            node.close()

            reopened = ConsensusNode.open(path, difficulty=0, block_subsidy=5)
            self.assertEqual(reopened.height, 1)
            self.assertEqual(reopened.state.utxos.total(), 55)
            self.assertEqual(len(reopened.active_chain_hashes()), 2)
            reopened.close()

            utxo_path = ConsensusNode._utxo_index_path(path)
            stale_index = SQLiteUTXOIndex(utxo_path)
            stale_index.save_snapshot(
                stored.block.hash,
                UTXOSet([UTXO(OutPoint("a" * 64, 0), TxOutput(1, self.bob.address))]),
            )
            stale_index.close()
            rebuilt_index = ConsensusNode.open(path, difficulty=0, block_subsidy=5)
            self.assertEqual(rebuilt_index.state.utxos.total(), 55)
            rebuilt_index.close()

            utxo_path.unlink()
            replayed = ConsensusNode.open(path, difficulty=0, block_subsidy=5)
            self.assertEqual(replayed.state.utxos.total(), 55)
            self.assertTrue(utxo_path.exists())
            replayed.close()

            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.open(path, difficulty=0, block_subsidy=6)

    def test_reorg_rebuilds_state_indexes_and_prunes_mempool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path,
                {self.alice.address: 100},
                difficulty=0,
                block_subsidy=5,
            )
            genesis = node.store.best_tip()
            self.assertIsNotNone(genesis)
            assert genesis is not None
            to_bob = node.create_transaction(self.alice, self.bob.address, amount=10, fee=1)
            node.submit_transaction(to_bob)
            low_tip = node.mine_block(self.alice.address).block

            bob_spend = node.create_transaction(self.bob, self.alice.address, amount=5, fee=1)
            node.submit_transaction(bob_spend)
            self.assertEqual([transaction.txid for transaction in node.mempool], [bob_spend.txid])

            side_state = ConsensusState(block_subsidy=5)
            BlockProcessor(side_state).apply_block(genesis.block, 0, "0" * 64)
            side_coinbase = side_state.create_coinbase(self.alice.address, height=1)
            side_block = ConsensusNode._mine(
                ConsensusBlock(
                    height=1,
                    previous_hash=genesis.block.hash,
                    transactions=(side_coinbase,),
                    difficulty=2,
                )
            )
            stored = node.import_block(side_block)

            self.assertEqual(stored.block.hash, side_block.hash)
            self.assertIsNotNone(node.last_reorg)
            assert node.last_reorg is not None
            self.assertEqual(node.last_reorg.old_tip, low_tip.hash)
            self.assertEqual(node.last_reorg.new_tip, side_block.hash)
            self.assertEqual(node.mempool, [])
            self.assertIsNotNone(node.mempool_store)
            assert node.mempool_store is not None
            self.assertEqual(node.mempool_store.count(), 0)
            self.assertEqual(
                sum(utxo.output.amount for utxo in node.spendable_outputs(self.bob.address)),
                0,
            )
            self.assertIsNotNone(node.utxo_index)
            assert node.utxo_index is not None
            self.assertEqual(node.utxo_index.active_tip(), side_block.hash)
            self.assertEqual(
                set(node.utxo_index.indexed_tips()),
                {genesis.block.hash, low_tip.hash, side_block.hash},
            )
            old_tip_snapshot = node.utxo_index.load_tip_snapshot(low_tip.hash)
            self.assertIsNotNone(old_tip_snapshot)
            assert old_tip_snapshot is not None
            self.assertEqual(
                sum(
                    entry.output.amount
                    for entry in old_tip_snapshot.entries_for_address(self.bob.address)
                ),
                10,
            )
            old_tip_spends = node.utxo_index.load_tip_spends(low_tip.hash)
            self.assertEqual(len(old_tip_spends), 1)
            self.assertEqual(old_tip_spends[0].spending_txid, to_bob.txid)
            self.assertEqual(node.utxo_index.load_tip_spends(side_block.hash), ())
            node.import_block(side_block)
            self.assertIsNone(node.last_reorg)
            node.close()

    def test_node_creates_signed_wallet_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 50}, difficulty=0)
            tx = node.create_transaction(self.alice, self.bob.address, amount=10, fee=2)
            self.assertEqual(len(tx.inputs), 1)
            self.assertEqual([output.amount for output in tx.outputs], [10, 38])
            node.submit_transaction(tx)
            mempool_summary = node.mempool_summary()
            self.assertEqual(mempool_summary[0]["fee"], 2)
            self.assertIsInstance(mempool_summary[0]["fee_rate"], int)
            pending_summary = node.transaction_summary(tx.txid)
            self.assertEqual(pending_summary["status"], "mempool")
            self.assertEqual(pending_summary["output_value"], 48)
            with self.assertRaises(ConsensusNodeError):
                node.transaction_summary("missing")
            node.mine_block(self.alice.address)
            block_summary = node.block_summary(height=1)
            self.assertEqual(block_summary["height"], 1)
            self.assertEqual(block_summary["transaction_count"], 2)
            self.assertTrue(block_summary["active"])
            self.assertEqual(
                node.block_summary(block_hash=str(block_summary["hash"]))["hash"],
                block_summary["hash"],
            )
            confirmed_summary = node.transaction_summary(tx.txid)
            self.assertEqual(confirmed_summary["status"], "confirmed")
            self.assertEqual(confirmed_summary["height"], 1)
            self.assertEqual(
                sum(utxo.output.amount for utxo in node.spendable_outputs(self.bob.address)),
                10,
            )
            summary = node.spendable_summary(self.bob.address)
            self.assertEqual(summary["balance"], 10)
            self.assertEqual(summary["count"], 1)
            summary_utxos = cast(list[dict[str, object]], summary["utxos"])
            self.assertEqual(summary_utxos[0]["address"], self.bob.address)
            with self.assertRaises(ConsensusNodeError):
                node.spendable_summary("bad")
            with self.assertRaises(ConsensusNodeError):
                node.block_summary()
            with self.assertRaises(ConsensusNodeError):
                node.block_summary(block_hash=str(block_summary["hash"]), height=1)
            with self.assertRaises(ConsensusNodeError):
                node.block_summary(height=-1)
            with self.assertRaises(ConsensusNodeError):
                node.block_summary(height=99)
            with self.assertRaises(ConsensusNodeError):
                node.block_summary(block_hash="missing")
            exact = node.create_transaction(self.bob, self.alice.address, amount=9, fee=1)
            self.assertEqual([output.amount for output in exact.outputs], [9])
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            node = ConsensusNode.create(
                Path(tmp) / "node.sqlite3",
                {self.alice.address: 5},
                difficulty=0,
            )
            with self.assertRaises(ConsensusNodeError):
                node.create_transaction(self.alice, self.bob.address, 0)
            with self.assertRaises(ConsensusNodeError):
                node.create_transaction(self.alice, self.bob.address, 1, fee=-1)
            with self.assertRaises(ConsensusNodeError):
                node.create_transaction(self.alice, "bad", 1)
            with self.assertRaises(ConsensusNodeError):
                node.create_transaction(self.alice, self.bob.address, 6)
            node.close()

    def test_node_persists_and_prunes_mempool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 25}, difficulty=0)
            valid = node.create_transaction(self.alice, self.bob.address, amount=7, fee=1)
            node.submit_transaction(valid)
            node.close()

            invalid = ConsensusTransaction(
                inputs=(TxInput(OutPoint("b" * 64, 0)),),
                outputs=(TxOutput(1, self.bob.address),),
            )
            mempool_store = SQLiteMempoolStore(ConsensusNode._mempool_path(path))
            mempool_store.add_transaction(invalid)
            mempool_store.close()

            reopened = ConsensusNode.open(path, difficulty=0)
            self.assertEqual([transaction.txid for transaction in reopened.mempool], [valid.txid])
            self.assertIsNotNone(reopened.mempool_store)
            assert reopened.mempool_store is not None
            self.assertEqual(reopened.mempool_store.count(), 1)
            reopened.mine_block(self.alice.address)
            reopened.close()

            mined = ConsensusNode.open(path, difficulty=0)
            self.assertEqual(mined.mempool, [])
            self.assertIsNotNone(mined.mempool_store)
            assert mined.mempool_store is not None
            self.assertEqual(mined.mempool_store.count(), 0)
            mined.close()

    def test_node_reports_and_clears_invalid_transient_mempool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 10}, difficulty=0)
            invalid = ConsensusTransaction(
                inputs=(TxInput(OutPoint("c" * 64, 0)),),
                outputs=(TxOutput(1, self.bob.address),),
            )
            transient = ConsensusNode(node.store, node.state, difficulty=0, mempool=[invalid])
            summary = transient.mempool_summary()
            self.assertEqual(summary[0]["txid"], invalid.txid)
            self.assertIsNone(summary[0]["fee"])
            self.assertFalse(summary[0]["valid"])
            self.assertEqual(transient.prune_mempool(), 1)
            self.assertEqual(transient.clear_mempool(), 0)
            transient.mempool = [invalid]
            with self.assertRaises(ConsensusNodeError):
                transient.build_candidate_block(self.alice.address)
            node.close()

    def test_candidate_block_prioritizes_fee_rate_without_breaking_dependencies(self) -> None:
        carol = Wallet.create()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path,
                {
                    self.alice.address: 100,
                    self.bob.address: 100,
                    carol.address: 100,
                },
                difficulty=0,
            )

            low_fee = node.create_transaction(self.alice, carol.address, amount=10, fee=1)
            high_fee = node.create_transaction(self.bob, carol.address, amount=10, fee=5)
            node.submit_transaction(high_fee)
            node.submit_transaction(low_fee)
            block = node.build_candidate_block(self.alice.address)
            self.assertEqual(
                [transaction.txid for transaction in block.transactions[1:]],
                [high_fee.txid, low_fee.txid],
            )

            node.clear_mempool()
            parent = node.create_transaction(self.alice, self.bob.address, amount=10, fee=1)
            child = ConsensusTransaction(
                inputs=(TxInput(OutPoint(parent.txid, 0)),),
                outputs=(TxOutput(5, self.alice.address),),
            ).sign_input(0, self.bob)
            node.submit_transaction(parent)
            node.submit_transaction(child)
            dependent_block = node.build_candidate_block(self.alice.address)
            self.assertEqual(
                [transaction.txid for transaction in dependent_block.transactions[1:]],
                [parent.txid, child.txid],
            )
            node.close()

    def test_mempool_policy_limits_admission_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path, {self.alice.address: 40, self.bob.address: 40}, difficulty=0
            )
            node.mempool_policy = MempoolPolicy(max_transactions=1)
            alice_tx = node.create_transaction(self.alice, self.bob.address, amount=5, fee=1)
            bob_tx = node.create_transaction(self.bob, self.alice.address, amount=5, fee=1)
            node.submit_transaction(alice_tx)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(bob_tx)
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 40}, difficulty=0)
            node.mempool_policy = MempoolPolicy(min_relay_fee=2)
            low_fee = node.create_transaction(self.alice, self.bob.address, amount=5, fee=1)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(low_fee)
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 1_000}, difficulty=0)
            low_fee = node.create_transaction(self.alice, self.bob.address, amount=5, fee=1)
            required_fee = MempoolPolicy.serialized_size(low_fee) + 1
            node.mempool_policy = MempoolPolicy(min_relay_fee_rate=1000)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(low_fee)
            high_fee = node.create_transaction(
                self.alice,
                self.bob.address,
                amount=5,
                fee=required_fee,
            )
            self.assertEqual(node.submit_transaction(high_fee), high_fee.txid)
            self.assertEqual(
                node.mempool_policy.minimum_fee(MempoolPolicy.serialized_size(high_fee)),
                MempoolPolicy.serialized_size(high_fee),
            )
            with self.assertRaises(MempoolPolicyError):
                node.mempool_policy.minimum_fee(-1)
            node.mempool = [low_fee]
            self.assertEqual(node.prune_mempool(), 1)
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path,
                {
                    self.alice.address: 1_000,
                    self.bob.address: 1_000,
                },
                difficulty=0,
            )
            node.mempool_policy = MempoolPolicy(dynamic_fee_rate_step=1000)
            first = node.create_transaction(self.alice, self.bob.address, amount=5, fee=1)
            second = node.create_transaction(self.bob, self.alice.address, amount=5, fee=1)
            required_fee = MempoolPolicy.serialized_size(second)
            expensive_second = node.create_transaction(
                self.bob,
                self.alice.address,
                amount=5,
                fee=required_fee,
            )
            self.assertEqual(node.submit_transaction(first), first.txid)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(second)
            self.assertEqual(node.submit_transaction(expensive_second), expensive_second.txid)
            self.assertEqual(node.mempool_policy.effective_min_relay_fee_rate(2), 2000)
            with self.assertRaises(MempoolPolicyError):
                node.mempool_policy.effective_min_relay_fee_rate(-1)
            node.mempool = [first, second]
            self.assertEqual(node.prune_mempool(), 1)
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 40}, difficulty=0)
            node.mempool_policy = MempoolPolicy(max_outputs=1)
            two_output = node.create_transaction(self.alice, self.bob.address, amount=5, fee=1)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(two_output)
            node.mempool_policy = MempoolPolicy(max_transaction_bytes=1)
            exact_output = node.create_transaction(self.alice, self.bob.address, amount=39, fee=1)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(exact_output)
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path, {self.alice.address: 40}, difficulty=0, block_subsidy=5
            )
            node.mine_block(self.alice.address)
            chain_blocks = node.active_chain_hashes()
            genesis = node.store.get_block(chain_blocks[0]).block.transactions[0]
            subsidy = node.store.get_block(chain_blocks[1]).block.transactions[0]
            two_input = ConsensusTransaction(
                inputs=(
                    TxInput(OutPoint(genesis.txid, 0)),
                    TxInput(OutPoint(subsidy.txid, 0)),
                ),
                outputs=(TxOutput(44, self.bob.address),),
            ).sign_input(0, self.alice)
            two_input = two_input.sign_input(1, self.alice)
            node.mempool_policy = MempoolPolicy(max_inputs=1)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(two_input)
            node.mempool = [two_input]
            self.assertEqual(node.prune_mempool(), 1)
            node.close()

    def test_mempool_eviction_prefers_higher_fee_rate_and_preserves_dependencies(self) -> None:
        carol = Wallet.create()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path,
                {
                    self.alice.address: 100,
                    self.bob.address: 100,
                    carol.address: 100,
                },
                difficulty=0,
            )
            node.mempool_policy = MempoolPolicy(max_transactions=2)
            low_fee = node.create_transaction(self.alice, carol.address, amount=10, fee=1)
            medium_fee = node.create_transaction(self.bob, carol.address, amount=10, fee=2)
            high_fee = node.create_transaction(carol, self.alice.address, amount=10, fee=6)

            node.submit_transaction(low_fee)
            node.submit_transaction(medium_fee)
            self.assertEqual(node.submit_transaction(high_fee), high_fee.txid)
            self.assertEqual(
                [transaction.txid for transaction in node.mempool],
                [medium_fee.txid, high_fee.txid],
            )
            self.assertIsNotNone(node.mempool_store)
            assert node.mempool_store is not None
            self.assertEqual(
                [transaction.txid for transaction in node.mempool_store.transactions()],
                sorted([medium_fee.txid, high_fee.txid]),
            )
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path,
                {
                    self.alice.address: 100,
                    self.bob.address: 100,
                    carol.address: 100,
                },
                difficulty=0,
            )
            node.mempool_policy = MempoolPolicy(max_transactions=2, allow_eviction=False)
            first = node.create_transaction(self.alice, carol.address, amount=10, fee=1)
            second = node.create_transaction(self.bob, carol.address, amount=10, fee=2)
            node.submit_transaction(first)
            node.submit_transaction(second)
            better = node.create_transaction(carol, self.alice.address, amount=10, fee=3)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(better)
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path,
                {
                    self.alice.address: 100,
                    self.bob.address: 100,
                    carol.address: 100,
                },
                difficulty=0,
            )
            node.mempool_policy = MempoolPolicy(max_transactions=2, eviction_fee_rate_bump=10_000)
            first = node.create_transaction(self.alice, carol.address, amount=10, fee=1)
            second = node.create_transaction(self.bob, carol.address, amount=10, fee=2)
            better = node.create_transaction(carol, self.alice.address, amount=10, fee=3)
            node.submit_transaction(first)
            node.submit_transaction(second)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(better)
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(
                path,
                {
                    self.alice.address: 100,
                    self.bob.address: 100,
                },
                difficulty=0,
            )
            node.mempool_policy = MempoolPolicy(max_transactions=2)
            parent = node.create_transaction(self.alice, self.bob.address, amount=10, fee=1)
            child = ConsensusTransaction(
                inputs=(TxInput(OutPoint(parent.txid, 0)),),
                outputs=(TxOutput(5, self.alice.address),),
            ).sign_input(0, self.bob)
            challenger = node.create_transaction(self.bob, self.alice.address, amount=10, fee=8)
            node.submit_transaction(parent)
            node.submit_transaction(child)
            node.submit_transaction(challenger)
            self.assertEqual(
                [transaction.txid for transaction in node.mempool],
                [parent.txid, challenger.txid],
            )
            node.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 100}, difficulty=0)
            node.mempool_policy = MempoolPolicy(max_transactions=2)
            parent = node.create_transaction(self.alice, self.bob.address, amount=10, fee=1)
            child = ConsensusTransaction(
                inputs=(TxInput(OutPoint(parent.txid, 0)),),
                outputs=(TxOutput(6, self.alice.address),),
            ).sign_input(0, self.bob)
            grandchild = ConsensusTransaction(
                inputs=(TxInput(OutPoint(child.txid, 0)),),
                outputs=(TxOutput(3, self.bob.address),),
            ).sign_input(0, self.alice)
            node.submit_transaction(parent)
            node.submit_transaction(child)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(grandchild)
            self.assertEqual(
                [transaction.txid for transaction in node.mempool],
                [parent.txid, child.txid],
            )
            node.close()

    def test_mempool_replacement_policy_requires_fee_bump_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 40}, difficulty=0)
            original = node.create_transaction(self.alice, self.bob.address, amount=5, fee=1)
            low_fee_replacement = node.create_transaction(
                self.alice,
                self.bob.address,
                amount=6,
                fee=1,
            )
            high_fee_replacement = node.create_transaction(
                self.alice,
                self.bob.address,
                amount=7,
                fee=2,
            )

            node.submit_transaction(original)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(low_fee_replacement)
            self.assertEqual([transaction.txid for transaction in node.mempool], [original.txid])

            self.assertEqual(
                node.submit_transaction(high_fee_replacement), high_fee_replacement.txid
            )
            self.assertEqual(
                [transaction.txid for transaction in node.mempool],
                [high_fee_replacement.txid],
            )
            self.assertIsNotNone(node.mempool_store)
            assert node.mempool_store is not None
            self.assertEqual(node.mempool_store.count(), 1)
            node.close()

            reopened = ConsensusNode.open(path, difficulty=0)
            self.assertEqual(
                [transaction.txid for transaction in reopened.mempool],
                [high_fee_replacement.txid],
            )
            reopened.mine_block(self.alice.address)
            self.assertEqual(
                sum(utxo.output.amount for utxo in reopened.spendable_outputs(self.bob.address)),
                7,
            )
            reopened.close()

    def test_mempool_replacement_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 40}, difficulty=0)
            node.mempool_policy = MempoolPolicy(allow_replacement=False)
            original = node.create_transaction(self.alice, self.bob.address, amount=5, fee=1)
            replacement = node.create_transaction(self.alice, self.bob.address, amount=7, fee=2)

            node.submit_transaction(original)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(replacement)
            self.assertEqual([transaction.txid for transaction in node.mempool], [original.txid])
            node.close()

    def test_mempool_policy_constructor_validation(self) -> None:
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(max_transactions=0)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(max_transaction_bytes=0)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(max_inputs=0)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(max_outputs=0)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(min_relay_fee=-1)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(min_relay_fee_rate=-1)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(dynamic_fee_rate_step=-1)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(replacement_fee_bump=-1)
        with self.assertRaises(MempoolPolicyError):
            MempoolPolicy(eviction_fee_rate_bump=-1)
        policy = MempoolPolicy()
        with self.assertRaises(MempoolPolicyError):
            policy.fee_rate(-1, 1)
        with self.assertRaises(MempoolPolicyError):
            policy.fee_rate(0, 0)

    def test_node_rejects_invalid_create_and_mempool_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.create(path, {}, difficulty=0)
            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.create(path, {"bad": 1}, difficulty=0)
            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.create(path, {self.alice.address: 0}, difficulty=0)
            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.create(path, {self.alice.address: MAX_MONEY + 1}, difficulty=0)
            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.create(path, {self.alice.address: 1}, difficulty=-1)

            node = ConsensusNode.create(path, {self.alice.address: 10}, difficulty=0)
            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.create(path, {self.alice.address: 10}, difficulty=0)
            with self.assertRaises(ConsensusNodeError):
                ConsensusNode(node.store, node.state, difficulty=-1)
            with self.assertRaises(ConsensusNodeError):
                node.build_candidate_block("bad")
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(node.state.create_coinbase(self.alice.address))

            transient_node = ConsensusNode(node.store, node.state, difficulty=0, mempool=[])
            tip = node.store.best_tip()
            self.assertIsNotNone(tip)
            assert tip is not None
            transient_tx = ConsensusTransaction(
                inputs=(TxInput(OutPoint(tip.block.transactions[0].txid, 0)),),
                outputs=(TxOutput(9, self.bob.address),),
            ).sign_input(0, self.alice)
            self.assertEqual(transient_node.submit_transaction(transient_tx), transient_tx.txid)

            tx = ConsensusTransaction(
                inputs=(TxInput(OutPoint(tip.block.transactions[0].txid, 0)),),
                outputs=(TxOutput(9, self.bob.address),),
            ).sign_input(0, self.alice)
            node.submit_transaction(tx)
            with self.assertRaises(MempoolPolicyError):
                node.submit_transaction(tx)
            with self.assertRaises(ConsensusError):
                node.submit_transaction(
                    ConsensusTransaction(
                        inputs=(TxInput(OutPoint("a" * 64, 0)),),
                        outputs=(TxOutput(1, self.bob.address),),
                    )
                )
            uncached_node = ConsensusNode(node.store, node.state, difficulty=0, mempool=[])
            with self.assertRaises(ConsensusNodeError):
                uncached_node.import_block(
                    ConsensusBlock(
                        1,
                        "f" * 64,
                        (node.state.create_coinbase(self.alice.address, height=1),),
                        0,
                    )
                )
            node.close()

            empty_store = SQLiteForkStore(Path(tmp) / "empty.sqlite3")
            empty_state = ConsensusState()
            empty_node = ConsensusNode(empty_store, empty_state, difficulty=0)
            genesis = ConsensusNode._build_genesis_block({self.alice.address: 1}, difficulty=0)
            empty_node.import_block(genesis)
            empty_node.close()

            empty_open_path = Path(tmp) / "empty-open.sqlite3"
            SQLiteForkStore(empty_open_path).close()
            opened_empty = ConsensusNode.open(empty_open_path, difficulty=0)
            self.assertEqual(opened_empty.height, -1)
            self.assertEqual(opened_empty.state.utxos.total(), 0)
            opened_empty.close()

    def test_node_imports_external_block_and_rejects_invalid_or_corrupt_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 10}, difficulty=0)
            block = node.build_candidate_block(self.alice.address)
            imported = node.import_block(block)
            self.assertEqual(imported.block.height, 1)

            with self.assertRaises(ConsensusNodeError):
                node.import_block(ConsensusBlock(99, "bad", block.transactions, 0))
            with self.assertRaises(ConsensusNodeError):
                node.import_block(
                    ConsensusNode._build_genesis_block(
                        {self.bob.address: 1},
                        difficulty=0,
                    )
                )
            invalid = ConsensusBlock(
                1,
                "f" * 64,
                (node.state.create_coinbase(self.alice.address, height=1),),
                difficulty=0,
            )
            with self.assertRaises(ConsensusNodeError):
                node.import_block(invalid)
            self.assertIsNotNone(node.invalid_blocks)
            assert node.invalid_blocks is not None
            self.assertTrue(node.invalid_blocks.is_invalid(invalid.hash))
            self.assertIn("parent", node.invalid_blocks.reason(invalid.hash) or "")
            node.close()

            reopened_invalid = ConsensusNode.open(path, difficulty=0)
            self.assertIsNotNone(reopened_invalid.invalid_blocks)
            assert reopened_invalid.invalid_blocks is not None
            self.assertTrue(reopened_invalid.invalid_blocks.is_invalid(invalid.hash))
            with self.assertRaisesRegex(ConsensusNodeError, "cached as invalid"):
                reopened_invalid.import_block(invalid)
            reopened_invalid.close()

            connection = sqlite3.connect(path)
            with connection:
                row = connection.execute("SELECT data FROM fork_blocks WHERE height = 0").fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                stored_genesis = ConsensusBlock.from_bytes(bytes.fromhex(str(row[0])))
                corrupted_genesis = ConsensusBlock(
                    height=stored_genesis.height,
                    previous_hash="f" * 64,
                    transactions=stored_genesis.transactions,
                    difficulty=stored_genesis.difficulty,
                    timestamp=stored_genesis.timestamp,
                    nonce=stored_genesis.nonce,
                    version=stored_genesis.version,
                )
                connection.execute(
                    "UPDATE fork_blocks SET data = ? WHERE height = 0",
                    (corrupted_genesis.to_bytes().hex(),),
                )
            connection.close()

            with self.assertRaises(ConsensusNodeError):
                ConsensusNode.open(path, difficulty=0)

    def test_node_mining_proof_of_work_and_store_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {self.alice.address: 10}, difficulty=1)
            block = node.build_candidate_block(self.alice.address)
            mined = ConsensusNode._mine(block)
            self.assertTrue(mined.hash.startswith("0"))
            node.close()

            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()
            with self.assertRaises(StorageError):
                SQLiteForkStore(path)


if __name__ == "__main__":
    unittest.main()
