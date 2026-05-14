import socket
import tempfile
import unittest
from pathlib import Path
from typing import cast

from blockchaincoin import ConsensusNode, Wallet
from blockchaincoin.consensus import ConsensusTransaction, OutPoint, TxInput, TxOutput
from blockchaincoin.network import (
    BlockHeader,
    InventoryVector,
    MessageType,
    NetworkError,
    PeerMessage,
    block_message,
    headers_message,
    inventory_message,
    transaction_message,
)
from blockchaincoin.peers import PeerAddressManager
from blockchaincoin.service import BlockDownload, NodeService
from blockchaincoin.transport import (
    ConnectionBuffer,
    NodeTCPServerAdapter,
    TCPPeerClient,
    TransportError,
)

AUTH_KEY = b"test-peer-auth-key"


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wallet = Wallet.create()
        self.node = ConsensusNode.create(
            Path(self.tmp.name) / "node.sqlite3",
            {self.wallet.address: 10},
            difficulty=0,
        )
        self.service = NodeService(self.node, "regtest", "node-a")

    def tearDown(self) -> None:
        self.node.close()
        self.tmp.cleanup()

    def spend_from_genesis(self) -> ConsensusTransaction:
        tip = self.node.store.best_tip()
        self.assertIsNotNone(tip)
        assert tip is not None
        genesis_tx = tip.block.transactions[0]
        return ConsensusTransaction(
            inputs=(TxInput(OutPoint(genesis_tx.txid, 0)),),
            outputs=(TxOutput(9, self.wallet.address),),
        ).sign_input(0, self.wallet)

    def test_connection_buffer_handles_partial_and_multiple_frames(self) -> None:
        buffer = ConnectionBuffer(AUTH_KEY)
        first = PeerMessage(MessageType.PING, {"nonce": 1}).to_frame(AUTH_KEY)
        second = PeerMessage(MessageType.PONG, {"nonce": 1}).to_frame(AUTH_KEY)

        self.assertEqual(buffer.feed(first[:3]), [])
        self.assertEqual(buffer.feed(first[3:]), [PeerMessage(MessageType.PING, {"nonce": 1})])
        self.assertEqual(buffer.feed(second[:-1]), [])
        self.assertEqual(buffer.feed(second[-1:]), [PeerMessage(MessageType.PONG, {"nonce": 1})])
        self.assertEqual(
            buffer.feed(first + second),
            [
                PeerMessage(MessageType.PING, {"nonce": 1}),
                PeerMessage(MessageType.PONG, {"nonce": 1}),
            ],
        )
        self.assertEqual(buffer.feed(b""), [])

    def test_connection_buffer_rejects_bad_frames(self) -> None:
        buffer = ConnectionBuffer(AUTH_KEY, b"BAD!")
        with self.assertRaises(NetworkError):
            buffer.feed(b"\x00\x00\x00\x00")

        huge = ConnectionBuffer(AUTH_KEY, b"BCCN" + (2_000_001).to_bytes(4, "big"))
        with self.assertRaises(NetworkError):
            huge.feed(b"")

    def test_tcp_server_client_roundtrip(self) -> None:
        server = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        server.start()
        host, port = server.address
        client = TCPPeerClient(host, port, AUTH_KEY)
        try:
            client.connect()
            client.send(PeerMessage(MessageType.PING, {"nonce": 42}))
            messages = client.recv_messages()
            self.assertEqual(messages, [PeerMessage(MessageType.PONG, {"nonce": 42})])
        finally:
            client.close()
            server.stop()

    def test_tcp_server_broadcasts_relay_to_other_long_lived_peers(self) -> None:
        server = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        server.start()
        host, port = server.address
        sender = TCPPeerClient(host, port, AUTH_KEY)
        receiver = TCPPeerClient(host, port, AUTH_KEY)
        try:
            sender.connect()
            receiver.connect()
            receiver.send(PeerMessage(MessageType.PING, {"nonce": 1}))
            self.assertEqual(
                receiver.recv_messages(), [PeerMessage(MessageType.PONG, {"nonce": 1})]
            )

            tx = self.spend_from_genesis()
            sender.send(transaction_message(tx))

            relayed = receiver.recv_messages()
            self.assertEqual(relayed[0].message_type, MessageType.INV)
            self.assertEqual(relayed[0].payload["items"], [{"kind": "tx", "hash": tx.txid}])

            block = self.node.build_candidate_block(self.wallet.address)
            sender.send(block_message(block))
            relayed = receiver.recv_messages()
            self.assertEqual(relayed[0].message_type, MessageType.INV)
            self.assertEqual(relayed[0].payload["items"], [{"kind": "block", "hash": block.hash}])
        finally:
            sender.close()
            receiver.close()
            server.stop()

    def test_tcp_server_routes_targeted_getdata_to_selected_peer(self) -> None:
        self.node.mine_block(self.wallet.address)
        remote_block = self.node.build_candidate_block(self.wallet.address)
        remote_header = BlockHeader.from_block(remote_block)
        server = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        server.start()
        host, port = server.address
        sender = TCPPeerClient(host, port, AUTH_KEY)
        receiver = TCPPeerClient(host, port, AUTH_KEY)
        try:
            receiver.connect()
            receiver.send(PeerMessage(MessageType.PING, {"nonce": 1}))
            self.assertEqual(
                receiver.recv_messages(), [PeerMessage(MessageType.PONG, {"nonce": 1})]
            )
            receiver_address = next(iter(self.service.sessions))
            self.service.sessions[receiver_address].known_inventory.add(
                ("block_header", remote_header.hash)
            )

            sender.connect()
            sender.send(PeerMessage(MessageType.PING, {"nonce": 2}))
            self.assertEqual(sender.recv_messages(), [PeerMessage(MessageType.PONG, {"nonce": 2})])
            sender_address = next(
                address for address in self.service.sessions if address != receiver_address
            )
            self.service.in_flight_blocks["c" * 64] = BlockDownload(
                "c" * 64,
                sender_address,
                1,
            )

            sender.send(headers_message([remote_header]))
            targeted = receiver.recv_messages()
            self.assertEqual(targeted[0].message_type, MessageType.GETDATA)
            self.assertEqual(
                targeted[0].payload["items"],
                [{"kind": "block", "hash": remote_header.hash}],
            )
        finally:
            sender.close()
            receiver.close()
            server.stop()

    def test_server_lifecycle_errors(self) -> None:
        with self.assertRaises(TransportError):
            NodeTCPServerAdapter("127.0.0.1", 0, self.service, b"")

        server = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        try:
            server.start()
            with self.assertRaises(TransportError):
                server.start()
        finally:
            server.stop()

        never_started = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        never_started.stop()

    def test_adapter_broadcast_handles_empty_and_stale_connections(self) -> None:
        server = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        left, right = socket.socketpair()
        try:
            server.register_connection("peer-a", left)
            server.broadcast([])
            left.close()
            server.broadcast([PeerMessage(MessageType.PING, {"nonce": 1})])
            self.assertEqual(server._connections, {})
            server.unregister_connection("missing")
        finally:
            left.close()
            right.close()
            server.stop()

    def test_adapter_send_to_targets_one_connection_and_cleans_stale_peer(self) -> None:
        server = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        left, right = socket.socketpair()
        other_left, other_right = socket.socketpair()
        try:
            server.register_connection("peer-a", left)
            server.register_connection("peer-b", other_left)
            message = PeerMessage(MessageType.PING, {"nonce": 7})

            server.send_to("peer-a", [message])
            self.assertEqual(ConnectionBuffer(AUTH_KEY).feed(right.recv(65536)), [message])
            other_right.settimeout(0.01)
            with self.assertRaises(TimeoutError):
                other_right.recv(65536)

            server.send_to("missing", [message])
            server.send_to("peer-a", [])
            left.close()
            server.send_to("peer-a", [message])
            self.assertNotIn("peer-a", server._connections)
        finally:
            right.close()
            other_left.close()
            other_right.close()
            server.stop()

    def test_server_drops_invalid_frame(self) -> None:
        server = NodeTCPServerAdapter("127.0.0.1", 0, self.service, AUTH_KEY)
        server.start()
        host, port = server.address
        sock = socket.create_connection((host, port), timeout=2)
        try:
            sock.sendall(b"BAD!\x00\x00\x00\x00")
            self.assertEqual(sock.recv(1024), b"")
        finally:
            sock.close()
            server.stop()

    def test_server_rejects_over_limit_inbound_peer(self) -> None:
        service = NodeService(
            self.node,
            "regtest",
            "node-a",
            peers=PeerAddressManager(max_inbound=0),
        )
        server = NodeTCPServerAdapter("127.0.0.1", 0, service, AUTH_KEY)
        server.start()
        host, port = server.address
        sock = socket.create_connection((host, port), timeout=2)
        try:
            self.assertEqual(sock.recv(1024), b"")
        finally:
            sock.close()
            server.stop()

    def test_server_closes_connection_when_service_disconnects_peer(self) -> None:
        service = NodeService(
            self.node,
            "regtest",
            "node-a",
            peers=PeerAddressManager(ban_threshold=10),
        )
        server = NodeTCPServerAdapter("127.0.0.1", 0, service, AUTH_KEY)
        server.start()
        host, port = server.address
        sock = socket.create_connection((host, port), timeout=2)
        try:
            sock.sendall(
                inventory_message(MessageType.GETDATA, [InventoryVector("tx", "a" * 64)]).to_frame(
                    AUTH_KEY
                )
            )
            buffer = ConnectionBuffer(AUTH_KEY)
            response: list[PeerMessage] = []
            while not response:
                chunk = sock.recv(65536)
                self.assertNotEqual(chunk, b"")
                response = buffer.feed(chunk)
            self.assertEqual(response[0].message_type, MessageType.REJECT)
            self.assertEqual(sock.recv(1024), b"")
        finally:
            sock.close()
            server.stop()

    def test_client_lifecycle_errors_and_factory(self) -> None:
        with self.assertRaises(TransportError):
            TCPPeerClient("127.0.0.1", 9, b"")

        client = TCPPeerClient("127.0.0.1", 9, AUTH_KEY)
        client.close()
        with self.assertRaises(TransportError):
            client.send(PeerMessage(MessageType.PING, {"nonce": 1}))
        with self.assertRaises(TransportError):
            client.recv_messages()

        left, right = socket.socketpair()

        def factory(address, timeout):
            self.assertEqual(address, ("unused", 0))
            self.assertEqual(timeout, 1.0)
            return left

        client = TCPPeerClient("unused", 0, AUTH_KEY, timeout=1.0, socket_factory=factory)
        try:
            client.connect()
            with self.assertRaises(TransportError):
                client.connect()
            client.send(PeerMessage(MessageType.PING, {"nonce": 3}))
            raw = right.recv(65536)
            self.assertTrue(raw.startswith(b"BCCN"))
            right.sendall(PeerMessage(MessageType.PONG, {"nonce": 3}).to_frame(AUTH_KEY))
            self.assertEqual(client.recv_messages(), [PeerMessage(MessageType.PONG, {"nonce": 3})])
            right.sendall(PeerMessage(MessageType.PONG, {"nonce": 4}).to_frame(AUTH_KEY))
            self.assertEqual(
                client.request(PeerMessage(MessageType.PING, {"nonce": 4})),
                [PeerMessage(MessageType.PONG, {"nonce": 4})],
            )
            right.close()
            self.assertEqual(client.recv_messages(), [])
        finally:
            client.close()

    def test_client_recv_treats_connection_reset_as_eof(self) -> None:
        class ResettingSocket:
            closed = False

            def recv(self, _max_bytes: int) -> bytes:
                raise ConnectionResetError("reset by peer")

            def close(self) -> None:
                self.closed = True

        resetting_socket = ResettingSocket()

        client = TCPPeerClient("unused", 0, AUTH_KEY)
        client.socket = cast(socket.socket, resetting_socket)

        self.assertEqual(client.recv_messages(), [])
        self.assertTrue(resetting_socket.closed)
        self.assertIsNone(client.socket)

    def test_client_recv_treats_empty_read_as_eof(self) -> None:
        class EmptyReadSocket:
            def recv(self, _max_bytes: int) -> bytes:
                return b""

        client = TCPPeerClient("unused", 0, AUTH_KEY)
        client.socket = cast(socket.socket, EmptyReadSocket())

        self.assertEqual(client.recv_messages(), [])
        self.assertIsNotNone(client.socket)


if __name__ == "__main__":
    unittest.main()
