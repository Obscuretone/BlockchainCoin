import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blockchaincoin import Wallet
from blockchaincoin.network import MessageType, PeerMessage
from blockchaincoin.runtime import NodeRuntime, NodeRuntimeConfig, RuntimeError
from blockchaincoin.transport import TCPPeerClient

AUTH_KEY = b"test-peer-auth-key"


class NodeRuntimeTests(unittest.TestCase):
    def test_runtime_create_start_tcp_and_reopen(self) -> None:
        wallet = Wallet.create()
        with tempfile.TemporaryDirectory() as tmp:
            config = NodeRuntimeConfig(
                db_path=Path(tmp) / "node.sqlite3",
                network="regtest",
                node_id="node-a",
                peer_auth_key=AUTH_KEY,
                difficulty=0,
            )
            runtime = NodeRuntime.create(config, {wallet.address: 10})
            runtime.start()
            host, port = runtime.address
            client = TCPPeerClient(host, port, AUTH_KEY)
            try:
                client.connect()
                client.send(PeerMessage(MessageType.PING, {"nonce": 11}))
                self.assertEqual(
                    client.recv_messages(),
                    [PeerMessage(MessageType.PONG, {"nonce": 11})],
                )
            finally:
                client.close()
                runtime.stop()

            reopened = NodeRuntime.open(config)
            self.assertEqual(reopened.node.height, 0)
            reopened.mine(wallet)
            self.assertEqual(reopened.node.height, 1)
            reopened.stop()

    def test_runtime_lifecycle_and_validation_errors(self) -> None:
        wallet = Wallet.create()
        with tempfile.TemporaryDirectory() as tmp:
            config = NodeRuntimeConfig(
                db_path=Path(tmp) / "node.sqlite3",
                network="regtest",
                node_id="node-a",
                peer_auth_key=AUTH_KEY,
            )
            runtime = NodeRuntime.create(config, {wallet.address: 1})
            runtime.start()
            with self.assertRaises(RuntimeError):
                runtime.start()
            runtime.stop()
            runtime.stop()

        invalid_configs = [
            NodeRuntimeConfig(Path("x"), "", "node", peer_auth_key=AUTH_KEY),
            NodeRuntimeConfig(Path("x"), "regtest", "", peer_auth_key=AUTH_KEY),
            NodeRuntimeConfig(Path("x"), "regtest", "node", host="", peer_auth_key=AUTH_KEY),
            NodeRuntimeConfig(Path("x"), "regtest", "node", port=-1, peer_auth_key=AUTH_KEY),
            NodeRuntimeConfig(Path("x"), "regtest", "node", port=65536, peer_auth_key=AUTH_KEY),
            NodeRuntimeConfig(Path("x"), "regtest", "node"),
            NodeRuntimeConfig(Path("x"), "regtest", "node", difficulty=-1, peer_auth_key=AUTH_KEY),
            NodeRuntimeConfig(
                Path("x"), "regtest", "node", block_subsidy=-1, peer_auth_key=AUTH_KEY
            ),
            NodeRuntimeConfig(Path("x"), "regtest", "node", max_money=0, peer_auth_key=AUTH_KEY),
        ]
        for config in invalid_configs:
            with self.assertRaises(RuntimeError):
                config.validate()

    def test_runtime_closes_node_when_server_construction_fails(self) -> None:
        wallet = Wallet.create()
        with tempfile.TemporaryDirectory() as tmp:
            config = NodeRuntimeConfig(
                db_path=Path(tmp) / "node.sqlite3",
                network="regtest",
                node_id="node-a",
                peer_auth_key=AUTH_KEY,
            )
            with (
                patch(
                    "blockchaincoin.runtime.NodeTCPServerAdapter",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertRaises(RuntimeError),
            ):
                NodeRuntime.create(config, {wallet.address: 1})

            open_config = NodeRuntimeConfig(
                db_path=Path(tmp) / "open-node.sqlite3",
                network="regtest",
                node_id="node-a",
                peer_auth_key=AUTH_KEY,
            )
            runtime = NodeRuntime.create(open_config, {wallet.address: 1})
            runtime.stop()
            with (
                patch(
                    "blockchaincoin.runtime.NodeTCPServerAdapter",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertRaises(RuntimeError),
            ):
                NodeRuntime.open(open_config)


if __name__ == "__main__":
    unittest.main()
