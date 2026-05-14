import json
import struct
import unittest
from dataclasses import replace

from blockchaincoin import ConsensusNode, Wallet
from blockchaincoin.consensus import ConsensusState
from blockchaincoin.network import (
    AUTH_TAG_SIZE,
    HEADER_SIZE,
    MAX_INVENTORY_ITEMS,
    MAX_MESSAGE_SIZE,
    NETWORK_MAGIC,
    BlockHeader,
    FrameAuthenticator,
    InventoryVector,
    MessageType,
    NetworkError,
    PeerMessage,
    block_from_payload,
    block_message,
    getheaders_message,
    headers_from_payload,
    headers_message,
    inventory_message,
    read_frame,
    transaction_from_payload,
    transaction_message,
    validate_header_chain,
    version_message,
)

AUTH_KEY = b"test-peer-auth-key"


class NetworkProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wallet = Wallet.create()
        self.block = ConsensusNode._build_genesis_block({self.wallet.address: 10}, difficulty=0)
        self.transaction = ConsensusState().create_coinbase(self.wallet.address)

    def test_message_roundtrips_and_checksums(self) -> None:
        messages = [
            version_message("regtest", 1, "node-a"),
            PeerMessage(MessageType.VERACK, {}),
            PeerMessage(MessageType.PING, {"nonce": 1}),
            PeerMessage(MessageType.PONG, {"nonce": 1}),
            inventory_message(
                MessageType.INV,
                [InventoryVector("block", self.block.hash)],
            ),
            inventory_message(
                MessageType.GETDATA,
                [InventoryVector("tx", self.transaction.txid)],
            ),
            getheaders_message([self.block.hash], limit=10),
            headers_message([BlockHeader.from_block(self.block)]),
            transaction_message(self.transaction),
            block_message(self.block),
            PeerMessage(MessageType.REJECT, {"reason": "bad block"}),
        ]

        buffer = b"".join(message.to_frame(AUTH_KEY) for message in messages)
        decoded: list[PeerMessage] = []
        while buffer:
            message, buffer = read_frame(buffer, AUTH_KEY)
            decoded.append(message)

        self.assertEqual(
            [message.message_type for message in decoded],
            [message.message_type for message in messages],
        )
        self.assertEqual(decoded[0].checksum, messages[0].checksum)
        self.assertEqual(PeerMessage.from_dict(messages[0].to_dict()), messages[0])
        self.assertEqual(
            len(messages[0].to_frame(AUTH_KEY)),
            HEADER_SIZE + len(messages[0].encode_payload()) + AUTH_TAG_SIZE,
        )

    def test_authenticated_frames_reject_wrong_keys_and_missing_tags(self) -> None:
        message = PeerMessage(MessageType.PING, {"nonce": 9})
        frame = message.to_frame(AUTH_KEY)

        self.assertEqual(PeerMessage.from_frame(frame, AUTH_KEY), message)
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(frame, b"wrong-key")
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(frame[:-AUTH_TAG_SIZE], AUTH_KEY)
        with self.assertRaises(NetworkError):
            PeerMessage(MessageType.PING, {"nonce": 1}).to_frame(b"")
        with self.assertRaises(NetworkError):
            FrameAuthenticator(AUTH_KEY).verify(frame[:HEADER_SIZE], b"short")
        invalid_json = NETWORK_MAGIC + struct.pack(">I", 1) + b"\xff"
        invalid_json += FrameAuthenticator(AUTH_KEY).tag(invalid_json)
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(invalid_json, AUTH_KEY)
        non_object = NETWORK_MAGIC + struct.pack(">I", 4) + b"true"
        non_object += FrameAuthenticator(AUTH_KEY).tag(non_object)
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(non_object, AUTH_KEY)

    def test_inventory_validation(self) -> None:
        vector = InventoryVector("tx", "a" * 64)
        self.assertEqual(InventoryVector.from_dict(vector.to_dict()), vector)

        with self.assertRaises(NetworkError):
            InventoryVector.from_dict({"kind": "address", "hash": "a" * 64})
        with self.assertRaises(NetworkError):
            InventoryVector.from_dict({"kind": "tx", "hash": "not-a-hash"})
        with self.assertRaises(NetworkError):
            inventory_message(MessageType.PING, [])

    def test_payload_validation_errors(self) -> None:
        invalid_messages = [
            PeerMessage(MessageType.VERACK, {"extra": True}),
            PeerMessage(MessageType.VERSION, {"network": "", "height": 1, "node_id": "n"}),
            PeerMessage(MessageType.VERSION, {"network": "regtest", "height": -2, "node_id": "n"}),
            PeerMessage(MessageType.VERSION, {"network": "regtest", "height": 1, "node_id": ""}),
            PeerMessage(MessageType.PING, {"nonce": -1}),
            PeerMessage(MessageType.INV, {"items": "bad"}),
            PeerMessage(
                MessageType.INV,
                {"items": [InventoryVector("tx", "a" * 64).to_dict()] * (MAX_INVENTORY_ITEMS + 1)},
            ),
            PeerMessage(MessageType.INV, {"items": ["bad"]}),
            PeerMessage(MessageType.GETHEADERS, {"locator": "bad", "limit": 1}),
            PeerMessage(MessageType.GETHEADERS, {"locator": [], "limit": -1}),
            PeerMessage(
                MessageType.GETHEADERS,
                {"locator": [], "limit": 2_001},
            ),
            PeerMessage(
                MessageType.GETHEADERS,
                {"locator": [], "limit": 1, "stop_hash": "bad"},
            ),
            PeerMessage(MessageType.GETHEADERS, {"locator": ["bad"], "limit": 1}),
            PeerMessage(MessageType.HEADERS, {"headers": "bad"}),
            PeerMessage(MessageType.HEADERS, {"headers": ["bad"]}),
            PeerMessage(
                MessageType.HEADERS,
                {
                    "headers": [
                        BlockHeader.from_block(self.block).to_dict(),
                        replace(
                            BlockHeader.from_block(self.block),
                            height=self.block.height + 1,
                            previous_hash="f" * 64,
                        ).to_dict(),
                    ]
                },
            ),
            PeerMessage(
                MessageType.HEADERS,
                {"headers": [BlockHeader.from_block(self.block).to_dict()] * 2_001},
            ),
            PeerMessage(MessageType.TX, {"transaction": "bad"}),
            PeerMessage(MessageType.TX, {"transaction_bytes": "not-hex"}),
            PeerMessage(MessageType.BLOCK, {"block": "bad"}),
            PeerMessage(MessageType.BLOCK, {"block_bytes": "not-hex"}),
            PeerMessage(MessageType.REJECT, {"reason": ""}),
        ]
        for message in invalid_messages:
            with self.assertRaises((NetworkError, ValueError)):
                message.validate_payload()

        with self.assertRaises(NetworkError):
            PeerMessage.from_dict({"type": "unknown", "version": 1, "payload": {}})
        with self.assertRaises(NetworkError):
            PeerMessage.from_dict({"type": "ping", "version": 999, "payload": {"nonce": 1}})
        with self.assertRaises(NetworkError):
            PeerMessage.from_dict({"type": "ping", "version": 1})

    def test_frame_validation_errors(self) -> None:
        valid = PeerMessage(MessageType.PING, {"nonce": 1}).to_frame(AUTH_KEY)

        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(b"short", AUTH_KEY)
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(b"BAD!" + valid[4:], AUTH_KEY)
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(
                NETWORK_MAGIC + struct.pack(">I", MAX_MESSAGE_SIZE + 1), AUTH_KEY
            )
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(valid[:-1], AUTH_KEY)
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(NETWORK_MAGIC + struct.pack(">I", 1) + b"\xff", AUTH_KEY)
        with self.assertRaises(NetworkError):
            PeerMessage.from_frame(NETWORK_MAGIC + struct.pack(">I", 4) + b"true", AUTH_KEY)

        with self.assertRaises(NetworkError):
            read_frame(b"short", AUTH_KEY)
        with self.assertRaises(NetworkError):
            read_frame(b"BAD!" + valid[4:], AUTH_KEY)
        with self.assertRaises(NetworkError):
            read_frame(NETWORK_MAGIC + struct.pack(">I", MAX_MESSAGE_SIZE + 1), AUTH_KEY)
        with self.assertRaises(NetworkError):
            read_frame(valid[:-1], AUTH_KEY)

        message, remainder = read_frame(valid + b"tail", AUTH_KEY)
        self.assertEqual(message.message_type, MessageType.PING)
        self.assertEqual(remainder, b"tail")

    def test_message_size_limit(self) -> None:
        large_reason = "x" * (MAX_MESSAGE_SIZE + 1)
        message = PeerMessage(MessageType.REJECT, {"reason": large_reason})
        with self.assertRaises(NetworkError):
            message.to_frame(AUTH_KEY)

    def test_block_and_transaction_payloads_are_parseable(self) -> None:
        tx_message = transaction_message(self.transaction)
        block_msg = block_message(self.block)

        parsed_tx = PeerMessage.from_frame(tx_message.to_frame(AUTH_KEY), AUTH_KEY)
        parsed_block = PeerMessage.from_frame(block_msg.to_frame(AUTH_KEY), AUTH_KEY)

        self.assertEqual(transaction_from_payload(parsed_tx.payload).txid, self.transaction.txid)
        self.assertEqual(block_from_payload(parsed_block.payload).hash, self.block.hash)
        self.assertNotIn("transaction", parsed_tx.payload)
        self.assertNotIn("block", parsed_block.payload)
        self.assertEqual(
            transaction_from_payload({"transaction": self.transaction.to_dict()}).txid,
            self.transaction.txid,
        )
        self.assertEqual(block_from_payload({"block": self.block.to_dict()}).hash, self.block.hash)
        self.assertEqual(
            json.loads(parsed_block.encode_payload().decode("utf-8"))["type"],
            "block",
        )
        self.assertEqual(HEADER_SIZE, 8)

    def test_block_header_validation(self) -> None:
        header = BlockHeader.from_block(self.block)
        self.assertEqual(BlockHeader.from_dict(header.to_dict()), header)
        self.assertEqual(header.hash, self.block.hash)

        malformed = header.to_dict()
        malformed["height"] = -1
        with self.assertRaises(NetworkError):
            BlockHeader.from_dict(malformed)

        bad_previous = header.to_dict()
        bad_previous["previous_hash"] = "bad"
        with self.assertRaises(NetworkError):
            BlockHeader.from_dict(bad_previous)

        bad_root = header.to_dict()
        bad_root["transaction_root"] = "bad"
        with self.assertRaises(NetworkError):
            BlockHeader.from_dict(bad_root)

        bad_difficulty = header.to_dict()
        bad_difficulty["difficulty"] = -1
        with self.assertRaises(NetworkError):
            BlockHeader.from_dict(bad_difficulty)

        bad_version = header.to_dict()
        bad_version["version"] = 999
        with self.assertRaises(NetworkError):
            BlockHeader.from_dict(bad_version)

        bad_hash = header.to_dict()
        bad_hash["hash"] = "f" * 64
        with self.assertRaises(NetworkError):
            BlockHeader.from_dict(bad_hash)

        missing = header.to_dict()
        del missing["height"]
        with self.assertRaises(NetworkError):
            BlockHeader.from_dict(missing)

    def test_header_chain_validation(self) -> None:
        first = BlockHeader.from_block(self.block)
        second = replace(
            first,
            height=first.height + 1,
            previous_hash=first.hash,
            timestamp=first.timestamp + 1,
        )
        self.assertEqual(validate_header_chain([first, second]), (first, second))
        self.assertEqual(
            headers_from_payload({"headers": [first.to_dict(), second.to_dict()]}),
            (first, second),
        )
        self.assertEqual(validate_header_chain([], previous_hash=first.hash), ())

        with self.assertRaises(NetworkError):
            validate_header_chain([first], previous_hash="bad")
        with self.assertRaises(NetworkError):
            validate_header_chain([first], previous_hash="f" * 64)
        with self.assertRaises(NetworkError):
            validate_header_chain([first, replace(second, previous_hash="f" * 64)])
        with self.assertRaises(NetworkError):
            validate_header_chain([first, replace(second, height=first.height + 2)])
        with self.assertRaises(NetworkError):
            validate_header_chain([replace(first, difficulty=64)])
        with self.assertRaises(NetworkError):
            headers_from_payload({"headers": "bad"})
        with self.assertRaises(NetworkError):
            headers_from_payload({"headers": [first.to_dict()] * 2_001})


if __name__ == "__main__":
    unittest.main()
