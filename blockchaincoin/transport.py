"""TCP transport adapter for authenticated peer messages.

This module is intentionally thin. It owns sockets, buffering, connection
registration, and fanout; all protocol semantics remain in ``network`` and
``service`` so the transport can be tested as plumbing instead of consensus.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from .network import (
    AUTH_TAG_SIZE,
    HEADER_SIZE,
    MAX_MESSAGE_SIZE,
    NETWORK_MAGIC,
    NetworkError,
    PeerMessage,
    read_frame,
)
from .peers import PeerManagerError
from .service import NodeService


class TransportError(RuntimeError):
    """Raised when TCP transport lifecycle or usage is invalid."""

    pass


@dataclass
class ConnectionBuffer:
    """Accumulates TCP bytes until complete authenticated frames are available."""

    auth_key: bytes
    data: bytes = b""

    def feed(self, chunk: bytes) -> list[PeerMessage]:
        if not chunk:
            if not self.data:
                return []
        else:
            self.data += chunk
        messages: list[PeerMessage] = []
        while self._has_complete_frame():
            message, self.data = read_frame(self.data, auth_key=self.auth_key)
            messages.append(message)
        return messages

    def _has_complete_frame(self) -> bool:
        if len(self.data) < HEADER_SIZE:
            return False
        if self.data[:4] != NETWORK_MAGIC:
            raise NetworkError("network magic is invalid")
        size = int.from_bytes(self.data[4:HEADER_SIZE], "big")
        if size > MAX_MESSAGE_SIZE:
            raise NetworkError("message exceeds maximum size")
        return len(self.data) >= HEADER_SIZE + size + AUTH_TAG_SIZE


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    adapter: NodeTCPServerAdapter


class NodeTCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast(ThreadedTCPServer, self.server).adapter
        address = f"{self.client_address[0]}:{self.client_address[1]}"
        buffer = ConnectionBuffer(server.auth_key)
        try:
            server.service.accept_inbound_peer(address)
        except PeerManagerError:
            return
        server.register_connection(address, self.request)
        try:
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                try:
                    messages = buffer.feed(chunk)
                except NetworkError:
                    break
                for message in messages:
                    result = server.service.receive(address, message)
                    for response in result.direct:
                        self.request.sendall(response.to_frame(server.auth_key))
                    for peer, responses in result.direct_to.items():
                        server.send_to(peer, responses)
                    server.broadcast(result.relay, exclude=address)
                    if result.disconnect:
                        return
        finally:
            server.unregister_connection(address)


class NodeTCPServerAdapter:
    """Threaded TCP server that dispatches frames through ``NodeService``."""

    def __init__(self, host: str, port: int, service: NodeService, auth_key: bytes) -> None:
        if not auth_key:
            raise TransportError("peer auth key is required")
        self.service = service
        self.auth_key = auth_key
        self.server = ThreadedTCPServer((host, port), NodeTCPHandler)
        self.server.adapter = self
        self.thread: threading.Thread | None = None
        self._connections: dict[str, socket.socket] = {}
        self._connections_lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        host, port = cast(tuple[str, int], self.server.server_address)
        return str(host), int(port)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            raise TransportError("server is already running")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread is None:
            self.server.server_close()
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def register_connection(self, address: str, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections[address] = connection

    def unregister_connection(self, address: str) -> None:
        with self._connections_lock:
            self._connections.pop(address, None)

    def broadcast(self, messages: list[PeerMessage], exclude: str | None = None) -> None:
        if not messages:
            return
        with self._connections_lock:
            recipients = [
                (address, connection)
                for address, connection in self._connections.items()
                if address != exclude
            ]
        stale: list[str] = []
        for address, connection in recipients:
            try:
                for message in messages:
                    connection.sendall(message.to_frame(self.auth_key))
            except OSError:
                stale.append(address)
        for address in stale:
            self.unregister_connection(address)

    def send_to(self, address: str, messages: list[PeerMessage]) -> None:
        if not messages:
            return
        with self._connections_lock:
            connection = self._connections.get(address)
        if connection is None:
            return
        try:
            for message in messages:
                connection.sendall(message.to_frame(self.auth_key))
        except OSError:
            self.unregister_connection(address)


class TCPPeerClient:
    """Small blocking TCP client used by tests and local peer workflows."""

    def __init__(
        self,
        host: str,
        port: int,
        auth_key: bytes,
        timeout: float = 2.0,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        if not auth_key:
            raise TransportError("peer auth key is required")
        self.host = host
        self.port = port
        self.auth_key = auth_key
        self.timeout = timeout
        self.socket_factory = socket_factory
        self.socket: socket.socket | None = None
        self.buffer = ConnectionBuffer(auth_key)

    def connect(self) -> None:
        if self.socket is not None:
            raise TransportError("client is already connected")
        self.socket = self.socket_factory((self.host, self.port), self.timeout)

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def send(self, message: PeerMessage) -> None:
        if self.socket is None:
            raise TransportError("client is not connected")
        self.socket.sendall(message.to_frame(self.auth_key))

    def recv_messages(self, max_bytes: int = 65536) -> list[PeerMessage]:
        if self.socket is None:
            raise TransportError("client is not connected")
        chunk = self.socket.recv(max_bytes)
        if not chunk:
            return []
        return self.buffer.feed(chunk)

    def request(self, message: PeerMessage) -> list[PeerMessage]:
        self.send(message)
        return self.recv_messages()
