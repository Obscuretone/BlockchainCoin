"""In-process peer service for a UTXO node.

``NodeService`` is the bridge between authenticated peer messages and local
node state. It owns peer sessions, inventory relay, header-first synchronization,
download scheduling, peer quality accounting, and misbehavior penalties while
delegating consensus decisions to ``ConsensusNode``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, cast

from .consensus import ConsensusBlock, ConsensusError, ConsensusTransaction
from .network import (
    BlockHeader,
    InventoryVector,
    MessageType,
    PeerMessage,
    block_from_payload,
    block_message,
    headers_from_payload,
    headers_message,
    inventory_message,
    transaction_from_payload,
    transaction_message,
)
from .peer import PeerSession
from .peers import PeerAddressManager, PeerManagerError
from .storage import StoredPeerSyncProgress
from .utxo_node import ConsensusNode, ConsensusNodeError, MempoolPolicyError


class NodeServiceError(RuntimeError):
    """Raised when peer service state or payload routing is invalid."""

    pass


@dataclass
class ServiceResult:
    """Messages and connection action produced by handling one service event."""

    direct: list[PeerMessage] = field(default_factory=list)
    direct_to: dict[str, list[PeerMessage]] = field(default_factory=dict)
    relay: list[PeerMessage] = field(default_factory=list)
    disconnect: bool = False


@dataclass(frozen=True)
class BlockDownload:
    """One in-flight block request assigned to a peer."""

    block_hash: str
    peer: str
    requested_at: int


@dataclass
class PeerSyncProgress:
    """Mutable counters used to score a peer's block-download behavior."""

    requested_blocks: int = 0
    completed_blocks: int = 0
    timed_out_blocks: int = 0
    failed_blocks: int = 0

    @property
    def quality_score(self) -> int:
        penalty = self.failed_blocks * 100 + self.timed_out_blocks * 20 + self.requested_blocks
        reward = self.completed_blocks * 10
        return max(0, penalty - reward)


class PeerSyncProgressStore(Protocol):
    def load_progress(self, address: str) -> StoredPeerSyncProgress | None: ...

    def save_progress(self, progress: StoredPeerSyncProgress) -> None: ...


class NodeService:
    """In-process node behavior for consensus and peer message handling.

    The service is transport-agnostic: TCP handlers call ``receive`` and then
    deliver the returned direct, targeted, and relayed messages. This keeps wire
    I/O separate from synchronization policy and makes the peer layer testable
    without sockets.
    """

    def __init__(
        self,
        node: ConsensusNode,
        network: str,
        node_id: str,
        peers: PeerAddressManager | None = None,
        block_download_timeout: int = 120,
        max_block_download_batch: int = 16,
        max_block_downloads_per_peer: int = 128,
        peer_sync_store: PeerSyncProgressStore | None = None,
    ) -> None:
        if not network:
            raise NodeServiceError("network name is required")
        if not node_id:
            raise NodeServiceError("node id is required")
        if block_download_timeout < 0:
            raise NodeServiceError("block download timeout cannot be negative")
        if max_block_download_batch <= 0:
            raise NodeServiceError("block download batch size must be positive")
        if max_block_downloads_per_peer <= 0:
            raise NodeServiceError("block downloads per peer must be positive")
        self.node = node
        self.network = network
        self.node_id = node_id
        self.peers = peers or PeerAddressManager()
        self.block_download_timeout = block_download_timeout
        self.max_block_download_batch = max_block_download_batch
        self.max_block_downloads_per_peer = max_block_downloads_per_peer
        self.peer_sync_store = peer_sync_store
        self.sessions: dict[str, PeerSession] = {}
        self.transactions: dict[str, ConsensusTransaction] = {}
        self.blocks: dict[str, ConsensusBlock] = {}
        self.in_flight_blocks: dict[str, BlockDownload] = {}
        self.peer_sync: dict[str, PeerSyncProgress] = {}

    def connect_peer(self, address: str, now: int = 0, inbound: bool = False) -> PeerMessage:
        """Register an outbound peer and return the initial version message."""

        self.peers.mark_connecting(address, now=now, inbound=inbound)
        self.peers.mark_connected(address, now=now)
        session = PeerSession(
            network=self.network,
            node_id=self.node_id,
            height=self.node.height,
        )
        self.sessions[address] = session
        return session.start_handshake()

    def disconnect_peer(self, address: str, now: int = 0) -> None:
        """Remove a connected peer and mark any in-flight downloads failed."""

        self.peers.mark_disconnected(address, now=now)
        self.sessions.pop(address, None)
        self._clear_peer_downloads(address, failed=True)

    def accept_inbound_peer(self, address: str, now: int = 0) -> None:
        """Admit an inbound peer subject to peer-manager limits."""

        self.peers.mark_connecting(address, now=now, inbound=True)
        self.peers.mark_connected(address, now=now)
        self.sessions[address] = PeerSession(
            network=self.network,
            node_id=self.node_id,
            height=self.node.height,
        )

    def receive(self, address: str, message: PeerMessage, now: int = 0) -> ServiceResult:
        """Handle one peer message and return transport actions."""

        try:
            session = self._session_for(address, now=now)
        except PeerManagerError:
            return ServiceResult(
                direct=[PeerMessage(MessageType.REJECT, {"reason": "peer banned"})],
                disconnect=True,
            )
        direct = session.receive(message)
        if direct and direct[0].message_type == MessageType.REJECT:
            return ServiceResult(direct=direct, disconnect=self._penalize_peer(address, 10, now))

        relay: list[PeerMessage] = []
        direct_to: dict[str, list[PeerMessage]] = {}
        try:
            if message.message_type == MessageType.TX:
                relay.extend(self._handle_transaction(message))
            elif message.message_type == MessageType.BLOCK:
                relay.extend(self._handle_block(message))
            elif message.message_type == MessageType.GETDATA:
                direct.extend(self._handle_getdata(message))
            elif message.message_type == MessageType.GETHEADERS:
                direct.append(self._handle_getheaders(message))
            elif message.message_type == MessageType.HEADERS:
                targeted = self._handle_headers(address, message, now)
                for peer, messages in targeted.items():
                    if peer == address:
                        direct.extend(messages)
                    else:
                        direct_to[peer] = [*direct_to.get(peer, []), *messages]
        except MempoolPolicyError:
            direct.append(PeerMessage(MessageType.REJECT, {"reason": "policy rejected"}))
        except (ConsensusError, ConsensusNodeError, NodeServiceError):
            disconnect = self._penalize_peer(address, 10, now)
            direct.append(PeerMessage(MessageType.REJECT, {"reason": "invalid payload"}))
            return ServiceResult(direct=direct, relay=relay, disconnect=disconnect)
        return ServiceResult(direct=direct, direct_to=direct_to, relay=relay)

    def submit_local_transaction(self, transaction: ConsensusTransaction) -> ServiceResult:
        """Accept a local transaction and announce its inventory to peers."""

        txid = self.node.submit_transaction(transaction)
        self.transactions[txid] = transaction
        relay = [
            inventory_message(
                MessageType.INV,
                [InventoryVector("tx", txid)],
            )
        ]
        return ServiceResult(relay=relay)

    def mine_local_block(self, miner_address: str) -> ServiceResult:
        """Mine a local block and announce its inventory to peers."""

        stored = self.node.mine_block(miner_address)
        block = stored.block
        self.blocks[block.hash] = block
        for transaction in block.transactions:
            self.transactions[transaction.txid] = transaction
        relay = [
            inventory_message(
                MessageType.INV,
                [InventoryVector("block", block.hash)],
            )
        ]
        return ServiceResult(relay=relay)

    def _session_for(self, address: str, now: int = 0) -> PeerSession:
        session = self.sessions.get(address)
        if session is not None:
            return session
        try:
            self.peers.mark_connected(address, now=now)
        except PeerManagerError:
            self.peers.add_peer(address, now=now)
            self.peers.mark_connected(address, now=now)
        session = PeerSession(
            network=self.network,
            node_id=self.node_id,
            height=self.node.height,
        )
        self.sessions[address] = session
        return session

    def _handle_transaction(self, message: PeerMessage) -> list[PeerMessage]:
        transaction = transaction_from_payload(message.payload)
        txid = transaction.txid
        if txid in self.transactions:
            return []
        self.node.submit_transaction(transaction)
        self.transactions[txid] = transaction
        return [
            inventory_message(
                MessageType.INV,
                [InventoryVector("tx", txid)],
            )
        ]

    def _handle_block(self, message: PeerMessage) -> list[PeerMessage]:
        block = block_from_payload(message.payload)
        if block.hash in self.blocks:
            return []
        self.node.import_block(block)
        self.blocks[block.hash] = block
        download = self.in_flight_blocks.pop(block.hash, None)
        if download is not None:
            self._record_progress(download.peer, completed_blocks=1)
        for transaction in block.transactions:
            self.transactions[transaction.txid] = transaction
        return [
            inventory_message(
                MessageType.INV,
                [InventoryVector("block", block.hash)],
            )
        ]

    def _handle_getdata(self, message: PeerMessage) -> list[PeerMessage]:
        responses: list[PeerMessage] = []
        for raw_item in cast(list[dict[str, object]], message.payload["items"]):
            item = InventoryVector.from_dict(raw_item)
            if item.kind == "tx" and item.hash in self.transactions:
                responses.append(transaction_message(self.transactions[item.hash]))
            elif item.kind == "block" and item.hash in self.blocks:
                responses.append(block_message(self.blocks[item.hash]))
            else:
                raise NodeServiceError("requested inventory is unknown")
        return responses

    def _handle_getheaders(self, message: PeerMessage) -> PeerMessage:
        chain = self.node.store.active_chain()
        locator = cast(list[str], message.payload["locator"])
        stop_hash = message.payload.get("stop_hash")
        limit = int(cast(str | int, message.payload["limit"]))
        heights_by_hash = {stored.block.hash: index for index, stored in enumerate(chain)}

        start = 0
        for block_hash in locator:
            known_height = heights_by_hash.get(block_hash)
            if known_height is not None:
                start = known_height + 1
                break

        headers: list[BlockHeader] = []
        for stored in chain[start:]:
            headers.append(BlockHeader.from_block(stored.block))
            if stored.block.hash == stop_hash or len(headers) >= limit:
                break
        return headers_message(headers)

    def _handle_headers(
        self,
        address: str,
        message: PeerMessage,
        now: int,
    ) -> dict[str, list[PeerMessage]]:
        self.expire_block_downloads(now)
        headers = headers_from_payload(message.payload)
        if (
            headers
            and headers[0].previous_hash != self.node.tip_hash
            and not self.node.store.has_block(headers[0].previous_hash)
        ):
            raise NodeServiceError("headers do not connect to a known block")
        requests: dict[str, list[InventoryVector]] = {}
        for header in headers:
            if not self._should_request_block(header.hash):
                continue
            peer = self._select_block_download_peer(address, header.hash)
            if peer is None:
                continue
            item = InventoryVector("block", header.hash)
            self.in_flight_blocks[item.hash] = BlockDownload(
                block_hash=item.hash,
                peer=peer,
                requested_at=now,
            )
            self._record_progress(peer, requested_blocks=1)
            requests.setdefault(peer, []).append(item)
        return {
            peer: [
                inventory_message(MessageType.GETDATA, batch)
                for batch in self._chunk_inventory(items)
            ]
            for peer, items in requests.items()
        }

    def _penalize_peer(self, address: str, score: int, now: int) -> bool:
        record = self.peers.report_misbehavior(address, score, now=now)
        if not record.is_banned:
            return False
        self.sessions.pop(address, None)
        self._clear_peer_downloads(address, failed=True)
        return True

    def expire_block_downloads(self, now: int) -> tuple[BlockDownload, ...]:
        expired = tuple(
            download
            for download in self.in_flight_blocks.values()
            if now - download.requested_at >= self.block_download_timeout
        )
        for download in expired:
            self.in_flight_blocks.pop(download.block_hash, None)
            self._record_progress(download.peer, timed_out_blocks=1)
        return expired

    def retry_expired_block_downloads(self, now: int) -> dict[str, list[PeerMessage]]:
        requests: dict[str, list[InventoryVector]] = {}
        for download in self.expire_block_downloads(now):
            if not self._should_request_block(download.block_hash):
                continue
            peer = self._select_block_download_peer(
                download.peer,
                download.block_hash,
                avoid=download.peer,
            )
            if peer is None:
                continue
            item = InventoryVector("block", download.block_hash)
            self.in_flight_blocks[item.hash] = BlockDownload(
                block_hash=item.hash,
                peer=peer,
                requested_at=now,
            )
            self._record_progress(peer, requested_blocks=1)
            requests.setdefault(peer, []).append(item)
        return {
            peer: [
                inventory_message(MessageType.GETDATA, batch)
                for batch in self._chunk_inventory(items)
            ]
            for peer, items in requests.items()
        }

    def _should_request_block(self, block_hash: str) -> bool:
        return (
            block_hash not in self.blocks
            and block_hash not in self.in_flight_blocks
            and not self.node.store.has_block(block_hash)
        )

    def _select_block_download_peer(
        self,
        source: str,
        block_hash: str,
        avoid: str | None = None,
    ) -> str | None:
        candidates = [
            address
            for address, session in self.sessions.items()
            if address == source or ("block_header", block_hash) in session.known_inventory
        ]
        alternatives = [address for address in candidates if address != avoid]
        if alternatives:
            candidates = alternatives
        if not candidates:
            return source
        below_limit = [
            address
            for address in candidates
            if self._in_flight_count(address) < self.max_block_downloads_per_peer
        ]
        if not below_limit:
            return None
        return min(
            below_limit,
            key=lambda address: (
                self._in_flight_count(address),
                self.sync_progress(address).quality_score,
                self.peers.get_peer(address).misbehavior_score,
                address != source,
                address,
            ),
        )

    def _in_flight_count(self, address: str) -> int:
        return sum(1 for download in self.in_flight_blocks.values() if download.peer == address)

    def _chunk_inventory(self, items: list[InventoryVector]) -> tuple[list[InventoryVector], ...]:
        return tuple(
            items[start : start + self.max_block_download_batch]
            for start in range(0, len(items), self.max_block_download_batch)
        )

    def sync_progress(self, address: str) -> PeerSyncProgress:
        return self._progress_for(address)

    def _progress_for(self, address: str) -> PeerSyncProgress:
        progress = self.peer_sync.get(address)
        if progress is None:
            stored = (
                self.peer_sync_store.load_progress(address)
                if self.peer_sync_store is not None
                else None
            )
            progress = (
                PeerSyncProgress(
                    requested_blocks=stored.requested_blocks,
                    completed_blocks=stored.completed_blocks,
                    timed_out_blocks=stored.timed_out_blocks,
                    failed_blocks=stored.failed_blocks,
                )
                if stored is not None
                else PeerSyncProgress()
            )
            self.peer_sync[address] = progress
        return progress

    def _record_progress(
        self,
        address: str,
        requested_blocks: int = 0,
        completed_blocks: int = 0,
        timed_out_blocks: int = 0,
        failed_blocks: int = 0,
    ) -> None:
        progress = self._progress_for(address)
        progress.requested_blocks += requested_blocks
        progress.completed_blocks += completed_blocks
        progress.timed_out_blocks += timed_out_blocks
        progress.failed_blocks += failed_blocks
        self._save_progress(address, progress)

    def _save_progress(self, address: str, progress: PeerSyncProgress) -> None:
        if self.peer_sync_store is None:
            return
        self.peer_sync_store.save_progress(
            StoredPeerSyncProgress(
                address=address,
                requested_blocks=progress.requested_blocks,
                completed_blocks=progress.completed_blocks,
                timed_out_blocks=progress.timed_out_blocks,
                failed_blocks=progress.failed_blocks,
            )
        )

    def _clear_peer_downloads(self, address: str, failed: bool = False) -> None:
        failed_count = 0
        for block_hash, download in tuple(self.in_flight_blocks.items()):
            if download.peer == address:
                self.in_flight_blocks.pop(block_hash, None)
                failed_count += 1
        if failed and failed_count:
            self._record_progress(address, failed_blocks=failed_count)
