"""Lifecycle facade for running a local node service and TCP server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import MAX_MONEY
from .crypto import Wallet
from .service import NodeService
from .transport import NodeTCPServerAdapter
from .utxo_node import ConsensusNode


class RuntimeError(ValueError):
    """Raised when runtime configuration or lifecycle state is invalid."""

    pass


@dataclass(frozen=True)
class NodeRuntimeConfig:
    """Operator-supplied runtime configuration for one node process."""

    db_path: Path
    network: str
    node_id: str
    host: str = "127.0.0.1"
    port: int = 0
    peer_auth_key: bytes = b""
    difficulty: int = 0
    block_subsidy: int = 50
    max_money: int = MAX_MONEY

    def validate(self) -> None:
        if not self.network:
            raise RuntimeError("network is required")
        if not self.node_id:
            raise RuntimeError("node id is required")
        if not self.host:
            raise RuntimeError("host is required")
        if self.port < 0 or self.port > 65535:
            raise RuntimeError("port is out of range")
        if not self.peer_auth_key:
            raise RuntimeError("peer auth key is required")
        if self.difficulty < 0:
            raise RuntimeError("difficulty cannot be negative")
        if self.block_subsidy < 0:
            raise RuntimeError("block subsidy cannot be negative")
        if self.max_money <= 0:
            raise RuntimeError("max money must be positive")


class NodeRuntime:
    """Owns a consensus node, peer service, and TCP server as one lifecycle."""

    def __init__(
        self,
        config: NodeRuntimeConfig,
        node: ConsensusNode,
        service: NodeService,
        server: NodeTCPServerAdapter,
    ) -> None:
        self.config = config
        self.node = node
        self.service = service
        self.server = server
        self.running = False

    @classmethod
    def create(
        cls,
        config: NodeRuntimeConfig,
        genesis_allocations: dict[str, int],
    ) -> NodeRuntime:
        config.validate()
        node = ConsensusNode.create(
            config.db_path,
            genesis_allocations,
            difficulty=config.difficulty,
            block_subsidy=config.block_subsidy,
            max_money=config.max_money,
        )
        try:
            service = NodeService(node, config.network, config.node_id)
            server = NodeTCPServerAdapter(config.host, config.port, service, config.peer_auth_key)
        except Exception:
            node.close()
            raise
        return cls(config=config, node=node, service=service, server=server)

    @classmethod
    def open(cls, config: NodeRuntimeConfig) -> NodeRuntime:
        config.validate()
        node = ConsensusNode.open(
            config.db_path,
            difficulty=config.difficulty,
            block_subsidy=config.block_subsidy,
            max_money=config.max_money,
        )
        try:
            service = NodeService(node, config.network, config.node_id)
            server = NodeTCPServerAdapter(config.host, config.port, service, config.peer_auth_key)
        except Exception:
            node.close()
            raise
        return cls(config=config, node=node, service=service, server=server)

    @property
    def address(self) -> tuple[str, int]:
        return self.server.address

    def start(self) -> None:
        if self.running:
            raise RuntimeError("runtime is already running")
        self.server.start()
        self.running = True

    def stop(self) -> None:
        if self.running:
            self.server.stop()
            self.running = False
        elif self.server.thread is None:
            self.server.stop()
        self.node.close()

    def mine(self, wallet: Wallet) -> None:
        self.service.mine_local_block(wallet.address)
