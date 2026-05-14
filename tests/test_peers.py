import sqlite3
import tempfile
import unittest
from pathlib import Path

from blockchaincoin.peers import ConnectionState, PeerAddressManager, PeerManagerError
from blockchaincoin.storage import SQLitePeerSyncStore, StorageError, StoredPeerSyncProgress


class PeerAddressManagerTests(unittest.TestCase):
    def test_add_update_and_lookup_peers(self) -> None:
        manager = PeerAddressManager()
        peer = manager.add_peer("127.0.0.1:8333", now=10)
        self.assertEqual(peer.last_seen, 10)

        same = manager.add_peer("127.0.0.1:8333", now=20, inbound=True)
        self.assertIs(peer, same)
        self.assertEqual(peer.last_seen, 20)
        self.assertTrue(peer.inbound)
        self.assertEqual(manager.get_peer("127.0.0.1:8333"), peer)
        self.assertEqual(manager.all_peers(), (peer,))

        with self.assertRaises(PeerManagerError):
            manager.get_peer("missing")
        with self.assertRaises(PeerManagerError):
            manager.add_peer("")
        with self.assertRaises(PeerManagerError):
            manager.add_peer(" " * 3)
        with self.assertRaises(PeerManagerError):
            manager.add_peer("x" * 256)

    def test_connection_limits_and_state_transitions(self) -> None:
        manager = PeerAddressManager(max_outbound=1, max_inbound=1)

        manager.mark_connecting("out-a", now=1)
        self.assertEqual(manager.outbound_count(), 1)
        with self.assertRaises(PeerManagerError):
            manager.mark_connecting("out-b", now=1)

        manager.mark_connected("out-a", now=2)
        self.assertEqual(manager.get_peer("out-a").failures, 0)
        manager.mark_disconnected("out-a", now=3)
        self.assertEqual(manager.get_peer("out-a").state, ConnectionState.DISCONNECTED)

        manager.mark_connecting("in-a", now=1, inbound=True)
        self.assertEqual(manager.inbound_count(), 1)
        with self.assertRaises(PeerManagerError):
            manager.mark_connecting("in-b", now=1, inbound=True)

    def test_failures_backoff_and_outbound_selection(self) -> None:
        manager = PeerAddressManager(
            max_outbound=2, base_backoff_seconds=10, max_backoff_seconds=25
        )
        manager.add_peer("old", now=1)
        manager.add_peer("new", now=10)
        manager.add_peer("retry", now=20)

        failed = manager.mark_failure("retry", now=100)
        self.assertEqual(failed.next_retry_at, 110)
        failed = manager.mark_failure("retry", now=110)
        self.assertEqual(failed.next_retry_at, 130)
        failed = manager.mark_failure("retry", now=130)
        self.assertEqual(failed.next_retry_at, 155)

        self.assertEqual(
            [peer.address for peer in manager.eligible_outbound(now=109)], ["new", "old"]
        )
        self.assertEqual(
            [peer.address for peer in manager.select_outbound(now=200)], ["retry", "new"]
        )

        manager.mark_connecting("new", now=200)
        self.assertEqual([peer.address for peer in manager.select_outbound(now=200)], ["retry"])
        self.assertEqual(manager.select_outbound(now=200, limit=0), [])

    def test_bans_misbehavior_and_unban_after_time(self) -> None:
        manager = PeerAddressManager(ban_threshold=10, ban_seconds=100)
        manager.add_peer("peer", now=0)

        with self.assertRaises(PeerManagerError):
            manager.report_misbehavior("peer", -1)

        record = manager.report_misbehavior("peer", 5, now=10)
        self.assertEqual(record.state, ConnectionState.DISCONNECTED)
        record = manager.report_misbehavior("peer", 5, now=20)
        self.assertEqual(record.state, ConnectionState.BANNED)
        self.assertTrue(record.is_banned)
        self.assertEqual(record.banned_until, 120)

        with self.assertRaises(PeerManagerError):
            manager.mark_connecting("peer", now=30)
        with self.assertRaises(PeerManagerError):
            manager.mark_connected("peer", now=30)

        manager.mark_failure("peer", now=40)
        self.assertEqual(record.state, ConnectionState.BANNED)
        manager.mark_disconnected("peer", now=40)
        self.assertEqual(record.state, ConnectionState.BANNED)

        manager.mark_connecting("peer", now=121)
        self.assertEqual(record.state, ConnectionState.CONNECTING)
        self.assertFalse(record.is_banned)

    def test_eviction_candidates(self) -> None:
        manager = PeerAddressManager()
        manager.mark_connecting("quiet", now=1)
        manager.mark_connected("quiet", now=1)
        manager.mark_connecting("noisy", now=2)
        manager.mark_connected("noisy", now=2)
        manager.report_misbehavior("noisy", 20, now=3)

        self.assertEqual(
            [peer.address for peer in manager.eviction_candidates()],
            ["noisy", "quiet"],
        )

    def test_constructor_validation_and_direct_ban(self) -> None:
        with self.assertRaises(PeerManagerError):
            PeerAddressManager(max_outbound=-1)
        with self.assertRaises(PeerManagerError):
            PeerAddressManager(max_inbound=-1)
        with self.assertRaises(PeerManagerError):
            PeerAddressManager(ban_threshold=0)

        manager = PeerAddressManager()
        manager.add_peer("peer")
        self.assertEqual(manager.ban_peer("peer", now=5).banned_until, 86405)


class PeerSyncStoreTests(unittest.TestCase):
    def test_peer_sync_progress_persists_and_orders_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peer-sync.sqlite3"
            store = SQLitePeerSyncStore(path)
            self.assertIsNone(store.load_progress("missing"))

            store.save_progress(
                StoredPeerSyncProgress(
                    address="peer-b",
                    requested_blocks=1,
                    completed_blocks=2,
                    timed_out_blocks=3,
                    failed_blocks=4,
                )
            )
            store.save_progress(StoredPeerSyncProgress(address="peer-a", requested_blocks=5))
            store.save_progress(
                StoredPeerSyncProgress(address="peer-b", requested_blocks=6, failed_blocks=7)
            )

            self.assertEqual(
                store.load_progress("peer-b"),
                StoredPeerSyncProgress(address="peer-b", requested_blocks=6, failed_blocks=7),
            )
            self.assertEqual(
                store.all_progress(),
                (
                    StoredPeerSyncProgress(address="peer-a", requested_blocks=5),
                    StoredPeerSyncProgress(address="peer-b", requested_blocks=6, failed_blocks=7),
                ),
            )
            store.close()

            reopened = SQLitePeerSyncStore(path)
            self.assertEqual(
                reopened.load_progress("peer-a"),
                StoredPeerSyncProgress(address="peer-a", requested_blocks=5),
            )
            reopened.close()

    def test_peer_sync_store_rejects_bad_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peer-sync.sqlite3"
            store = SQLitePeerSyncStore(path)
            store.close()
            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
            connection.close()

            with self.assertRaises(StorageError):
                SQLitePeerSyncStore(path)


if __name__ == "__main__":
    unittest.main()
