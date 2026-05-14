"""Peer session state machine for validated protocol messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from .network import (
    InventoryVector,
    MessageType,
    NetworkError,
    PeerMessage,
    block_from_payload,
    headers_from_payload,
    inventory_message,
    transaction_from_payload,
    version_message,
)


class PeerError(RuntimeError):
    """Raised when a peer violates session ordering or state rules."""

    pass


@dataclass
class PeerSession:
    """One transport peer's handshake, liveness, inventory, and score state."""

    network: str
    node_id: str
    height: int = -1
    remote_node_id: str | None = None
    remote_height: int = -1
    version_sent: bool = False
    version_received: bool = False
    verack_sent: bool = False
    verack_received: bool = False
    pending_pings: set[int] = field(default_factory=set)
    known_inventory: set[tuple[str, str]] = field(default_factory=set)
    requested_inventory: set[tuple[str, str]] = field(default_factory=set)
    misbehavior_score: int = 0
    max_inventory_items: int = 10_000

    @property
    def handshake_complete(self) -> bool:
        return (
            self.version_sent
            and self.version_received
            and self.verack_sent
            and self.verack_received
        )

    def start_handshake(self) -> PeerMessage:
        self.version_sent = True
        return version_message(self.network, self.height, self.node_id)

    def receive(self, message: PeerMessage) -> list[PeerMessage]:
        try:
            message.validate_payload()
            handler = {
                MessageType.VERSION: self._handle_version,
                MessageType.VERACK: self._handle_verack,
                MessageType.PING: self._handle_ping,
                MessageType.PONG: self._handle_pong,
                MessageType.INV: self._handle_inv,
                MessageType.GETDATA: self._handle_getdata,
                MessageType.GETHEADERS: self._handle_getheaders,
                MessageType.HEADERS: self._handle_headers,
                MessageType.TX: self._handle_payload,
                MessageType.BLOCK: self._handle_payload,
                MessageType.REJECT: self._handle_reject,
            }[message.message_type]
            return handler(message)
        except (NetworkError, PeerError):
            self.misbehavior_score += 1
            return [PeerMessage(MessageType.REJECT, {"reason": "invalid message"})]

    def send_verack(self) -> PeerMessage:
        if not self.version_received:
            raise PeerError("cannot verack before receiving version")
        self.verack_sent = True
        return PeerMessage(MessageType.VERACK, {})

    def send_ping(self, nonce: int) -> PeerMessage:
        if nonce < 0:
            raise PeerError("ping nonce cannot be negative")
        self.pending_pings.add(nonce)
        return PeerMessage(MessageType.PING, {"nonce": nonce})

    def announce(self, items: list[InventoryVector]) -> PeerMessage:
        for item in items:
            self.known_inventory.add((item.kind, item.hash))
        return inventory_message(MessageType.INV, items)

    def request(self, items: list[InventoryVector]) -> PeerMessage:
        for item in items:
            self.requested_inventory.add((item.kind, item.hash))
        return inventory_message(MessageType.GETDATA, items)

    def _handle_version(self, message: PeerMessage) -> list[PeerMessage]:
        network = str(message.payload["network"])
        if network != self.network:
            raise PeerError("peer is on a different network")
        self.remote_node_id = str(message.payload["node_id"])
        if self.remote_node_id == self.node_id:
            raise PeerError("peer has the same node id")
        self.remote_height = int(cast(str | int, message.payload["height"]))
        self.version_received = True
        responses: list[PeerMessage] = []
        if not self.version_sent:
            responses.append(self.start_handshake())
        if not self.verack_sent:
            responses.append(self.send_verack())
        return responses

    def _handle_verack(self, message: PeerMessage) -> list[PeerMessage]:
        if not self.version_sent:
            raise PeerError("received verack before sending version")
        self.verack_received = True
        return []

    def _handle_ping(self, message: PeerMessage) -> list[PeerMessage]:
        return [PeerMessage(MessageType.PONG, {"nonce": message.payload["nonce"]})]

    def _handle_pong(self, message: PeerMessage) -> list[PeerMessage]:
        nonce = int(cast(str | int, message.payload["nonce"]))
        if nonce not in self.pending_pings:
            raise PeerError("pong nonce was not pending")
        self.pending_pings.remove(nonce)
        return []

    def _handle_inv(self, message: PeerMessage) -> list[PeerMessage]:
        wanted: list[InventoryVector] = []
        for raw_item in cast(list[dict[str, object]], message.payload["items"]):
            item = InventoryVector.from_dict(raw_item)
            key = (item.kind, item.hash)
            if key not in self.known_inventory:
                if len(self.known_inventory) >= self.max_inventory_items:
                    raise PeerError("known inventory limit reached")
                self.known_inventory.add(key)
                wanted.append(item)
        return [self.request(wanted)] if wanted else []

    def _handle_getdata(self, message: PeerMessage) -> list[PeerMessage]:
        for raw_item in cast(list[dict[str, object]], message.payload["items"]):
            item = InventoryVector.from_dict(raw_item)
            if len(self.requested_inventory) >= self.max_inventory_items:
                raise PeerError("requested inventory limit reached")
            self.requested_inventory.add((item.kind, item.hash))
        return []

    def _handle_getheaders(self, message: PeerMessage) -> list[PeerMessage]:
        for block_hash in cast(list[str], message.payload["locator"]):
            self.requested_inventory.add(("block_header", block_hash))
        return []

    def _handle_headers(self, message: PeerMessage) -> list[PeerMessage]:
        for header in headers_from_payload(message.payload):
            if len(self.known_inventory) >= self.max_inventory_items:
                raise PeerError("known inventory limit reached")
            self.known_inventory.add(("block_header", header.hash))
        return []

    def _handle_payload(self, message: PeerMessage) -> list[PeerMessage]:
        if message.message_type == MessageType.TX:
            transaction = transaction_from_payload(message.payload)
            self.known_inventory.add(("tx", transaction.txid))
        if message.message_type == MessageType.BLOCK:
            block = block_from_payload(message.payload)
            self.known_inventory.add(("block", block.hash))
        return []

    def _handle_reject(self, message: PeerMessage) -> list[PeerMessage]:
        self.misbehavior_score += 1
        return []
