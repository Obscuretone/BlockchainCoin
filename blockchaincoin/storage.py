"""SQLite-backed durable stores for node state.

Storage classes in this module keep consensus objects and operational indexes
on disk using SQLite WAL mode. The fork store is the canonical block database
for the UTXO node; the other stores cache mempool entries, UTXO snapshots,
invalid-block decisions, and peer sync quality so restart behavior is explicit
and testable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .consensus import UTXO, ConsensusBlock, ConsensusTransaction, OutPoint, TxOutput, UTXOSet
from .crypto import canonical_json, sha256_hex

STORE_SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    """Raised when durable state cannot satisfy a storage invariant."""

    pass


@dataclass(frozen=True)
class StoredConsensusBlock:
    """UTXO consensus block plus cumulative work."""

    block: ConsensusBlock
    cumulative_work: int


@dataclass(frozen=True)
class IndexedSpend:
    """Indexed record showing which transaction consumed an outpoint."""

    outpoint: OutPoint
    spending_txid: str
    input_index: int


@dataclass(frozen=True)
class StoredPeerSyncProgress:
    """Persisted counters used to score block-download peer quality."""

    address: str
    requested_blocks: int = 0
    completed_blocks: int = 0
    timed_out_blocks: int = 0
    failed_blocks: int = 0


@dataclass(frozen=True)
class ChainParameters:
    """Consensus parameters persisted with a chain database.

    ``chain_id`` is derived from the canonical parameter payload. Reopened nodes
    must supply matching parameters before the stored blocks are trusted.
    """

    difficulty: int
    block_subsidy: int
    max_money: int

    @property
    def chain_id(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    def to_dict(self) -> dict[str, int]:
        return {
            "block_subsidy": self.block_subsidy,
            "difficulty": self.difficulty,
            "max_money": self.max_money,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ChainParameters:
        return cls(
            difficulty=int(cast(str | int, data["difficulty"])),
            block_subsidy=int(cast(str | int, data["block_subsidy"])),
            max_money=int(cast(str | int, data["max_money"])),
        )


def _encode_consensus_block(block: ConsensusBlock) -> str:
    return block.to_bytes().hex()


def _decode_consensus_block(payload: str) -> ConsensusBlock:
    return ConsensusBlock.from_bytes(bytes.fromhex(payload))


class SQLiteForkStore:
    """Fork-aware consensus block storage.

    Blocks are keyed by hash and may share the same height. Best tip selection
    uses cumulative work, then height, then hash for deterministic tie-breaking.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA synchronous=NORMAL")

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fork_blocks (
                    hash TEXT PRIMARY KEY,
                    height INTEGER NOT NULL,
                    previous_hash TEXT NOT NULL,
                    difficulty INTEGER NOT NULL,
                    cumulative_work INTEGER NOT NULL,
                    data TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fork_blocks_previous ON fork_blocks(previous_hash)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fork_blocks_height ON fork_blocks(height)"
            )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata (key, value) VALUES ('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
                return
            if int(row["value"]) != STORE_SCHEMA_VERSION:
                raise StorageError(f"unsupported store schema version: {row['value']}")

    def put_block(self, block: ConsensusBlock) -> StoredConsensusBlock:
        if self.has_block(block.hash):
            return self.get_block(block.hash)

        if block.height == 0:
            if block.previous_hash != "0" * 64:
                raise StorageError("genesis block previous hash is invalid")
            cumulative_work = self._work_for(block)
        else:
            parent = self.get_block(block.previous_hash)
            if block.height != parent.block.height + 1:
                raise StorageError("block height does not extend parent")
            cumulative_work = parent.cumulative_work + self._work_for(block)

        payload = _encode_consensus_block(block)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO fork_blocks (
                    hash,
                    height,
                    previous_hash,
                    difficulty,
                    cumulative_work,
                    data
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    block.hash,
                    block.height,
                    block.previous_hash,
                    block.difficulty,
                    cumulative_work,
                    payload,
                ),
            )
        return StoredConsensusBlock(block=block, cumulative_work=cumulative_work)

    def save_chain_parameters(self, parameters: ChainParameters) -> None:
        payload = canonical_json(parameters.to_dict())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES ('chain_parameters', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (payload,),
            )
            self.connection.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES ('chain_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (parameters.chain_id,),
            )

    def load_chain_parameters(self) -> ChainParameters | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'chain_parameters'"
        ).fetchone()
        if row is None:
            return None
        return ChainParameters.from_dict(json.loads(str(row["value"])))

    def require_chain_parameters(self, expected: ChainParameters) -> None:
        stored = self.load_chain_parameters()
        if stored is None:
            if self.best_tip() is None:
                self.save_chain_parameters(expected)
                return
            raise StorageError("chain parameters are missing")
        if stored != expected:
            raise StorageError("chain parameters do not match stored chain")

    def has_block(self, block_hash: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM fork_blocks WHERE hash = ?",
            (block_hash,),
        ).fetchone()
        return row is not None

    def get_block(self, block_hash: str) -> StoredConsensusBlock:
        row = self.connection.execute(
            "SELECT data, cumulative_work FROM fork_blocks WHERE hash = ?",
            (block_hash,),
        ).fetchone()
        if row is None:
            raise StorageError(f"unknown block: {block_hash}")
        return StoredConsensusBlock(
            block=_decode_consensus_block(str(row["data"])),
            cumulative_work=int(row["cumulative_work"]),
        )

    def best_tip(self) -> StoredConsensusBlock | None:
        row = self.connection.execute(
            """
            SELECT hash
            FROM fork_blocks
            ORDER BY cumulative_work DESC, height DESC, hash ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self.get_block(str(row["hash"]))

    def children_of(self, block_hash: str) -> list[StoredConsensusBlock]:
        rows = self.connection.execute(
            "SELECT hash FROM fork_blocks WHERE previous_hash = ? ORDER BY hash ASC",
            (block_hash,),
        ).fetchall()
        return [self.get_block(str(row["hash"])) for row in rows]

    def chain_to_tip(self, tip_hash: str) -> list[StoredConsensusBlock]:
        chain: list[StoredConsensusBlock] = []
        current = self.get_block(tip_hash)
        while True:
            chain.append(current)
            if current.block.height == 0:
                break
            current = self.get_block(current.block.previous_hash)
        chain.reverse()
        return chain

    def active_chain(self) -> list[StoredConsensusBlock]:
        tip = self.best_tip()
        if tip is None:
            return []
        return self.chain_to_tip(tip.block.hash)

    def iter_blocks(self) -> Iterator[StoredConsensusBlock]:
        rows = self.connection.execute(
            "SELECT hash FROM fork_blocks ORDER BY height ASC, hash ASC"
        ).fetchall()
        for row in rows:
            yield self.get_block(str(row["hash"]))

    @staticmethod
    def _work_for(block: ConsensusBlock) -> int:
        return 2**block.difficulty


class SQLiteUTXOIndex:
    """Persistent UTXO snapshots and spent-output indexes.

    The active snapshot accelerates node reopen. Per-tip snapshots and spend
    indexes preserve branch-local query surfaces for competing forks.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA synchronous=NORMAL")

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS utxos (
                    txid TEXT NOT NULL,
                    output_index INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    PRIMARY KEY (txid, output_index)
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS utxo_snapshots (
                    tip_hash TEXT NOT NULL,
                    txid TEXT NOT NULL,
                    output_index INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    PRIMARY KEY (tip_hash, txid, output_index)
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spent_outpoints (
                    tip_hash TEXT NOT NULL,
                    txid TEXT NOT NULL,
                    output_index INTEGER NOT NULL,
                    spending_txid TEXT NOT NULL,
                    input_index INTEGER NOT NULL,
                    PRIMARY KEY (tip_hash, txid, output_index)
                )
                """
            )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata (key, value) VALUES ('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
                return
            if int(row["value"]) != STORE_SCHEMA_VERSION:
                raise StorageError(f"unsupported store schema version: {row['value']}")

    def save_snapshot(self, tip_hash: str, utxos: UTXOSet) -> None:
        """Replace the active snapshot and record the same state by tip hash."""

        with self.connection:
            self.connection.execute("DELETE FROM utxos")
            self.connection.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES ('active_tip', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (tip_hash,),
            )
            self._save_tip_snapshot(tip_hash, utxos)
            self.connection.executemany(
                """
                INSERT INTO utxos (txid, output_index, amount, address)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        entry.outpoint.txid,
                        entry.outpoint.index,
                        entry.output.amount,
                        entry.output.address,
                    )
                    for entry in utxos.entries()
                ],
            )

    def save_tip_snapshot(self, tip_hash: str, utxos: UTXOSet) -> None:
        with self.connection:
            self._save_tip_snapshot(tip_hash, utxos)

    def save_tip_spends(self, tip_hash: str, blocks: Iterable[ConsensusBlock]) -> None:
        """Index all non-coinbase spends visible on the chain ending at ``tip_hash``."""

        spends: list[IndexedSpend] = []
        for block in blocks:
            for transaction in block.transactions:
                if transaction.is_coinbase:
                    continue
                spends.extend(
                    IndexedSpend(
                        outpoint=tx_input.previous_output,
                        spending_txid=transaction.txid,
                        input_index=input_index,
                    )
                    for input_index, tx_input in enumerate(transaction.inputs)
                )
        with self.connection:
            self.connection.execute("DELETE FROM spent_outpoints WHERE tip_hash = ?", (tip_hash,))
            self.connection.executemany(
                """
                INSERT INTO spent_outpoints (
                    tip_hash,
                    txid,
                    output_index,
                    spending_txid,
                    input_index
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        tip_hash,
                        spend.outpoint.txid,
                        spend.outpoint.index,
                        spend.spending_txid,
                        spend.input_index,
                    )
                    for spend in spends
                ],
            )

    def load_snapshot(self, expected_tip_hash: str) -> UTXOSet | None:
        """Load the active snapshot only when it matches ``expected_tip_hash``."""

        indexed = self.load_tip_snapshot(expected_tip_hash)
        if indexed is not None:
            return indexed
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'active_tip'"
        ).fetchone()
        if row is None or str(row["value"]) != expected_tip_hash:
            return None
        rows = self.connection.execute(
            """
            SELECT txid, output_index, amount, address
            FROM utxos
            ORDER BY txid ASC, output_index ASC
            """
        ).fetchall()
        return UTXOSet(
            UTXO(
                outpoint=OutPoint(str(row["txid"]), int(row["output_index"])),
                output=TxOutput(int(row["amount"]), str(row["address"])),
            )
            for row in rows
        )

    def load_tip_snapshot(self, tip_hash: str) -> UTXOSet | None:
        rows = self.connection.execute(
            """
            SELECT txid, output_index, amount, address
            FROM utxo_snapshots
            WHERE tip_hash = ?
            ORDER BY txid ASC, output_index ASC
            """,
            (tip_hash,),
        ).fetchall()
        if not rows:
            return None
        return UTXOSet(
            UTXO(
                outpoint=OutPoint(str(row["txid"]), int(row["output_index"])),
                output=TxOutput(int(row["amount"]), str(row["address"])),
            )
            for row in rows
        )

    def active_tip(self) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'active_tip'"
        ).fetchone()
        return str(row["value"]) if row else None

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM utxos").fetchone()
        return int(row["count"])

    def tip_count(self, tip_hash: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM utxo_snapshots WHERE tip_hash = ?",
            (tip_hash,),
        ).fetchone()
        return int(row["count"])

    def spent_count(self, tip_hash: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM spent_outpoints WHERE tip_hash = ?",
            (tip_hash,),
        ).fetchone()
        return int(row["count"])

    def load_tip_spends(self, tip_hash: str) -> tuple[IndexedSpend, ...]:
        rows = self.connection.execute(
            """
            SELECT txid, output_index, spending_txid, input_index
            FROM spent_outpoints
            WHERE tip_hash = ?
            ORDER BY txid ASC, output_index ASC
            """,
            (tip_hash,),
        ).fetchall()
        return tuple(
            IndexedSpend(
                outpoint=OutPoint(str(row["txid"]), int(row["output_index"])),
                spending_txid=str(row["spending_txid"]),
                input_index=int(row["input_index"]),
            )
            for row in rows
        )

    def spent_by_tip(self, tip_hash: str, outpoint: OutPoint) -> IndexedSpend | None:
        row = self.connection.execute(
            """
            SELECT spending_txid, input_index
            FROM spent_outpoints
            WHERE tip_hash = ? AND txid = ? AND output_index = ?
            """,
            (tip_hash, outpoint.txid, outpoint.index),
        ).fetchone()
        if row is None:
            return None
        return IndexedSpend(
            outpoint=outpoint,
            spending_txid=str(row["spending_txid"]),
            input_index=int(row["input_index"]),
        )

    def is_spent_at_tip(self, tip_hash: str, outpoint: OutPoint) -> bool:
        return self.spent_by_tip(tip_hash, outpoint) is not None

    def utxos_for_address_at_tip(self, tip_hash: str, address: str) -> tuple[UTXO, ...]:
        rows = self.connection.execute(
            """
            SELECT txid, output_index, amount, address
            FROM utxo_snapshots
            WHERE tip_hash = ? AND address = ?
            ORDER BY txid ASC, output_index ASC
            """,
            (tip_hash, address),
        ).fetchall()
        return tuple(
            UTXO(
                outpoint=OutPoint(str(row["txid"]), int(row["output_index"])),
                output=TxOutput(int(row["amount"]), str(row["address"])),
            )
            for row in rows
        )

    def indexed_tips(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT DISTINCT tip_hash FROM utxo_snapshots ORDER BY tip_hash ASC"
        ).fetchall()
        return tuple(str(row["tip_hash"]) for row in rows)

    def _save_tip_snapshot(self, tip_hash: str, utxos: UTXOSet) -> None:
        self.connection.execute("DELETE FROM utxo_snapshots WHERE tip_hash = ?", (tip_hash,))
        self.connection.executemany(
            """
            INSERT INTO utxo_snapshots (tip_hash, txid, output_index, amount, address)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    tip_hash,
                    entry.outpoint.txid,
                    entry.outpoint.index,
                    entry.output.amount,
                    entry.output.address,
                )
                for entry in utxos.entries()
            ],
        )


class SQLiteMempoolStore:
    """Persistent local mempool for transactions not yet mined.

    The consensus node revalidates these entries on startup before keeping them,
    so this store is a durability cache rather than a source of consensus truth.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mempool_transactions (
                    txid TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                )
                """
            )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata (key, value) VALUES ('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
                return
            if int(row["value"]) != STORE_SCHEMA_VERSION:
                raise StorageError(f"unsupported store schema version: {row['value']}")

    def add_transaction(self, transaction: ConsensusTransaction) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO mempool_transactions (txid, data)
                VALUES (?, ?)
                """,
                (transaction.txid, self._encode(transaction)),
            )

    def replace_all(self, transactions: list[ConsensusTransaction]) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM mempool_transactions")
            self.connection.executemany(
                """
                INSERT INTO mempool_transactions (txid, data)
                VALUES (?, ?)
                """,
                [(transaction.txid, self._encode(transaction)) for transaction in transactions],
            )

    def transactions(self) -> list[ConsensusTransaction]:
        rows = self.connection.execute(
            "SELECT data FROM mempool_transactions ORDER BY txid ASC"
        ).fetchall()
        return [self._decode(str(row["data"])) for row in rows]

    def count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM mempool_transactions"
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def _encode(transaction: ConsensusTransaction) -> str:
        return transaction.to_bytes().hex()

    @staticmethod
    def _decode(payload: str) -> ConsensusTransaction:
        return ConsensusTransaction.from_bytes(bytes.fromhex(payload))


class SQLiteInvalidBlockCache:
    """Persistent cache of blocks known to fail local validation.

    Caching invalid hashes prevents repeated expensive validation of the same
    malformed or disconnected block across restarts.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS invalid_blocks (
                    hash TEXT PRIMARY KEY,
                    reason TEXT NOT NULL
                )
                """
            )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata (key, value) VALUES ('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
                return
            if int(row["value"]) != STORE_SCHEMA_VERSION:
                raise StorageError(f"unsupported store schema version: {row['value']}")

    def mark_invalid(self, block_hash: str, reason: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO invalid_blocks (hash, reason)
                VALUES (?, ?)
                ON CONFLICT(hash) DO UPDATE SET reason = excluded.reason
                """,
                (block_hash, reason),
            )

    def is_invalid(self, block_hash: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM invalid_blocks WHERE hash = ?",
            (block_hash,),
        ).fetchone()
        return row is not None

    def reason(self, block_hash: str) -> str | None:
        row = self.connection.execute(
            "SELECT reason FROM invalid_blocks WHERE hash = ?",
            (block_hash,),
        ).fetchone()
        return str(row["reason"]) if row else None

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM invalid_blocks").fetchone()
        return int(row["count"])


class SQLitePeerSyncStore:
    """Persistent peer sync quality counters.

    The service uses these counters to avoid repeatedly selecting peers that
    time out or fail block downloads after the process restarts.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS peer_sync_progress (
                    address TEXT PRIMARY KEY,
                    requested_blocks INTEGER NOT NULL,
                    completed_blocks INTEGER NOT NULL,
                    timed_out_blocks INTEGER NOT NULL,
                    failed_blocks INTEGER NOT NULL
                )
                """
            )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata (key, value) VALUES ('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
                return
            if int(row["value"]) != STORE_SCHEMA_VERSION:
                raise StorageError(f"unsupported store schema version: {row['value']}")

    def save_progress(self, progress: StoredPeerSyncProgress) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO peer_sync_progress (
                    address,
                    requested_blocks,
                    completed_blocks,
                    timed_out_blocks,
                    failed_blocks
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    requested_blocks = excluded.requested_blocks,
                    completed_blocks = excluded.completed_blocks,
                    timed_out_blocks = excluded.timed_out_blocks,
                    failed_blocks = excluded.failed_blocks
                """,
                (
                    progress.address,
                    progress.requested_blocks,
                    progress.completed_blocks,
                    progress.timed_out_blocks,
                    progress.failed_blocks,
                ),
            )

    def load_progress(self, address: str) -> StoredPeerSyncProgress | None:
        row = self.connection.execute(
            """
            SELECT address, requested_blocks, completed_blocks, timed_out_blocks, failed_blocks
            FROM peer_sync_progress
            WHERE address = ?
            """,
            (address,),
        ).fetchone()
        if row is None:
            return None
        return StoredPeerSyncProgress(
            address=str(row["address"]),
            requested_blocks=int(row["requested_blocks"]),
            completed_blocks=int(row["completed_blocks"]),
            timed_out_blocks=int(row["timed_out_blocks"]),
            failed_blocks=int(row["failed_blocks"]),
        )

    def all_progress(self) -> tuple[StoredPeerSyncProgress, ...]:
        rows = self.connection.execute(
            """
            SELECT address, requested_blocks, completed_blocks, timed_out_blocks, failed_blocks
            FROM peer_sync_progress
            ORDER BY address ASC
            """
        ).fetchall()
        return tuple(
            StoredPeerSyncProgress(
                address=str(row["address"]),
                requested_blocks=int(row["requested_blocks"]),
                completed_blocks=int(row["completed_blocks"]),
                timed_out_blocks=int(row["timed_out_blocks"]),
                failed_blocks=int(row["failed_blocks"]),
            )
            for row in rows
        )
