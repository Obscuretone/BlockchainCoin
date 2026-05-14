import unittest

from blockchaincoin import ConsensusNode, Wallet
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
    version_message,
)
from blockchaincoin.peer import PeerError, PeerSession


class PeerSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wallet = Wallet.create()
        self.block = ConsensusNode._build_genesis_block({self.wallet.address: 10}, difficulty=0)
        self.transaction = self.block.transactions[0]

    def test_handshake_completes_between_two_peers(self) -> None:
        alice = PeerSession(network="regtest", node_id="alice", height=1)
        bob = PeerSession(network="regtest", node_id="bob", height=2)

        alice_version = alice.start_handshake()
        bob_responses = bob.receive(alice_version)
        self.assertEqual(
            [message.message_type for message in bob_responses],
            [MessageType.VERSION, MessageType.VERACK],
        )

        alice_responses = []
        for message in bob_responses:
            alice_responses.extend(alice.receive(message))
        self.assertEqual(
            [message.message_type for message in alice_responses], [MessageType.VERACK]
        )

        for message in alice_responses:
            bob.receive(message)

        self.assertTrue(alice.handshake_complete)
        self.assertTrue(bob.handshake_complete)
        self.assertEqual(alice.remote_node_id, "bob")
        self.assertEqual(bob.remote_height, 1)

    def test_handshake_rejects_bad_order_network_and_self_connection(self) -> None:
        session = PeerSession(network="regtest", node_id="alice")

        with self.assertRaises(PeerError):
            session.send_verack()
        responses = session.receive(PeerMessage(MessageType.VERACK, {}))
        self.assertEqual(responses[0].message_type, MessageType.REJECT)
        self.assertEqual(session.misbehavior_score, 1)

        wrong_network = session.receive(version_message("mainnet", 0, "bob"))
        self.assertEqual(wrong_network[0].message_type, MessageType.REJECT)

        self_connection = session.receive(version_message("regtest", 0, "alice"))
        self.assertEqual(self_connection[0].message_type, MessageType.REJECT)
        self.assertEqual(session.misbehavior_score, 3)

        outbound = PeerSession(network="regtest", node_id="alice")
        outbound.start_handshake()
        responses = outbound.receive(version_message("regtest", 3, "bob"))
        self.assertEqual([message.message_type for message in responses], [MessageType.VERACK])
        repeated = outbound.receive(version_message("regtest", 4, "bob"))
        self.assertEqual(repeated, [])

    def test_ping_pong_liveness(self) -> None:
        session = PeerSession(network="regtest", node_id="alice")
        ping = session.send_ping(7)
        self.assertIn(7, session.pending_pings)

        response = session.receive(ping)
        self.assertEqual(response, [PeerMessage(MessageType.PONG, {"nonce": 7})])
        self.assertEqual(session.receive(response[0]), [])
        self.assertNotIn(7, session.pending_pings)

        bad_pong = session.receive(PeerMessage(MessageType.PONG, {"nonce": 99}))
        self.assertEqual(bad_pong[0].message_type, MessageType.REJECT)

        with self.assertRaises(PeerError):
            session.send_ping(-1)

    def test_inventory_announcement_and_requests(self) -> None:
        session = PeerSession(network="regtest", node_id="alice")
        item = InventoryVector("block", self.block.hash)

        announcement = session.announce([item])
        self.assertIn(("block", self.block.hash), session.known_inventory)
        self.assertEqual(announcement.message_type, MessageType.INV)

        other = PeerSession(network="regtest", node_id="bob")
        request = other.receive(announcement)
        self.assertEqual(request[0].message_type, MessageType.GETDATA)
        self.assertIn(("block", self.block.hash), other.requested_inventory)

        duplicate = other.receive(announcement)
        self.assertEqual(duplicate, [])

        other.receive(request[0])
        self.assertIn(("block", self.block.hash), other.requested_inventory)

        other.receive(getheaders_message([self.block.hash]))
        self.assertIn(("block_header", self.block.hash), other.requested_inventory)

        other.receive(headers_message([BlockHeader.from_block(self.block)]))
        self.assertIn(("block_header", self.block.hash), other.known_inventory)

    def test_payload_and_reject_tracking(self) -> None:
        session = PeerSession(network="regtest", node_id="alice")
        session.receive(transaction_message(self.transaction))
        self.assertIn(("tx", self.transaction.txid), session.known_inventory)
        session.receive(PeerMessage(MessageType.TX, {"transaction": self.transaction.to_dict()}))

        session.receive(block_message(self.block))
        self.assertIn(("block", self.block.hash), session.known_inventory)
        session.receive(PeerMessage(MessageType.BLOCK, {"block": self.block.to_dict()}))

        session.receive(PeerMessage(MessageType.REJECT, {"reason": "nope"}))
        self.assertEqual(session.misbehavior_score, 1)

    def test_invalid_message_validation_is_rejected(self) -> None:
        session = PeerSession(network="regtest", node_id="alice")
        response = session.receive(PeerMessage(MessageType.PING, {"nonce": -1}))
        self.assertEqual(response[0].message_type, MessageType.REJECT)
        self.assertEqual(session.misbehavior_score, 1)

    def test_inventory_tracking_is_bounded(self) -> None:
        session = PeerSession(network="regtest", node_id="alice", max_inventory_items=0)
        item = InventoryVector("tx", self.transaction.txid)

        self.assertEqual(
            session.receive(inventory_message(MessageType.INV, [item]))[0].message_type,
            MessageType.REJECT,
        )
        self.assertEqual(
            session.receive(inventory_message(MessageType.GETDATA, [item]))[0].message_type,
            MessageType.REJECT,
        )
        self.assertEqual(
            session.receive(headers_message([BlockHeader.from_block(self.block)]))[0].message_type,
            MessageType.REJECT,
        )


if __name__ == "__main__":
    unittest.main()
