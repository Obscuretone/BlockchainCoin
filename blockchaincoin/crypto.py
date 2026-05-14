"""Wallet, address, signature, and canonical hashing helpers.

The academic release keeps cryptographic helpers intentionally small and
reviewable: Ed25519 keys for signatures, SHA-256 for identifiers and release
fingerprints, canonical JSON for signed structured data, and AES-GCM encrypted
wallet files derived from passphrases with PBKDF2-SHA256.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

ADDRESS_PREFIX = "bcc_"
KEY_ALGORITHM = "ed25519"
WALLET_SCHEMA_VERSION = 1
WALLET_KDF = "pbkdf2-sha256"
WALLET_ENCRYPTION = "aes-256-gcm"
WALLET_KDF_ITERATIONS = 600_000


def sha256_hex(data: bytes | str) -> str:
    """Return a lowercase SHA-256 digest for bytes or UTF-8 text."""

    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> str:
    """Serialize structured data deterministically for hashes and signatures."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def b64encode(data: bytes) -> str:
    """Encode bytes as unpadded URL-safe base64 text."""

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64decode(data: str) -> bytes:
    """Decode unpadded URL-safe base64 text."""

    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def atomic_write_json(path: str | Path, payload: object) -> None:
    """Write JSON via a temporary file and atomic rename."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        "wb",
        delete=False,
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(encoded)
        handle.write(b"\n")
        temp_name = handle.name
    os.replace(temp_name, target)


def _derive_wallet_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if not passphrase:
        raise ValueError("wallet passphrase is required")
    if iterations <= 0:
        raise ValueError("wallet KDF iterations must be positive")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    ).derive(passphrase.encode("utf-8"))


def _encrypt_wallet_payload(payload: dict[str, object], passphrase: str) -> dict[str, object]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_wallet_key(passphrase, salt, WALLET_KDF_ITERATIONS)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        b"blockchaincoin-wallet-v1",
    )
    return {
        "schema_version": WALLET_SCHEMA_VERSION,
        "encrypted": True,
        "encryption": WALLET_ENCRYPTION,
        "kdf": WALLET_KDF,
        "iterations": WALLET_KDF_ITERATIONS,
        "salt": b64encode(salt),
        "nonce": b64encode(nonce),
        "ciphertext": b64encode(ciphertext),
    }


def _decrypt_wallet_payload(data: dict[str, Any], passphrase: str | None) -> dict[str, Any]:
    if passphrase is None:
        raise ValueError("wallet passphrase is required")
    if data.get("encryption") != WALLET_ENCRYPTION:
        raise ValueError("unsupported wallet encryption")
    if data.get("kdf") != WALLET_KDF:
        raise ValueError("unsupported wallet key derivation")
    iterations = int(data["iterations"])
    key = _derive_wallet_key(passphrase, b64decode(str(data["salt"])), iterations)
    try:
        plaintext = AESGCM(key).decrypt(
            b64decode(str(data["nonce"])),
            b64decode(str(data["ciphertext"])),
            b"blockchaincoin-wallet-v1",
        )
    except InvalidTag as exc:
        raise ValueError("wallet passphrase is invalid") from exc
    except ValueError as exc:
        raise ValueError("encrypted wallet payload is invalid") from exc
    try:
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("encrypted wallet payload is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("encrypted wallet payload is invalid")
    return decoded


def is_valid_address(address: str) -> bool:
    """Return whether an address has the expected BlockchainCoin shape."""

    if not address.startswith(ADDRESS_PREFIX):
        return False
    suffix = address[len(ADDRESS_PREFIX) :]
    return len(suffix) == 40 and all(char in "0123456789abcdef" for char in suffix)


@dataclass(frozen=True)
class PublicKey:
    """Serializable Ed25519 public key and derived address."""

    algorithm: str
    key: str

    @classmethod
    def create(cls, public_key: Ed25519PublicKey) -> PublicKey:
        """Encode an Ed25519 public key into the wallet/public-key schema."""

        raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return cls(algorithm=KEY_ALGORITHM, key=b64encode(raw))

    @property
    def address(self) -> str:
        """Return the deterministic `bcc_` address for this public key."""

        return f"{ADDRESS_PREFIX}{sha256_hex(self.key)[:40]}"

    def material(self) -> Ed25519PublicKey:
        """Decode the public key into a cryptography verification object."""

        if self.algorithm != KEY_ALGORITHM:
            raise ValueError(f"unsupported public key algorithm: {self.algorithm}")
        return Ed25519PublicKey.from_public_bytes(b64decode(self.key))

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublicKey:
        return cls(algorithm=str(data["algorithm"]), key=str(data["key"]))


@dataclass(frozen=True)
class Wallet:
    """Ed25519 wallet containing a public key and base64 private key material."""

    public_key: PublicKey
    private_key: str

    @property
    def address(self) -> str:
        return self.public_key.address

    @classmethod
    def create(cls, bits: int | None = None) -> Wallet:
        """Generate a new Ed25519 wallet.

        ``bits`` exists for callers that want to assert key size explicitly.
        Ed25519 key size is fixed, so any provided value must be 256.
        """

        if bits is not None and bits != 256:
            raise ValueError("Ed25519 wallets use fixed 256-bit private keys")
        private_key = Ed25519PrivateKey.generate()
        raw_private = private_key.private_bytes(
            Encoding.Raw,
            PrivateFormat.Raw,
            NoEncryption(),
        )
        return cls(
            public_key=PublicKey.create(private_key.public_key()),
            private_key=b64encode(raw_private),
        )

    def sign(self, message: str) -> str:
        """Sign UTF-8 text and return an unpadded URL-safe base64 signature."""

        private_key = Ed25519PrivateKey.from_private_bytes(b64decode(self.private_key))
        return b64encode(private_key.sign(message.encode("utf-8")))

    def to_dict(self) -> dict[str, object]:
        """Return the plaintext wallet serialization payload."""

        return {
            "schema_version": WALLET_SCHEMA_VERSION,
            "public_key": self.public_key.to_dict(),
            "private_key": self.private_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Wallet:
        """Load a plaintext wallet and verify private/public key consistency."""

        schema_version = int(data.get("schema_version", 0))
        if schema_version != WALLET_SCHEMA_VERSION:
            raise ValueError(f"unsupported wallet schema version: {schema_version}")
        wallet = cls(
            public_key=PublicKey.from_dict(data["public_key"]),
            private_key=str(data["private_key"]),
        )
        derived_public = Ed25519PrivateKey.from_private_bytes(
            b64decode(wallet.private_key)
        ).public_key()
        if PublicKey.create(derived_public) != wallet.public_key:
            raise ValueError("wallet private key does not match public key")
        return wallet

    def encrypted_dict(self, passphrase: str) -> dict[str, object]:
        """Return an encrypted wallet file payload."""

        return _encrypt_wallet_payload(self.to_dict(), passphrase)

    def save(self, path: str | Path, passphrase: str | None = None) -> None:
        """Persist a plaintext or passphrase-encrypted wallet file."""

        payload = self.encrypted_dict(passphrase) if passphrase is not None else self.to_dict()
        atomic_write_json(path, payload)
        with suppress(OSError):
            Path(path).chmod(0o600)

    @classmethod
    def load(cls, path: str | Path, passphrase: str | None = None) -> Wallet:
        """Load a plaintext or encrypted wallet file."""

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("wallet file must contain an object")
        if payload.get("encrypted") is True:
            payload = _decrypt_wallet_payload(payload, passphrase)
        return cls.from_dict(payload)


def verify_signature(public_key: PublicKey, message: str, signature: str) -> bool:
    """Return whether an Ed25519 signature verifies for UTF-8 text."""

    try:
        public_key.material().verify(b64decode(signature), message.encode("utf-8"))
    except (InvalidSignature, ValueError):
        return False
    return True
