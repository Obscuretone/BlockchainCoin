"""Peer address book, connection limits, backoff, and bans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PeerManagerError(ValueError):
    """Raised when peer address-manager state rejects an operation."""

    pass


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BANNED = "banned"


@dataclass
class PeerRecord:
    """Mutable operational record for one known peer address."""

    address: str
    last_seen: int = 0
    failures: int = 0
    misbehavior_score: int = 0
    state: ConnectionState = ConnectionState.DISCONNECTED
    next_retry_at: int = 0
    banned_until: int = 0
    inbound: bool = False

    @property
    def is_banned(self) -> bool:
        return self.state == ConnectionState.BANNED


class PeerAddressManager:
    """Tracks known peers and enforces connection and ban policy."""

    def __init__(
        self,
        max_outbound: int = 8,
        max_inbound: int = 32,
        ban_threshold: int = 100,
        base_backoff_seconds: int = 30,
        max_backoff_seconds: int = 3600,
        ban_seconds: int = 24 * 60 * 60,
    ) -> None:
        if max_outbound < 0 or max_inbound < 0:
            raise PeerManagerError("connection limits cannot be negative")
        if ban_threshold <= 0:
            raise PeerManagerError("ban threshold must be positive")
        self.max_outbound = max_outbound
        self.max_inbound = max_inbound
        self.ban_threshold = ban_threshold
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.ban_seconds = ban_seconds
        self._peers: dict[str, PeerRecord] = {}

    def add_peer(self, address: str, now: int = 0, inbound: bool = False) -> PeerRecord:
        self._validate_address(address)
        existing = self._peers.get(address)
        if existing:
            existing.last_seen = max(existing.last_seen, now)
            existing.inbound = existing.inbound or inbound
            return existing
        record = PeerRecord(address=address, last_seen=now, inbound=inbound)
        self._peers[address] = record
        return record

    def get_peer(self, address: str) -> PeerRecord:
        try:
            return self._peers[address]
        except KeyError as exc:
            raise PeerManagerError("unknown peer") from exc

    def mark_connecting(self, address: str, now: int = 0, inbound: bool = False) -> PeerRecord:
        record = self.add_peer(address, now=now, inbound=inbound)
        self._clear_expired_ban(record, now)
        if record.is_banned:
            raise PeerManagerError("peer is banned")
        if (
            inbound
            and self.inbound_count() >= self.max_inbound
            and record.state != ConnectionState.CONNECTED
        ):
            raise PeerManagerError("inbound peer limit reached")
        if (
            not inbound
            and self.outbound_count() >= self.max_outbound
            and record.state != ConnectionState.CONNECTED
        ):
            raise PeerManagerError("outbound peer limit reached")
        record.state = ConnectionState.CONNECTING
        record.inbound = inbound
        return record

    def mark_connected(self, address: str, now: int = 0) -> PeerRecord:
        record = self.get_peer(address)
        self._clear_expired_ban(record, now)
        if record.is_banned:
            raise PeerManagerError("peer is banned")
        record.state = ConnectionState.CONNECTED
        record.last_seen = now
        record.failures = 0
        record.next_retry_at = 0
        return record

    def mark_disconnected(self, address: str, now: int = 0) -> PeerRecord:
        record = self.get_peer(address)
        if record.is_banned:
            return record
        record.state = ConnectionState.DISCONNECTED
        record.next_retry_at = now
        return record

    def mark_failure(self, address: str, now: int = 0) -> PeerRecord:
        record = self.get_peer(address)
        if record.is_banned:
            return record
        record.failures += 1
        delay = min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * 2 ** (record.failures - 1),
        )
        record.state = ConnectionState.DISCONNECTED
        record.next_retry_at = now + delay
        return record

    def report_misbehavior(self, address: str, score: int, now: int = 0) -> PeerRecord:
        if score < 0:
            raise PeerManagerError("misbehavior score cannot be negative")
        record = self.get_peer(address)
        record.misbehavior_score += score
        if record.misbehavior_score >= self.ban_threshold:
            self.ban_peer(address, now=now)
        return record

    def ban_peer(self, address: str, now: int = 0) -> PeerRecord:
        record = self.get_peer(address)
        record.state = ConnectionState.BANNED
        record.banned_until = now + self.ban_seconds
        record.next_retry_at = record.banned_until
        return record

    def eligible_outbound(self, now: int = 0) -> list[PeerRecord]:
        eligible = []
        for record in self._peers.values():
            self._clear_expired_ban(record, now)
            if (
                record.state == ConnectionState.DISCONNECTED
                and not record.inbound
                and record.next_retry_at <= now
            ):
                eligible.append(record)
        return sorted(eligible, key=lambda peer: (-peer.last_seen, peer.failures, peer.address))

    def select_outbound(self, now: int = 0, limit: int | None = None) -> list[PeerRecord]:
        remaining = max(0, self.max_outbound - self.outbound_count())
        count = remaining if limit is None else min(limit, remaining)
        return self.eligible_outbound(now=now)[:count]

    def eviction_candidates(self) -> list[PeerRecord]:
        connected = [
            peer for peer in self._peers.values() if peer.state == ConnectionState.CONNECTED
        ]
        return sorted(
            connected,
            key=lambda peer: (
                peer.misbehavior_score,
                peer.last_seen,
                peer.address,
            ),
            reverse=True,
        )

    def outbound_count(self) -> int:
        return sum(
            1
            for peer in self._peers.values()
            if peer.state in {ConnectionState.CONNECTING, ConnectionState.CONNECTED}
            and not peer.inbound
        )

    def inbound_count(self) -> int:
        return sum(
            1
            for peer in self._peers.values()
            if peer.state in {ConnectionState.CONNECTING, ConnectionState.CONNECTED}
            and peer.inbound
        )

    def all_peers(self) -> tuple[PeerRecord, ...]:
        return tuple(sorted(self._peers.values(), key=lambda peer: peer.address))

    def _clear_expired_ban(self, record: PeerRecord, now: int) -> None:
        if record.state == ConnectionState.BANNED and record.banned_until <= now:
            record.state = ConnectionState.DISCONNECTED
            record.banned_until = 0

    @staticmethod
    def _validate_address(address: str) -> None:
        if not address or address.isspace():
            raise PeerManagerError("peer address is invalid")
        if len(address) > 255:
            raise PeerManagerError("peer address is too long")
