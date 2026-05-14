"""Legacy account-ledger data models.

The UTXO modules are the consensus surface for the academic release. These
models remain documented because the CLI still exposes compatibility workflows,
and reviewers should be able to distinguish the legacy account ledger from the
UTXO implementation without reading tests first.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .crypto import PublicKey, canonical_json, sha256_hex, verify_signature

if TYPE_CHECKING:
    from .crypto import Wallet

COINBASE = "COINBASE"


@dataclass
class Transaction:
    """Account-ledger transaction used by the compatibility chain.

    Unlike ``ConsensusTransaction`` in the UTXO layer, this object uses sender,
    recipient, amount, fee, and nonce fields. It is useful for local examples and
    backwards-compatible CLI behavior, but it is not the primary research
    consensus transaction type.
    """

    sender: str
    recipient: str
    amount: int
    fee: int = 0
    nonce: int = 0
    signature: str | None = None
    public_key: dict[str, str] | None = None

    def unsigned_payload(self) -> dict[str, object]:
        """Return the deterministic fields covered by the sender signature."""

        return {
            "amount": self.amount,
            "fee": self.fee,
            "nonce": self.nonce,
            "recipient": self.recipient,
            "sender": self.sender,
        }

    def signing_message(self) -> str:
        """Return canonical JSON for signature creation and verification."""

        return canonical_json(self.unsigned_payload())

    @property
    def txid(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    @property
    def is_coinbase(self) -> bool:
        return self.sender == COINBASE

    def sign_with(self, wallet: Wallet) -> None:
        """Attach a wallet signature to this mutable compatibility transaction."""

        from .crypto import Wallet

        if not isinstance(wallet, Wallet):
            raise TypeError("wallet must be a Wallet")
        if wallet.address != self.sender:
            raise ValueError("wallet address does not match transaction sender")
        self.public_key = wallet.public_key.to_dict()
        self.signature = wallet.sign(self.signing_message())

    def has_valid_signature(self) -> bool:
        """Check signature ownership without raising on malformed witness data."""

        if self.is_coinbase:
            return True
        if not self.public_key or not self.signature:
            return False
        try:
            key = PublicKey.from_dict(self.public_key)
        except (KeyError, TypeError, ValueError):
            return False
        if key.address != self.sender:
            return False
        return verify_signature(key, self.signing_message(), self.signature)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transaction:
        return cls(
            sender=str(data["sender"]),
            recipient=str(data["recipient"]),
            amount=int(data["amount"]),
            fee=int(data.get("fee", 0)),
            nonce=int(data.get("nonce", 0)),
            signature=data.get("signature") if data.get("signature") else None,
            public_key=dict(data["public_key"]) if data.get("public_key") else None,
        )


@dataclass
class Block:
    """Account-ledger block used by compatibility storage and CLI commands."""

    index: int
    previous_hash: str
    transactions: list[Transaction]
    difficulty: int
    timestamp: float = field(default_factory=time.time)
    nonce: int = 0
    merkle_root: str = ""

    def __post_init__(self) -> None:
        if not self.merkle_root:
            self.merkle_root = calculate_merkle_root([tx.txid for tx in self.transactions])

    def header(self) -> dict[str, object]:
        """Return the header fields committed by the compatibility block hash."""

        return {
            "difficulty": self.difficulty,
            "index": self.index,
            "merkle_root": self.merkle_root,
            "nonce": self.nonce,
            "previous_hash": self.previous_hash,
            "timestamp": self.timestamp,
        }

    @property
    def hash(self) -> str:
        return sha256_hex(canonical_json(self.header()))

    def to_dict(self) -> dict[str, object]:
        return {
            **self.header(),
            "hash": self.hash,
            "transactions": [tx.to_dict() for tx in self.transactions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Block:
        return cls(
            index=int(data["index"]),
            previous_hash=str(data["previous_hash"]),
            transactions=[Transaction.from_dict(tx) for tx in data.get("transactions", [])],
            difficulty=int(data["difficulty"]),
            timestamp=float(data["timestamp"]),
            nonce=int(data["nonce"]),
            merkle_root=str(data["merkle_root"]),
        )


def calculate_merkle_root(txids: list[str]) -> str:
    """Calculate the legacy block Merkle root from transaction IDs."""

    if not txids:
        return sha256_hex("")
    layer = txids[:]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [sha256_hex(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]
