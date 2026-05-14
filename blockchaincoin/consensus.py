"""Consensus data structures and state-transition rules.

This module is the consensus-critical core for the UTXO node. It defines the
canonical binary encodings used for transaction IDs, Merkle roots, and block
hashes; validates signatures, output values, coinbase rewards, and proof of
work; and applies transactions and blocks to an in-memory UTXO set.

Code outside this module may decide local relay policy, storage layout, or peer
behavior, but it must not redefine whether a transaction or block is valid.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import cast

from .constants import MAX_MONEY
from .crypto import PublicKey, Wallet, canonical_json, is_valid_address, sha256_hex

COINBASE_PREV_TXID = "0" * 64
BINARY_CODEC_VERSION = 1
SUPPORTED_TRANSACTION_VERSIONS = frozenset({1})
SUPPORTED_BLOCK_VERSIONS = frozenset({1})


class ConsensusError(ValueError):
    """Raised when consensus data violates protocol rules."""

    pass


class _ByteReader:
    """Bounds-checked cursor used by the binary consensus codecs."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.payload):
            raise ConsensusError("binary payload is truncated")
        chunk = self.payload[self.offset : end]
        self.offset = end
        return chunk

    def require_finished(self) -> None:
        if self.offset != len(self.payload):
            raise ConsensusError("binary payload has trailing bytes")


def _encode_int(value: int) -> bytes:
    return struct.pack(">q", value)


def _encode_uint(value: int) -> bytes:
    if value < 0:
        raise ConsensusError("unsigned integer cannot be negative")
    return struct.pack(">Q", value)


def _encode_float(value: float) -> bytes:
    return struct.pack(">d", value)


def _encode_bytes(value: bytes) -> bytes:
    if len(value) > 2**32 - 1:
        raise ConsensusError("binary field is too large")
    return struct.pack(">I", len(value)) + value


def _decode_int(reader: _ByteReader) -> int:
    return struct.unpack(">q", reader.read(8))[0]


def _decode_uint(reader: _ByteReader) -> int:
    return struct.unpack(">Q", reader.read(8))[0]


def _decode_float(reader: _ByteReader) -> float:
    return struct.unpack(">d", reader.read(8))[0]


def _decode_bytes(reader: _ByteReader) -> bytes:
    size = struct.unpack(">I", reader.read(4))[0]
    return reader.read(size)


def _decode_text(reader: _ByteReader) -> str:
    try:
        return _decode_bytes(reader).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConsensusError("text field is not valid utf-8") from exc


def _encode_text(value: str) -> bytes:
    return _encode_bytes(value.encode("utf-8"))


def _encode_hash(value: str) -> bytes:
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ConsensusError("hash field is not hexadecimal") from exc
    if len(raw) != 32:
        raise ConsensusError("hash field must be 32 bytes")
    return raw


@dataclass(frozen=True)
class OutPoint:
    """Reference to one transaction output.

    An outpoint is the UTXO model's pointer type: the pair of transaction ID and
    output index uniquely names a spendable coin until another transaction
    consumes it.
    """

    txid: str
    index: int

    def to_dict(self) -> dict[str, object]:
        return {"txid": self.txid, "index": self.index}

    def to_bytes(self) -> bytes:
        return _encode_hash(self.txid) + _encode_int(self.index)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> OutPoint:
        return cls(txid=str(data["txid"]), index=int(cast(str | int, data["index"])))

    @classmethod
    def from_reader(cls, reader: _ByteReader) -> OutPoint:
        return cls(txid=reader.read(32).hex(), index=_decode_int(reader))


@dataclass(frozen=True)
class TxInput:
    """Transaction input that spends a previous output.

    Non-coinbase inputs carry a signature and public key. The signature covers a
    witness-free signing message so each input authorizes the transaction
    outputs without signing its own signature bytes.
    """

    previous_output: OutPoint
    signature: str | None = None
    public_key: dict[str, str] | None = None

    def unsigned_payload(self) -> dict[str, object]:
        return {"previous_output": self.previous_output.to_dict()}

    def to_dict(self, include_witness: bool = True) -> dict[str, object]:
        payload: dict[str, object] = self.unsigned_payload()
        if include_witness:
            payload["signature"] = self.signature
            payload["public_key"] = self.public_key
        return payload

    def to_bytes(self, include_witness: bool = True) -> bytes:
        payload = self.previous_output.to_bytes()
        if not include_witness:
            return payload
        if self.signature is None:
            payload += b"\x00"
        else:
            payload += b"\x01" + _encode_text(self.signature)
        if self.public_key is None:
            return payload + b"\x00"
        return (
            payload
            + b"\x01"
            + _encode_text(str(self.public_key.get("algorithm", "")))
            + _encode_text(str(self.public_key.get("key", "")))
        )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TxInput:
        public_key = None
        if data.get("public_key"):
            public_key = cast(dict[str, str], data["public_key"])
        return cls(
            previous_output=OutPoint.from_dict(cast(dict[str, object], data["previous_output"])),
            signature=str(data["signature"]) if data.get("signature") else None,
            public_key=public_key,
        )

    @classmethod
    def from_reader(cls, reader: _ByteReader, include_witness: bool = True) -> TxInput:
        previous_output = OutPoint.from_reader(reader)
        if not include_witness:
            return cls(previous_output=previous_output)
        signature_present = reader.read(1)
        if signature_present not in {b"\x00", b"\x01"}:
            raise ConsensusError("signature presence flag is invalid")
        signature = _decode_text(reader) if signature_present == b"\x01" else None
        public_key_present = reader.read(1)
        if public_key_present not in {b"\x00", b"\x01"}:
            raise ConsensusError("public key presence flag is invalid")
        public_key = None
        if public_key_present == b"\x01":
            public_key = {
                "algorithm": _decode_text(reader),
                "key": _decode_text(reader),
            }
        return cls(
            previous_output=previous_output,
            signature=signature,
            public_key=public_key,
        )


@dataclass(frozen=True)
class TxOutput:
    """Spendable transaction output assigned to a BlockchainCoin address."""

    amount: int
    address: str

    def to_dict(self) -> dict[str, object]:
        return {"amount": self.amount, "address": self.address}

    def to_bytes(self) -> bytes:
        return _encode_uint(self.amount) + _encode_text(self.address)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TxOutput:
        return cls(amount=int(cast(str | int, data["amount"])), address=str(data["address"]))

    @classmethod
    def from_reader(cls, reader: _ByteReader) -> TxOutput:
        return cls(amount=_decode_uint(reader), address=_decode_text(reader))


@dataclass(frozen=True)
class ConsensusTransaction:
    """Canonical UTXO transaction.

    Transaction IDs are SHA-256 hashes of the canonical binary representation,
    including witness data. Coinbase transactions are represented as one input
    whose previous transaction ID is all zeroes and whose index is negative.
    """

    inputs: tuple[TxInput, ...]
    outputs: tuple[TxOutput, ...]
    version: int = 1

    @property
    def is_coinbase(self) -> bool:
        return (
            len(self.inputs) == 1
            and self.inputs[0].previous_output.txid == COINBASE_PREV_TXID
            and self.inputs[0].previous_output.index < 0
        )

    @property
    def txid(self) -> str:
        return sha256_hex(self.to_bytes())

    def signing_message(self, input_index: int) -> str:
        """Return the deterministic message that an input signature authorizes."""

        return canonical_json(
            {
                "input_index": input_index,
                "inputs": [tx_input.to_dict(include_witness=False) for tx_input in self.inputs],
                "outputs": [output.to_dict() for output in self.outputs],
                "version": self.version,
            }
        )

    def sign_input(self, input_index: int, wallet: Wallet) -> ConsensusTransaction:
        """Return a copy with one input signed by ``wallet``."""

        if input_index < 0 or input_index >= len(self.inputs):
            raise ConsensusError("input index out of range")
        signed_inputs = list(self.inputs)
        tx_input = signed_inputs[input_index]
        signed_inputs[input_index] = TxInput(
            previous_output=tx_input.previous_output,
            signature=wallet.sign(self.signing_message(input_index)),
            public_key=wallet.public_key.to_dict(),
        )
        return ConsensusTransaction(
            inputs=tuple(signed_inputs),
            outputs=self.outputs,
            version=self.version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "inputs": [tx_input.to_dict() for tx_input in self.inputs],
            "outputs": [output.to_dict() for output in self.outputs],
        }

    def to_bytes(self, include_witness: bool = True) -> bytes:
        payload = b"bcctx" + struct.pack(">H", BINARY_CODEC_VERSION) + _encode_int(self.version)
        payload += _encode_uint(len(self.inputs))
        for tx_input in self.inputs:
            payload += tx_input.to_bytes(include_witness=include_witness)
        payload += _encode_uint(len(self.outputs))
        for output in self.outputs:
            payload += output.to_bytes()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ConsensusTransaction:
        inputs = cast(list[dict[str, object]], data.get("inputs", []))
        outputs = cast(list[dict[str, object]], data.get("outputs", []))
        return cls(
            version=int(cast(str | int, data.get("version", 1))),
            inputs=tuple(TxInput.from_dict(item) for item in inputs),
            outputs=tuple(TxOutput.from_dict(item) for item in outputs),
        )

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        include_witness: bool = True,
    ) -> ConsensusTransaction:
        reader = _ByteReader(payload)
        if reader.read(5) != b"bcctx":
            raise ConsensusError("transaction binary magic is invalid")
        version = struct.unpack(">H", reader.read(2))[0]
        if version != BINARY_CODEC_VERSION:
            raise ConsensusError("transaction binary codec version is unsupported")
        transaction_version = _decode_int(reader)
        input_count = _decode_uint(reader)
        inputs = tuple(
            TxInput.from_reader(reader, include_witness=include_witness) for _ in range(input_count)
        )
        output_count = _decode_uint(reader)
        outputs = tuple(TxOutput.from_reader(reader) for _ in range(output_count))
        reader.require_finished()
        return cls(inputs=inputs, outputs=outputs, version=transaction_version)


def calculate_transaction_root(transactions: tuple[ConsensusTransaction, ...]) -> str:
    """Calculate the Merkle root committed by a block header."""

    if not transactions:
        return sha256_hex("")
    layer = [bytes.fromhex(transaction.txid) for transaction in transactions]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [
            bytes.fromhex(sha256_hex(layer[index] + layer[index + 1]))
            for index in range(0, len(layer), 2)
        ]
    return layer[0].hex()


@dataclass(frozen=True)
class ConsensusBlock:
    """Block header and ordered consensus transactions.

    The header commits to height, parent hash, transaction root, difficulty,
    timestamp, nonce, and block version. The transaction root is filled from the
    transaction list when omitted, which keeps hand-built blocks ergonomic while
    preserving explicit root validation on decoded blocks.
    """

    height: int
    previous_hash: str
    transactions: tuple[ConsensusTransaction, ...]
    difficulty: int
    timestamp: float = field(default_factory=time.time)
    nonce: int = 0
    transaction_root: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if not self.transaction_root:
            object.__setattr__(
                self,
                "transaction_root",
                calculate_transaction_root(self.transactions),
            )

    def header(self) -> dict[str, object]:
        return {
            "difficulty": self.difficulty,
            "height": self.height,
            "nonce": self.nonce,
            "previous_hash": self.previous_hash,
            "timestamp": self.timestamp,
            "transaction_root": self.transaction_root,
            "version": self.version,
        }

    @property
    def hash(self) -> str:
        return sha256_hex(self.header_bytes())

    def header_bytes(self) -> bytes:
        return (
            b"bccblk"
            + struct.pack(">H", BINARY_CODEC_VERSION)
            + _encode_int(self.version)
            + _encode_int(self.height)
            + _encode_hash(self.previous_hash)
            + _encode_hash(self.transaction_root)
            + _encode_int(self.difficulty)
            + _encode_float(self.timestamp)
            + _encode_int(self.nonce)
        )

    def to_bytes(self) -> bytes:
        payload = self.header_bytes()
        payload += _encode_uint(len(self.transactions))
        for transaction in self.transactions:
            payload += _encode_bytes(transaction.to_bytes())
        return payload

    def to_dict(self) -> dict[str, object]:
        return {
            **self.header(),
            "hash": self.hash,
            "transactions": [transaction.to_dict() for transaction in self.transactions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ConsensusBlock:
        transactions = cast(list[dict[str, object]], data.get("transactions", []))
        return cls(
            height=int(cast(str | int, data["height"])),
            previous_hash=str(data["previous_hash"]),
            transactions=tuple(
                ConsensusTransaction.from_dict(transaction) for transaction in transactions
            ),
            difficulty=int(cast(str | int, data["difficulty"])),
            timestamp=float(cast(str | int | float, data["timestamp"])),
            nonce=int(cast(str | int, data["nonce"])),
            transaction_root=str(data["transaction_root"]),
            version=int(cast(str | int, data.get("version", 1))),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> ConsensusBlock:
        reader = _ByteReader(payload)
        if reader.read(6) != b"bccblk":
            raise ConsensusError("block binary magic is invalid")
        codec_version = struct.unpack(">H", reader.read(2))[0]
        if codec_version != BINARY_CODEC_VERSION:
            raise ConsensusError("block binary codec version is unsupported")
        block_version = _decode_int(reader)
        height = _decode_int(reader)
        previous_hash = reader.read(32).hex()
        transaction_root = reader.read(32).hex()
        difficulty = _decode_int(reader)
        timestamp = _decode_float(reader)
        nonce = _decode_int(reader)
        transaction_count = _decode_uint(reader)
        transactions = tuple(
            ConsensusTransaction.from_bytes(_decode_bytes(reader)) for _ in range(transaction_count)
        )
        reader.require_finished()
        return cls(
            height=height,
            previous_hash=previous_hash,
            transactions=transactions,
            difficulty=difficulty,
            timestamp=timestamp,
            nonce=nonce,
            transaction_root=transaction_root,
            version=block_version,
        )


@dataclass(frozen=True)
class UTXO:
    """One unspent output and the outpoint that names it."""

    outpoint: OutPoint
    output: TxOutput


class UTXOSet:
    """Mutable set of currently spendable outputs."""

    def __init__(self, entries: Iterable[UTXO] | None = None) -> None:
        self._entries: dict[OutPoint, TxOutput] = {}
        for entry in entries or ():
            self.add(entry.outpoint, entry.output)

    def copy(self) -> UTXOSet:
        """Return an independent snapshot suitable for candidate validation."""

        return UTXOSet(UTXO(outpoint, output) for outpoint, output in self._entries.items())

    def add(self, outpoint: OutPoint, output: TxOutput) -> None:
        """Add a newly created output, rejecting duplicate outpoints."""

        if outpoint in self._entries:
            raise ConsensusError("duplicate utxo")
        self._entries[outpoint] = output

    def spend(self, outpoint: OutPoint) -> TxOutput:
        """Consume an outpoint and return the output it previously held."""

        try:
            return self._entries.pop(outpoint)
        except KeyError as exc:
            raise ConsensusError("missing or already spent output") from exc

    def get(self, outpoint: OutPoint) -> TxOutput:
        try:
            return self._entries[outpoint]
        except KeyError as exc:
            raise ConsensusError("missing output") from exc

    def contains(self, outpoint: OutPoint) -> bool:
        return outpoint in self._entries

    def total(self) -> int:
        return sum(output.amount for output in self._entries.values())

    def entries(self) -> tuple[UTXO, ...]:
        return tuple(
            UTXO(outpoint=outpoint, output=output)
            for outpoint, output in sorted(
                self._entries.items(),
                key=lambda item: (item[0].txid, item[0].index),
            )
        )

    def entries_for_address(self, address: str) -> tuple[UTXO, ...]:
        return tuple(entry for entry in self.entries() if entry.output.address == address)

    def __len__(self) -> int:
        return len(self._entries)


class ConsensusState:
    """Current spendable state plus monetary parameters.

    ``ConsensusState`` is intentionally small and deterministic: callers feed it
    transactions or blocks in order, and it either mutates to the next valid UTXO
    set or raises ``ConsensusError`` without committing a partial update.
    """

    def __init__(
        self,
        utxos: UTXOSet | None = None,
        block_subsidy: int = 50,
        max_money: int = MAX_MONEY,
    ) -> None:
        if block_subsidy < 0:
            raise ConsensusError("block subsidy cannot be negative")
        if max_money <= 0:
            raise ConsensusError("max money must be positive")
        self.utxos = utxos or UTXOSet()
        self.block_subsidy = block_subsidy
        self.max_money = max_money

    def validate_transaction(self, transaction: ConsensusTransaction) -> int:
        """Validate a transaction against the current UTXO set and return its fee."""

        self._validate_transaction_version(transaction)
        self._validate_outputs(transaction.outputs)
        if transaction.is_coinbase:
            return self._validate_coinbase(transaction)
        if not transaction.inputs:
            raise ConsensusError("transaction has no inputs")

        seen_inputs: set[OutPoint] = set()
        input_total = 0
        for input_index, tx_input in enumerate(transaction.inputs):
            if tx_input.previous_output in seen_inputs:
                raise ConsensusError("transaction double-spends an input")
            seen_inputs.add(tx_input.previous_output)
            previous_output = self.utxos.get(tx_input.previous_output)
            input_total += previous_output.amount
            self._validate_input_signature(transaction, input_index, tx_input, previous_output)

        output_total = sum(output.amount for output in transaction.outputs)
        if output_total > input_total:
            raise ConsensusError("transaction spends more than its inputs")
        return input_total - output_total

    def apply_transaction(self, transaction: ConsensusTransaction) -> int:
        """Atomically apply a validated transaction and return its fee."""

        fee = self.validate_transaction(transaction)
        updated = self.utxos.copy()
        if not transaction.is_coinbase:
            for tx_input in transaction.inputs:
                updated.spend(tx_input.previous_output)
        for index, output in enumerate(transaction.outputs):
            updated.add(OutPoint(transaction.txid, index), output)
        if updated.total() > self.max_money:
            raise ConsensusError("utxo set exceeds maximum supply")
        self.utxos = updated
        return fee

    def create_coinbase(
        self,
        miner_address: str,
        fees: int = 0,
        height: int = 0,
    ) -> ConsensusTransaction:
        """Build the coinbase transaction for a candidate block."""

        if not is_valid_address(miner_address):
            raise ConsensusError("invalid miner address")
        if fees < 0:
            raise ConsensusError("fees cannot be negative")
        if height < 0:
            raise ConsensusError("height cannot be negative")
        amount = self.block_subsidy + fees
        if self.utxos.total() + self.block_subsidy > self.max_money:
            raise ConsensusError("coinbase would exceed maximum supply")
        return ConsensusTransaction(
            inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -(height + 1))),),
            outputs=(TxOutput(amount=amount, address=miner_address),),
        )

    def apply_block_coinbase(self, transaction: ConsensusTransaction, fees: int) -> None:
        """Apply a block's first transaction under subsidy-plus-fees rules."""

        self._validate_transaction_version(transaction)
        self._validate_outputs(transaction.outputs)
        if not transaction.is_coinbase:
            raise ConsensusError("block reward transaction must be coinbase")
        if fees < 0:
            raise ConsensusError("fees cannot be negative")
        if (
            transaction.inputs[0].signature is not None
            or transaction.inputs[0].public_key is not None
        ):
            raise ConsensusError("coinbase input must not be signed")
        output_total = sum(output.amount for output in transaction.outputs)
        if output_total > self.block_subsidy + fees:
            raise ConsensusError("coinbase pays more than subsidy plus fees")
        updated = self.utxos.copy()
        for index, output in enumerate(transaction.outputs):
            updated.add(OutPoint(transaction.txid, index), output)
        if updated.total() > self.max_money:
            raise ConsensusError("utxo set exceeds maximum supply")
        self.utxos = updated

    def apply_genesis_coinbase(self, transaction: ConsensusTransaction) -> None:
        """Apply a genesis allocation transaction without normal subsidy limits."""

        self._validate_transaction_version(transaction)
        self._validate_outputs(transaction.outputs)
        if not transaction.is_coinbase:
            raise ConsensusError("genesis transaction must be coinbase")
        if (
            transaction.inputs[0].signature is not None
            or transaction.inputs[0].public_key is not None
        ):
            raise ConsensusError("coinbase input must not be signed")
        updated = self.utxos.copy()
        for index, output in enumerate(transaction.outputs):
            updated.add(OutPoint(transaction.txid, index), output)
        if updated.total() > self.max_money:
            raise ConsensusError("utxo set exceeds maximum supply")
        self.utxos = updated

    def _validate_transaction_version(self, transaction: ConsensusTransaction) -> None:
        if transaction.version not in SUPPORTED_TRANSACTION_VERSIONS:
            raise ConsensusError("transaction version is unsupported")

    def _validate_outputs(self, outputs: tuple[TxOutput, ...]) -> None:
        if not outputs:
            raise ConsensusError("transaction has no outputs")
        for output in outputs:
            if output.amount <= 0:
                raise ConsensusError("output amount must be positive")
            if output.amount > self.max_money:
                raise ConsensusError("output exceeds maximum supply")
            if not is_valid_address(output.address):
                raise ConsensusError("invalid output address")

    def _validate_coinbase(self, transaction: ConsensusTransaction) -> int:
        if len(transaction.inputs) != 1:
            raise ConsensusError("coinbase must have exactly one input")
        if (
            transaction.inputs[0].signature is not None
            or transaction.inputs[0].public_key is not None
        ):
            raise ConsensusError("coinbase input must not be signed")
        output_total = sum(output.amount for output in transaction.outputs)
        if output_total > self.block_subsidy:
            raise ConsensusError("coinbase pays more than subsidy")
        return 0

    def _validate_input_signature(
        self,
        transaction: ConsensusTransaction,
        input_index: int,
        tx_input: TxInput,
        previous_output: TxOutput,
    ) -> None:
        if not tx_input.public_key or not tx_input.signature:
            raise ConsensusError("input is missing witness")
        try:
            public_key = PublicKey.from_dict(tx_input.public_key)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConsensusError("invalid input public key") from exc
        if public_key.address != previous_output.address:
            raise ConsensusError("input public key does not own output")
        from .crypto import verify_signature

        if not verify_signature(
            public_key, transaction.signing_message(input_index), tx_input.signature
        ):
            raise ConsensusError("invalid input signature")


class BlockProcessor:
    """Validates and applies blocks against a ``ConsensusState``."""

    def __init__(self, state: ConsensusState) -> None:
        self.state = state

    def validate_block(
        self,
        block: ConsensusBlock,
        expected_height: int,
        expected_previous_hash: str,
    ) -> int:
        """Validate a block without mutating the processor's state."""

        if block.version not in SUPPORTED_BLOCK_VERSIONS:
            raise ConsensusError("block version is unsupported")
        if block.height != expected_height:
            raise ConsensusError("block height is invalid")
        if block.previous_hash != expected_previous_hash:
            raise ConsensusError("block previous hash is invalid")
        if block.difficulty < 0:
            raise ConsensusError("block difficulty cannot be negative")
        if not block.hash.startswith("0" * block.difficulty):
            raise ConsensusError("block proof of work is invalid")
        if block.transaction_root != calculate_transaction_root(block.transactions):
            raise ConsensusError("block transaction root is invalid")
        if not block.transactions:
            raise ConsensusError("block has no transactions")
        if not block.transactions[0].is_coinbase:
            raise ConsensusError("first transaction must be coinbase")
        if any(transaction.is_coinbase for transaction in block.transactions[1:]):
            raise ConsensusError("block has multiple coinbase transactions")

        candidate = ConsensusState(
            utxos=self.state.utxos.copy(),
            block_subsidy=self.state.block_subsidy,
            max_money=self.state.max_money,
        )
        fees = 0
        for transaction in block.transactions[1:]:
            fees += candidate.apply_transaction(transaction)

        if block.height == 0 and block.previous_hash == "0" * 64:
            candidate.apply_genesis_coinbase(block.transactions[0])
        else:
            candidate.apply_block_coinbase(block.transactions[0], fees)
        return fees

    def apply_block(
        self,
        block: ConsensusBlock,
        expected_height: int,
        expected_previous_hash: str,
    ) -> int:
        """Validate and atomically apply a block, returning collected fees."""

        fees = self.validate_block(block, expected_height, expected_previous_hash)
        for transaction in block.transactions[1:]:
            self.state.apply_transaction(transaction)
        if block.height == 0 and block.previous_hash == "0" * 64:
            self.state.apply_genesis_coinbase(block.transactions[0])
        else:
            self.state.apply_block_coinbase(block.transactions[0], fees)
        return fees


def rebuild_state_from_blocks(
    blocks: Iterable[ConsensusBlock],
    block_subsidy: int = 50,
    max_money: int = MAX_MONEY,
) -> ConsensusState:
    """Replay an ordered chain into a fresh consensus state."""

    state = ConsensusState(block_subsidy=block_subsidy, max_money=max_money)
    processor = BlockProcessor(state)
    expected_previous_hash = "0" * 64
    for expected_height, block in enumerate(blocks):
        processor.apply_block(block, expected_height, expected_previous_hash)
        expected_previous_hash = block.hash
    return state
