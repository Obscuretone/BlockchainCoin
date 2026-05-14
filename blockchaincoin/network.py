"""Peer wire protocol and authenticated frame codec.

The network layer defines message shapes and defensive parsing for peer traffic.
Frames use a magic prefix, length field, JSON payload, and HMAC-SHA256 tag. The
HMAC is not a substitute for public Internet transport security, but it prevents
accidental unauthenticated peers from participating in private or test networks.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest
from hmac import new as hmac_new
from typing import cast

from .consensus import (
    SUPPORTED_BLOCK_VERSIONS,
    ConsensusBlock,
    ConsensusError,
    ConsensusTransaction,
)
from .crypto import canonical_json, sha256_hex

NETWORK_MAGIC = b"BCCN"
PROTOCOL_VERSION = 1
MAX_MESSAGE_SIZE = 2_000_000
MAX_INVENTORY_ITEMS = 2_000
HEADER_SIZE = 8
AUTH_TAG_SIZE = 32


class NetworkError(ValueError):
    """Raised when a peer frame or message violates protocol rules."""

    pass


class MessageType(StrEnum):
    VERSION = "version"
    VERACK = "verack"
    PING = "ping"
    PONG = "pong"
    INV = "inv"
    GETDATA = "getdata"
    GETHEADERS = "getheaders"
    HEADERS = "headers"
    TX = "tx"
    BLOCK = "block"
    REJECT = "reject"


@dataclass(frozen=True)
class FrameAuthenticator:
    """HMAC-SHA256 authenticator for peer frames."""

    key: bytes

    def __post_init__(self) -> None:
        if not self.key:
            raise NetworkError("frame authentication key is required")

    def tag(self, header_and_payload: bytes) -> bytes:
        return hmac_new(self.key, header_and_payload, sha256).digest()

    def verify(self, header_and_payload: bytes, tag: bytes) -> None:
        if len(tag) != AUTH_TAG_SIZE:
            raise NetworkError("frame authentication tag is invalid")
        if not compare_digest(self.tag(header_and_payload), tag):
            raise NetworkError("frame authentication failed")


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class InventoryVector:
    """Compact reference to a transaction or block advertised by a peer."""

    kind: str
    hash: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "hash": self.hash}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> InventoryVector:
        kind = str(data["kind"])
        item_hash = str(data["hash"])
        if kind not in {"tx", "block"}:
            raise NetworkError("inventory kind is invalid")
        if not _is_hash(item_hash):
            raise NetworkError("inventory hash is invalid")
        return cls(kind=kind, hash=item_hash)


@dataclass(frozen=True)
class BlockHeader:
    """Serializable block header used during header-first synchronization."""

    height: int
    previous_hash: str
    transaction_root: str
    difficulty: int
    timestamp: float
    nonce: int
    version: int = 1

    @classmethod
    def from_block(cls, block: ConsensusBlock) -> BlockHeader:
        return cls(
            height=block.height,
            previous_hash=block.previous_hash,
            transaction_root=block.transaction_root,
            difficulty=block.difficulty,
            timestamp=block.timestamp,
            nonce=block.nonce,
            version=block.version,
        )

    @property
    def hash(self) -> str:
        return ConsensusBlock(
            height=self.height,
            previous_hash=self.previous_hash,
            transactions=(),
            difficulty=self.difficulty,
            timestamp=self.timestamp,
            nonce=self.nonce,
            transaction_root=self.transaction_root,
            version=self.version,
        ).hash

    def to_dict(self) -> dict[str, object]:
        return {
            "difficulty": self.difficulty,
            "hash": self.hash,
            "height": self.height,
            "nonce": self.nonce,
            "previous_hash": self.previous_hash,
            "timestamp": self.timestamp,
            "transaction_root": self.transaction_root,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BlockHeader:
        try:
            header = cls(
                height=int(cast(str | int, data["height"])),
                previous_hash=str(data["previous_hash"]),
                transaction_root=str(data["transaction_root"]),
                difficulty=int(cast(str | int, data["difficulty"])),
                timestamp=float(cast(str | int | float, data["timestamp"])),
                nonce=int(cast(str | int, data["nonce"])),
                version=int(cast(str | int, data.get("version", 1))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkError("block header is malformed") from exc
        header_hash = data.get("hash")
        if header.height < 0:
            raise NetworkError("block header height is invalid")
        if header.difficulty < 0:
            raise NetworkError("block header difficulty is invalid")
        if header.version not in SUPPORTED_BLOCK_VERSIONS:
            raise NetworkError("block header version is unsupported")
        if not _is_hash(header.previous_hash):
            raise NetworkError("block header previous hash is invalid")
        if not _is_hash(header.transaction_root):
            raise NetworkError("block header transaction root is invalid")
        if header_hash is not None and header_hash != header.hash:
            raise NetworkError("block header hash is invalid")
        return header


@dataclass(frozen=True)
class PeerMessage:
    """Versioned peer message with validated payload semantics."""

    message_type: MessageType
    payload: dict[str, object]
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.message_type.value,
            "version": self.version,
            "payload": self.payload,
        }

    @property
    def checksum(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    def encode_payload(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")

    def to_frame(self, auth_key: bytes) -> bytes:
        payload = self.encode_payload()
        if len(payload) > MAX_MESSAGE_SIZE:
            raise NetworkError("message exceeds maximum size")
        frame = NETWORK_MAGIC + struct.pack(">I", len(payload)) + payload
        return frame + FrameAuthenticator(auth_key).tag(frame)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PeerMessage:
        try:
            message_type = MessageType(str(data["type"]))
            version = int(cast(str | int, data["version"]))
            payload = cast(dict[str, object], data["payload"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkError("message is malformed") from exc
        if version != PROTOCOL_VERSION:
            raise NetworkError("protocol version is unsupported")
        message = cls(message_type=message_type, payload=payload, version=version)
        message.validate_payload()
        return message

    @classmethod
    def from_frame(cls, frame: bytes, auth_key: bytes) -> PeerMessage:
        if len(frame) < HEADER_SIZE:
            raise NetworkError("frame is too short")
        if frame[:4] != NETWORK_MAGIC:
            raise NetworkError("network magic is invalid")
        size = struct.unpack(">I", frame[4:HEADER_SIZE])[0]
        if size > MAX_MESSAGE_SIZE:
            raise NetworkError("message exceeds maximum size")
        end = HEADER_SIZE + size
        expected_size = end + AUTH_TAG_SIZE
        if len(frame) != expected_size:
            raise NetworkError("frame payload length mismatch")
        FrameAuthenticator(auth_key).verify(frame[:end], frame[end:expected_size])
        payload = frame[HEADER_SIZE:end]
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError("frame payload is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise NetworkError("message payload must be an object")
        return cls.from_dict(decoded)

    def validate_payload(self) -> None:
        validators = {
            MessageType.VERSION: self._validate_version,
            MessageType.VERACK: self._validate_empty,
            MessageType.PING: self._validate_nonce,
            MessageType.PONG: self._validate_nonce,
            MessageType.INV: self._validate_inventory,
            MessageType.GETDATA: self._validate_inventory,
            MessageType.GETHEADERS: self._validate_getheaders,
            MessageType.HEADERS: self._validate_headers,
            MessageType.TX: self._validate_transaction,
            MessageType.BLOCK: self._validate_block,
            MessageType.REJECT: self._validate_reject,
        }
        validators[self.message_type]()

    def _validate_empty(self) -> None:
        if self.payload:
            raise NetworkError("message payload must be empty")

    def _validate_version(self) -> None:
        network = self.payload.get("network")
        height = self.payload.get("height")
        node_id = self.payload.get("node_id")
        if not isinstance(network, str) or not network:
            raise NetworkError("version network is invalid")
        if not isinstance(height, int) or height < -1:
            raise NetworkError("version height is invalid")
        if not isinstance(node_id, str) or not node_id:
            raise NetworkError("version node id is invalid")

    def _validate_nonce(self) -> None:
        nonce = self.payload.get("nonce")
        if not isinstance(nonce, int) or nonce < 0:
            raise NetworkError("nonce is invalid")

    def _validate_inventory(self) -> None:
        items = self.payload.get("items")
        if not isinstance(items, list):
            raise NetworkError("inventory items are invalid")
        if len(items) > MAX_INVENTORY_ITEMS:
            raise NetworkError("inventory items exceed maximum count")
        for item in items:
            InventoryVector.from_dict(dict(item))

    def _validate_getheaders(self) -> None:
        locator = self.payload.get("locator")
        stop_hash = self.payload.get("stop_hash")
        limit = self.payload.get("limit")
        if not isinstance(locator, list):
            raise NetworkError("header locator is invalid")
        if not isinstance(limit, int) or limit < 0 or limit > 2_000:
            raise NetworkError("header limit is invalid")
        if stop_hash is not None and not _is_hash(stop_hash):
            raise NetworkError("header stop hash is invalid")
        for item in locator:
            if not _is_hash(item):
                raise NetworkError("header locator hash is invalid")

    def _validate_headers(self) -> None:
        headers = self.payload.get("headers")
        if not isinstance(headers, list):
            raise NetworkError("headers payload is invalid")
        if len(headers) > 2_000:
            raise NetworkError("headers payload is too large")
        validate_header_chain([BlockHeader.from_dict(dict(header)) for header in headers])

    def _validate_transaction(self) -> None:
        transaction_from_payload(self.payload)

    def _validate_block(self) -> None:
        block_from_payload(self.payload)

    def _validate_reject(self) -> None:
        reason = self.payload.get("reason")
        if not isinstance(reason, str) or not reason:
            raise NetworkError("reject reason is invalid")


def version_message(network: str, height: int, node_id: str) -> PeerMessage:
    """Build the opening handshake message for a peer session."""

    return PeerMessage(
        MessageType.VERSION,
        {"network": network, "height": height, "node_id": node_id},
    )


def inventory_message(
    message_type: MessageType,
    items: list[InventoryVector],
) -> PeerMessage:
    """Build an ``inv`` or ``getdata`` message from inventory vectors."""

    if message_type not in {MessageType.INV, MessageType.GETDATA}:
        raise NetworkError("inventory message type is invalid")
    return PeerMessage(message_type, {"items": [item.to_dict() for item in items]})


def getheaders_message(
    locator: list[str],
    stop_hash: str | None = None,
    limit: int = 2_000,
) -> PeerMessage:
    """Build a compact request for headers after one of the locator hashes."""

    return PeerMessage(
        MessageType.GETHEADERS,
        {"limit": limit, "locator": locator, "stop_hash": stop_hash},
    )


def headers_message(headers: list[BlockHeader]) -> PeerMessage:
    """Build a headers response message."""

    return PeerMessage(MessageType.HEADERS, {"headers": [header.to_dict() for header in headers]})


def headers_from_payload(payload: Mapping[str, object]) -> tuple[BlockHeader, ...]:
    """Decode and validate a peer headers payload."""

    headers = payload.get("headers")
    if not isinstance(headers, list):
        raise NetworkError("headers payload is invalid")
    if len(headers) > 2_000:
        raise NetworkError("headers payload is too large")
    return validate_header_chain([BlockHeader.from_dict(dict(header)) for header in headers])


def validate_header_chain(
    headers: list[BlockHeader],
    previous_hash: str | None = None,
) -> tuple[BlockHeader, ...]:
    """Validate proof of work and continuity for an ordered header batch."""

    if previous_hash is not None and not _is_hash(previous_hash):
        raise NetworkError("header chain anchor is invalid")
    prior_hash = previous_hash
    prior_height: int | None = None
    for header in headers:
        if not header.hash.startswith("0" * header.difficulty):
            raise NetworkError("block header proof of work is invalid")
        if prior_hash is not None and header.previous_hash != prior_hash:
            raise NetworkError("block headers are disconnected")
        if prior_height is not None and header.height != prior_height + 1:
            raise NetworkError("block header height is not sequential")
        prior_hash = header.hash
        prior_height = header.height
    return tuple(headers)


def transaction_message(transaction: ConsensusTransaction) -> PeerMessage:
    """Build a transaction relay message using the canonical binary codec."""

    return PeerMessage(MessageType.TX, {"transaction_bytes": transaction.to_bytes().hex()})


def block_message(block: ConsensusBlock) -> PeerMessage:
    """Build a block relay message using the canonical binary codec."""

    return PeerMessage(MessageType.BLOCK, {"block_bytes": block.to_bytes().hex()})


def transaction_from_payload(payload: Mapping[str, object]) -> ConsensusTransaction:
    """Decode a transaction payload from binary or legacy dictionary form."""

    raw_transaction = payload.get("transaction_bytes")
    if isinstance(raw_transaction, str):
        try:
            return ConsensusTransaction.from_bytes(bytes.fromhex(raw_transaction))
        except (ConsensusError, ValueError) as exc:
            raise NetworkError("transaction binary payload is invalid") from exc
    transaction = payload.get("transaction")
    if isinstance(transaction, dict):
        return ConsensusTransaction.from_dict(transaction)
    raise NetworkError("transaction payload is invalid")


def block_from_payload(payload: Mapping[str, object]) -> ConsensusBlock:
    """Decode a block payload from binary or legacy dictionary form."""

    raw_block = payload.get("block_bytes")
    if isinstance(raw_block, str):
        try:
            return ConsensusBlock.from_bytes(bytes.fromhex(raw_block))
        except (ConsensusError, ValueError) as exc:
            raise NetworkError("block binary payload is invalid") from exc
    block = payload.get("block")
    if isinstance(block, dict):
        return ConsensusBlock.from_dict(block)
    raise NetworkError("block payload is invalid")


def read_frame(buffer: bytes, auth_key: bytes) -> tuple[PeerMessage, bytes]:
    """Read one complete authenticated frame and return remaining bytes."""

    if len(buffer) < HEADER_SIZE:
        raise NetworkError("buffer does not contain a complete header")
    if buffer[:4] != NETWORK_MAGIC:
        raise NetworkError("network magic is invalid")
    size = struct.unpack(">I", buffer[4:HEADER_SIZE])[0]
    if size > MAX_MESSAGE_SIZE:
        raise NetworkError("message exceeds maximum size")
    end = HEADER_SIZE + size + AUTH_TAG_SIZE
    if len(buffer) < end:
        raise NetworkError("buffer does not contain a complete frame")
    return PeerMessage.from_frame(buffer[:end], auth_key=auth_key), buffer[end:]
