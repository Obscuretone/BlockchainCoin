import tempfile
import unittest
from pathlib import Path
from typing import cast

from blockchaincoin import ConsensusNode, MempoolPolicy, Wallet
from blockchaincoin.consensus import (
    ConsensusBlock,
    ConsensusTransaction,
    OutPoint,
    TxInput,
    TxOutput,
)
from blockchaincoin.network import (
    BlockHeader,
    InventoryVector,
    MessageType,
    PeerMessage,
    block_message,
    getheaders_message,
    headers_message,
    inventory_message,
    transaction_message,
)
from blockchaincoin.peers import ConnectionState, PeerAddressManager
from blockchaincoin.service import BlockDownload, NodeService, NodeServiceError, PeerSyncProgress
from blockchaincoin.storage import SQLitePeerSyncStore, StoredPeerSyncProgress


class NodeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Wallet.create()
        self.bob = Wallet.create()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "node.sqlite3"
        self.node = ConsensusNode.create(
            self.path, {self.alice.address: 50}, difficulty=0, block_subsidy=5
        )
        self.service = NodeService(self.node, "regtest", "node-a")

    def tearDown(self) -> None:
        self.node.close()
        self.tmp.cleanup()

    def spend_from_genesis(self, amount: int = 10, change: int = 35) -> ConsensusTransaction:
        tip = self.node.store.best_tip()
        self.assertIsNotNone(tip)
        assert tip is not None
        genesis_tx = tip.block.transactions[0]
        return ConsensusTransaction(
            inputs=(TxInput(OutPoint(genesis_tx.txid, 0)),),
            outputs=(
                TxOutput(amount, self.bob.address),
                TxOutput(change, self.alice.address),
            ),
        ).sign_input(0, self.alice)

    def test_connect_disconnect_and_constructor_validation(self) -> None:
        with self.assertRaises(NodeServiceError):
            NodeService(self.node, "", "node")
        with self.assertRaises(NodeServiceError):
            NodeService(self.node, "regtest", "")
        with self.assertRaises(NodeServiceError):
            NodeService(self.node, "regtest", "node", block_download_timeout=-1)
        with self.assertRaises(NodeServiceError):
            NodeService(self.node, "regtest", "node", max_block_download_batch=0)
        with self.assertRaises(NodeServiceError):
            NodeService(self.node, "regtest", "node", max_block_downloads_per_peer=0)

        version = self.service.connect_peer("peer-a", now=1)
        self.assertEqual(version.message_type, MessageType.VERSION)
        self.assertIn("peer-a", self.service.sessions)
        self.service.disconnect_peer("peer-a", now=2)
        self.assertNotIn("peer-a", self.service.sessions)

    def test_peer_sync_progress_quality_score(self) -> None:
        self.assertEqual(PeerSyncProgress().quality_score, 0)
        self.assertEqual(
            PeerSyncProgress(
                requested_blocks=3,
                completed_blocks=2,
                timed_out_blocks=1,
                failed_blocks=1,
            ).quality_score,
            103,
        )
        self.assertEqual(
            PeerSyncProgress(requested_blocks=1, completed_blocks=10).quality_score,
            0,
        )

    def test_submit_local_transaction_and_getdata(self) -> None:
        tx = self.spend_from_genesis()
        result = self.service.submit_local_transaction(tx)
        self.assertEqual(result.relay[0].message_type, MessageType.INV)
        self.assertIn(tx.txid, self.service.transactions)

        getdata = inventory_message(MessageType.GETDATA, [InventoryVector("tx", tx.txid)])
        response = self.service.receive("peer-a", getdata)
        self.assertEqual(response.direct[-1].message_type, MessageType.TX)

    def test_mine_local_block_and_getdata(self) -> None:
        tx = self.spend_from_genesis()
        self.service.submit_local_transaction(tx)
        result = self.service.mine_local_block(self.alice.address)
        items = cast(list[dict[str, object]], result.relay[0].payload["items"])
        block_hash = str(items[0]["hash"])
        self.assertIn(block_hash, self.service.blocks)

        getdata = inventory_message(MessageType.GETDATA, [InventoryVector("block", block_hash)])
        response = self.service.receive("peer-a", getdata)
        self.assertEqual(response.direct[-1].message_type, MessageType.BLOCK)

    def test_getheaders_returns_active_chain_headers_after_locator(self) -> None:
        block_one = self.node.mine_block(self.alice.address).block
        block_two = self.node.mine_block(self.alice.address).block

        response = self.service.receive(
            "peer-a",
            getheaders_message([block_one.hash], limit=10),
        )

        self.assertEqual(response.direct[-1].message_type, MessageType.HEADERS)
        headers = cast(list[dict[str, object]], response.direct[-1].payload["headers"])
        self.assertEqual(headers, [BlockHeader.from_block(block_two).to_dict()])

    def test_getheaders_honors_empty_locator_stop_hash_and_limit(self) -> None:
        genesis = self.node.store.active_chain()[0].block
        block_one = self.node.mine_block(self.alice.address).block
        block_two = self.node.mine_block(self.alice.address).block

        stopped = self.service.receive(
            "peer-a",
            getheaders_message([], stop_hash=block_one.hash, limit=10),
        )
        stopped_headers = cast(list[dict[str, object]], stopped.direct[-1].payload["headers"])
        self.assertEqual(
            stopped_headers,
            [
                BlockHeader.from_block(genesis).to_dict(),
                BlockHeader.from_block(block_one).to_dict(),
            ],
        )

        limited = self.service.receive("peer-a", getheaders_message([], limit=1))
        limited_headers = cast(list[dict[str, object]], limited.direct[-1].payload["headers"])
        self.assertEqual(limited_headers, [BlockHeader.from_block(genesis).to_dict()])

        unknown_locator = self.service.receive(
            "peer-a",
            getheaders_message(["f" * 64], limit=10),
        )
        unknown_headers = cast(
            list[dict[str, object]],
            unknown_locator.direct[-1].payload["headers"],
        )
        self.assertEqual(
            unknown_headers,
            [
                BlockHeader.from_block(genesis).to_dict(),
                BlockHeader.from_block(block_one).to_dict(),
                BlockHeader.from_block(block_two).to_dict(),
            ],
        )

    def test_headers_schedule_missing_block_downloads(self) -> None:
        block_one = self.node.mine_block(self.alice.address).block
        block_two = self.node.mine_block(self.alice.address).block
        remote_header = BlockHeader(
            height=block_two.height + 1,
            previous_hash=block_two.hash,
            transaction_root="a" * 64,
            difficulty=0,
            timestamp=block_two.timestamp + 1,
            nonce=0,
        )

        response = self.service.receive(
            "peer-a",
            headers_message(
                [
                    BlockHeader.from_block(block_one),
                    BlockHeader.from_block(block_two),
                    remote_header,
                ]
            ),
            now=10,
        )

        self.assertEqual(response.direct[-1].message_type, MessageType.GETDATA)
        self.assertEqual(
            response.direct[-1].payload["items"],
            [{"kind": "block", "hash": remote_header.hash}],
        )
        self.assertEqual(
            self.service.in_flight_blocks[remote_header.hash],
            BlockDownload(remote_header.hash, "peer-a", 10),
        )
        self.assertEqual(
            self.service.sync_progress("peer-a"),
            PeerSyncProgress(requested_blocks=1),
        )

        duplicate = self.service.receive("peer-a", headers_message([remote_header]), now=11)
        self.assertEqual(duplicate.direct, [])

        self.service.blocks[remote_header.hash] = ConsensusBlock(
            height=remote_header.height,
            previous_hash=remote_header.previous_hash,
            transactions=(),
            difficulty=remote_header.difficulty,
            timestamp=remote_header.timestamp,
            nonce=remote_header.nonce,
            transaction_root=remote_header.transaction_root,
            version=remote_header.version,
        )
        known_response = self.service.receive("peer-a", headers_message([remote_header]))
        self.assertEqual(known_response.direct, [])

    def test_block_downloads_expire_and_clear_on_block_or_peer_state(self) -> None:
        self.node.mine_block(self.alice.address)
        remote_block = self.node.build_candidate_block(self.alice.address)
        remote_header = BlockHeader.from_block(remote_block)
        service = NodeService(
            self.node,
            "regtest",
            "node-b",
            block_download_timeout=5,
        )

        first = service.receive("peer-a", headers_message([remote_header]), now=10)
        self.assertEqual(first.direct[-1].message_type, MessageType.GETDATA)
        self.assertEqual(
            service.receive("peer-a", headers_message([remote_header]), now=14).direct, []
        )
        self.assertEqual(
            service.expire_block_downloads(15),
            (BlockDownload(remote_header.hash, "peer-a", 10),),
        )
        self.assertEqual(
            service.sync_progress("peer-a"),
            PeerSyncProgress(requested_blocks=1, timed_out_blocks=1),
        )
        retry = service.receive("peer-b", headers_message([remote_header]), now=16)
        self.assertEqual(retry.direct[-1].message_type, MessageType.GETDATA)
        self.assertEqual(
            service.in_flight_blocks[remote_header.hash],
            BlockDownload(remote_header.hash, "peer-b", 16),
        )
        self.assertEqual(service.sync_progress("peer-b"), PeerSyncProgress(requested_blocks=1))

        service.disconnect_peer("peer-b", now=17)
        self.assertEqual(service.in_flight_blocks, {})
        self.assertEqual(
            service.sync_progress("peer-b"),
            PeerSyncProgress(requested_blocks=1, failed_blocks=1),
        )

        service.in_flight_blocks = {
            "c" * 64: BlockDownload("c" * 64, "peer-a", 17),
            "d" * 64: BlockDownload("d" * 64, "peer-c", 17),
        }
        service.disconnect_peer("peer-a", now=17)
        self.assertEqual(
            service.in_flight_blocks,
            {"d" * 64: BlockDownload("d" * 64, "peer-c", 17)},
        )

        service.receive("peer-a", headers_message([remote_header]), now=18)
        service.receive("peer-a", block_message(remote_block), now=19)
        self.assertNotIn(remote_header.hash, service.in_flight_blocks)
        self.assertEqual(
            service.sync_progress("peer-a"),
            PeerSyncProgress(
                requested_blocks=2,
                completed_blocks=1,
                timed_out_blocks=1,
                failed_blocks=1,
            ),
        )

    def test_block_download_peer_selection_prefers_less_busy_known_peer(self) -> None:
        self.node.mine_block(self.alice.address)
        remote_block = self.node.build_candidate_block(self.alice.address)
        remote_header = BlockHeader.from_block(remote_block)
        service = NodeService(self.node, "regtest", "node-b")
        service.connect_peer("peer-a", now=1)
        service.connect_peer("peer-b", now=1)
        service.sessions["peer-b"].known_inventory.add(("block_header", remote_header.hash))
        service.in_flight_blocks["c" * 64] = BlockDownload("c" * 64, "peer-a", 1)

        response = service.receive("peer-a", headers_message([remote_header]), now=2)

        self.assertEqual(response.direct, [])
        self.assertEqual(response.direct_to["peer-b"][0].message_type, MessageType.GETDATA)
        self.assertEqual(
            response.direct_to["peer-b"][0].payload["items"],
            [{"kind": "block", "hash": remote_header.hash}],
        )
        self.assertEqual(
            service.in_flight_blocks[remote_header.hash],
            BlockDownload(remote_header.hash, "peer-b", 2),
        )
        self.assertEqual(service.sync_progress("peer-b"), PeerSyncProgress(requested_blocks=1))

        fallback = service._select_block_download_peer("peer-a", "d" * 64)
        self.assertEqual(fallback, "peer-a")
        service.sessions.clear()
        self.assertEqual(service._select_block_download_peer("peer-a", "d" * 64), "peer-a")

    def test_block_download_peer_selection_respects_per_peer_limit(self) -> None:
        self.node.mine_block(self.alice.address)
        base = self.node.build_candidate_block(self.alice.address)
        headers = [BlockHeader.from_block(base)]
        for index in range(1, 3):
            headers.append(
                BlockHeader(
                    height=headers[-1].height + 1,
                    previous_hash=headers[-1].hash,
                    transaction_root=f"{index:064x}",
                    difficulty=0,
                    timestamp=headers[-1].timestamp + 1,
                    nonce=0,
                )
            )
        service = NodeService(
            self.node,
            "regtest",
            "node-b",
            max_block_downloads_per_peer=1,
        )
        service.connect_peer("peer-a", now=1)
        service.connect_peer("peer-b", now=1)
        for header in headers:
            service.sessions["peer-b"].known_inventory.add(("block_header", header.hash))

        response = service.receive("peer-a", headers_message(headers), now=2)
        direct_items = [
            cast(list[dict[str, object]], message.payload["items"]) for message in response.direct
        ]
        targeted_items = [
            cast(list[dict[str, object]], message.payload["items"])
            for message in response.direct_to["peer-b"]
        ]

        self.assertEqual(
            [items[0]["hash"] for items in direct_items],
            [headers[0].hash],
        )
        self.assertEqual(
            [items[0]["hash"] for items in targeted_items],
            [headers[1].hash],
        )
        self.assertNotIn(headers[2].hash, service.in_flight_blocks)
        self.assertIsNone(service._select_block_download_peer("peer-a", headers[2].hash))

    def test_block_download_scheduling_splits_bounded_batches(self) -> None:
        self.node.mine_block(self.alice.address)
        base = self.node.build_candidate_block(self.alice.address)
        headers = [BlockHeader.from_block(base)]
        for index in range(1, 5):
            headers.append(
                BlockHeader(
                    height=headers[-1].height + 1,
                    previous_hash=headers[-1].hash,
                    transaction_root=f"{index:064x}",
                    difficulty=0,
                    timestamp=headers[-1].timestamp + 1,
                    nonce=0,
                )
            )
        service = NodeService(
            self.node,
            "regtest",
            "node-b",
            max_block_download_batch=2,
        )

        response = service.receive("peer-a", headers_message(headers), now=3)

        self.assertEqual(
            [message.message_type for message in response.direct], [MessageType.GETDATA] * 3
        )
        batches = [
            cast(list[dict[str, object]], message.payload["items"]) for message in response.direct
        ]
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        self.assertEqual(
            [item["hash"] for batch in batches for item in batch],
            [header.hash for header in headers],
        )
        self.assertEqual(service._chunk_inventory([]), ())

    def test_header_download_scheduling_rejects_unanchored_headers(self) -> None:
        remote_header = BlockHeader(
            height=99,
            previous_hash="f" * 64,
            transaction_root="1" * 64,
            difficulty=0,
            timestamp=1,
            nonce=0,
        )

        response = self.service.receive("peer-a", headers_message([remote_header]), now=1)

        self.assertEqual(response.direct[0].message_type, MessageType.REJECT)
        self.assertEqual(self.service.in_flight_blocks, {})

    def test_expired_block_download_retry_prefers_alternate_known_peer(self) -> None:
        self.node.mine_block(self.alice.address)
        remote_block = self.node.build_candidate_block(self.alice.address)
        remote_header = BlockHeader.from_block(remote_block)
        service = NodeService(
            self.node,
            "regtest",
            "node-b",
            block_download_timeout=5,
            max_block_download_batch=1,
        )
        service.connect_peer("peer-a", now=1)
        service.connect_peer("peer-b", now=1)
        service.sessions["peer-b"].known_inventory.add(("block_header", remote_header.hash))
        service.in_flight_blocks[remote_header.hash] = BlockDownload(
            remote_header.hash,
            "peer-a",
            10,
        )

        retry = service.retry_expired_block_downloads(15)

        self.assertEqual(list(retry), ["peer-b"])
        self.assertEqual(retry["peer-b"][0].message_type, MessageType.GETDATA)
        self.assertEqual(
            retry["peer-b"][0].payload["items"],
            [{"kind": "block", "hash": remote_header.hash}],
        )
        self.assertEqual(
            service.in_flight_blocks[remote_header.hash],
            BlockDownload(remote_header.hash, "peer-b", 15),
        )
        self.assertEqual(
            service.sync_progress("peer-a"),
            PeerSyncProgress(timed_out_blocks=1),
        )
        self.assertEqual(service.sync_progress("peer-b"), PeerSyncProgress(requested_blocks=1))

    def test_expired_block_download_retry_fallbacks_and_skips_known_blocks(self) -> None:
        self.node.mine_block(self.alice.address)
        remote_block = self.node.build_candidate_block(self.alice.address)
        remote_header = BlockHeader.from_block(remote_block)
        service = NodeService(
            self.node,
            "regtest",
            "node-b",
            block_download_timeout=5,
        )
        service.connect_peer("peer-a", now=1)
        service.in_flight_blocks[remote_header.hash] = BlockDownload(
            remote_header.hash,
            "peer-a",
            10,
        )

        retry = service.retry_expired_block_downloads(15)

        self.assertEqual(list(retry), ["peer-a"])
        self.assertEqual(
            service.in_flight_blocks[remote_header.hash],
            BlockDownload(remote_header.hash, "peer-a", 15),
        )
        self.assertEqual(
            service.sync_progress("peer-a"),
            PeerSyncProgress(requested_blocks=1, timed_out_blocks=1),
        )

        service.blocks[remote_header.hash] = remote_block
        self.assertEqual(service.retry_expired_block_downloads(20), {})
        self.assertEqual(service.in_flight_blocks, {})

    def test_expired_block_download_retry_defers_when_candidates_are_saturated(self) -> None:
        self.node.mine_block(self.alice.address)
        remote_block = self.node.build_candidate_block(self.alice.address)
        remote_header = BlockHeader.from_block(remote_block)
        service = NodeService(
            self.node,
            "regtest",
            "node-b",
            block_download_timeout=5,
            max_block_downloads_per_peer=1,
        )
        service.connect_peer("peer-a", now=1)
        service.connect_peer("peer-b", now=1)
        service.sessions["peer-b"].known_inventory.add(("block_header", remote_header.hash))
        busy_hash = "c" * 64
        service.in_flight_blocks[remote_header.hash] = BlockDownload(
            remote_header.hash,
            "peer-a",
            10,
        )
        service.in_flight_blocks[busy_hash] = BlockDownload(busy_hash, "peer-b", 14)

        self.assertEqual(service.retry_expired_block_downloads(15), {})
        self.assertEqual(
            service.in_flight_blocks,
            {busy_hash: BlockDownload(busy_hash, "peer-b", 14)},
        )
        self.assertEqual(
            service.sync_progress("peer-a"),
            PeerSyncProgress(timed_out_blocks=1),
        )

    def test_block_download_progress_tracks_ban_cleanup(self) -> None:
        peers = PeerAddressManager(ban_threshold=10)
        service = NodeService(self.node, "regtest", "node-b", peers=peers)
        service.connect_peer("peer-a", now=1)
        service.in_flight_blocks["c" * 64] = BlockDownload("c" * 64, "peer-a", 1)

        response = service.receive("peer-a", PeerMessage(MessageType.PING, {"nonce": -1}), now=2)

        self.assertTrue(response.disconnect)
        self.assertEqual(
            service.sync_progress("peer-a"),
            PeerSyncProgress(failed_blocks=1),
        )

    def test_peer_sync_progress_can_persist_through_store(self) -> None:
        progress_path = Path(self.tmp.name) / "peer-sync.sqlite3"
        store = SQLitePeerSyncStore(progress_path)
        store.save_progress(StoredPeerSyncProgress(address="peer-a", requested_blocks=2))
        service = NodeService(
            self.node,
            "regtest",
            "node-b",
            block_download_timeout=5,
            peer_sync_store=store,
        )

        self.assertEqual(service.sync_progress("peer-a"), PeerSyncProgress(requested_blocks=2))
        service.in_flight_blocks["c" * 64] = BlockDownload("c" * 64, "peer-a", 1)
        service.expire_block_downloads(10)

        self.assertEqual(
            store.load_progress("peer-a"),
            StoredPeerSyncProgress(
                address="peer-a",
                requested_blocks=2,
                timed_out_blocks=1,
            ),
        )
        store.close()

        reopened = SQLitePeerSyncStore(progress_path)
        try:
            reloaded = NodeService(self.node, "regtest", "node-c", peer_sync_store=reopened)
            self.assertEqual(
                reloaded.sync_progress("peer-a"),
                PeerSyncProgress(requested_blocks=2, timed_out_blocks=1),
            )
        finally:
            reopened.close()

    def test_persisted_peer_quality_score_influences_selection(self) -> None:
        progress_path = Path(self.tmp.name) / "peer-quality.sqlite3"
        store = SQLitePeerSyncStore(progress_path)
        store.save_progress(
            StoredPeerSyncProgress(
                address="peer-a",
                requested_blocks=10,
                completed_blocks=10,
            )
        )
        store.save_progress(
            StoredPeerSyncProgress(
                address="peer-b",
                requested_blocks=1,
                timed_out_blocks=3,
            )
        )
        self.node.mine_block(self.alice.address)
        remote_header = BlockHeader.from_block(self.node.build_candidate_block(self.alice.address))
        service = NodeService(self.node, "regtest", "node-b", peer_sync_store=store)
        try:
            service.connect_peer("peer-a", now=1)
            service.connect_peer("peer-b", now=1)
            service.sessions["peer-b"].known_inventory.add(("block_header", remote_header.hash))

            self.assertEqual(
                service._select_block_download_peer("peer-a", remote_header.hash),
                "peer-a",
            )
            self.assertLess(
                service.sync_progress("peer-a").quality_score,
                service.sync_progress("peer-b").quality_score,
            )
        finally:
            store.close()

    def test_receive_peer_transaction_and_duplicate_suppression(self) -> None:
        tx = self.spend_from_genesis()
        result = self.service.receive("peer-a", transaction_message(tx))
        self.assertEqual(result.relay[0].message_type, MessageType.INV)

        duplicate = self.service.receive("peer-a", transaction_message(tx))
        self.assertEqual(duplicate.relay, [])

    def test_receive_peer_block_and_duplicate_suppression(self) -> None:
        block = self.node.build_candidate_block(self.alice.address)
        result = self.service.receive("peer-a", block_message(block))
        self.assertEqual(result.relay[0].message_type, MessageType.INV)

        duplicate = self.service.receive("peer-a", block_message(block))
        self.assertEqual(duplicate.relay, [])

    def test_invalid_messages_raise_misbehavior_and_reject(self) -> None:
        response = self.service.receive(
            "peer-a", PeerMessage(MessageType.PING, {"nonce": -1}), now=1
        )
        self.assertEqual(response.direct[0].message_type, MessageType.REJECT)
        self.assertFalse(response.disconnect)
        self.assertEqual(self.service.peers.get_peer("peer-a").misbehavior_score, 10)

        unknown = inventory_message(MessageType.GETDATA, [InventoryVector("tx", "a" * 64)])
        response = self.service.receive("peer-a", unknown, now=2)
        self.assertEqual(response.direct[-1].message_type, MessageType.REJECT)
        self.assertFalse(response.disconnect)
        self.assertEqual(self.service.peers.get_peer("peer-a").misbehavior_score, 20)

    def test_misbehavior_ban_disconnects_and_rejects_banned_peer(self) -> None:
        peers = PeerAddressManager(ban_threshold=10)
        service = NodeService(self.node, "regtest", "node-b", peers=peers)
        service.connect_peer("peer-a", now=1)

        response = service.receive("peer-a", PeerMessage(MessageType.PING, {"nonce": -1}), now=2)

        self.assertTrue(response.disconnect)
        self.assertEqual(peers.get_peer("peer-a").state, ConnectionState.BANNED)
        self.assertNotIn("peer-a", service.sessions)

        banned = service.receive("peer-a", PeerMessage(MessageType.PING, {"nonce": 1}), now=3)
        self.assertTrue(banned.disconnect)
        self.assertEqual(banned.direct[0].payload["reason"], "peer banned")

    def test_receive_invalid_consensus_payload_rejects(self) -> None:
        bad_tx = ConsensusTransaction((), (TxOutput(1, self.bob.address),))
        response = self.service.receive("peer-a", transaction_message(bad_tx), now=1)
        self.assertEqual(response.direct[-1].message_type, MessageType.REJECT)
        self.assertEqual(self.service.peers.get_peer("peer-a").misbehavior_score, 10)

        bad_block = self.node.build_candidate_block(self.alice.address)
        bad_block = type(bad_block)(
            height=99,
            previous_hash="f" * 64,
            transactions=bad_block.transactions,
            difficulty=0,
        )
        response = self.service.receive("peer-a", block_message(bad_block), now=2)
        self.assertEqual(response.direct[-1].message_type, MessageType.REJECT)
        self.assertEqual(self.service.peers.get_peer("peer-a").misbehavior_score, 20)

    def test_receive_policy_rejected_transaction_does_not_score_misbehavior(self) -> None:
        self.node.mempool_policy = MempoolPolicy(min_relay_fee=2)
        tx = self.spend_from_genesis(amount=10, change=39)

        response = self.service.receive("peer-a", transaction_message(tx), now=1)

        self.assertEqual(response.direct[-1].message_type, MessageType.REJECT)
        self.assertEqual(response.direct[-1].payload["reason"], "policy rejected")
        self.assertEqual(response.relay, [])
        self.assertEqual(self.service.peers.get_peer("peer-a").misbehavior_score, 0)

    def test_service_with_existing_peer_manager(self) -> None:
        peers = PeerAddressManager()
        service = NodeService(self.node, "regtest", "node-b", peers=peers)
        service.receive("peer-x", PeerMessage(MessageType.PING, {"nonce": 1}))
        self.assertEqual(peers.get_peer("peer-x").state.value, "connected")


if __name__ == "__main__":
    unittest.main()
