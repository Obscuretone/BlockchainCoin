"""Release manifest, digest, and detached-signature helpers."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .crypto import PublicKey, Wallet, canonical_json, sha256_hex, verify_signature


@dataclass(frozen=True)
class ReleaseArtifact:
    """One built artifact and its reproducible verification metadata."""

    filename: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class ReleaseManifest:
    """Signed manifest payload for a set of release artifacts."""

    project: str
    version: str
    python: str
    artifacts: tuple[ReleaseArtifact, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "project": self.project,
            "python": self.python,
            "version": self.version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class ReleaseSignature:
    """Detached release manifest signature and signing public key."""

    public_key: PublicKey
    signature: str

    @property
    def public_key_fingerprint(self) -> str:
        return release_public_key_fingerprint(self.public_key)

    def to_dict(self) -> dict[str, object]:
        return {
            "public_key": self.public_key.to_dict(),
            "public_key_fingerprint": self.public_key_fingerprint,
            "signature": self.signature,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ReleaseSignature:
        public_key = data["public_key"]
        if not isinstance(public_key, dict):
            raise ValueError("release signature public key is invalid")
        signature = cls(
            public_key=PublicKey.from_dict(public_key), signature=str(data["signature"])
        )
        expected_fingerprint = data.get("public_key_fingerprint")
        if (
            expected_fingerprint is not None
            and str(expected_fingerprint) != signature.public_key_fingerprint
        ):
            raise ValueError("release signature public key fingerprint is invalid")
        return signature


def sha256_file(path: str | Path) -> str:
    """Hash a release artifact without loading the whole file into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release_manifest(
    artifacts: list[str | Path],
    project: str = "blockchaincoin",
    version: str = "0.1.0",
    python: str | None = None,
) -> ReleaseManifest:
    """Create a deterministic manifest from build artifacts."""

    resolved = [Path(artifact) for artifact in artifacts]
    entries = tuple(
        ReleaseArtifact(
            filename=path.name,
            size=path.stat().st_size,
            sha256=sha256_file(path),
        )
        for path in sorted(resolved, key=lambda item: item.name)
    )
    runtime = (
        python or f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    return ReleaseManifest(
        project=project,
        version=version,
        python=runtime,
        artifacts=entries,
    )


def release_public_key_fingerprint(public_key: PublicKey) -> str:
    """Return the canonical fingerprint used to pin a release signing key."""

    return sha256_hex(canonical_json(public_key.to_dict()))


def sign_release_manifest(manifest: ReleaseManifest, wallet: Wallet) -> ReleaseSignature:
    """Sign a manifest with a wallet used as a release signing key."""

    return ReleaseSignature(
        public_key=wallet.public_key,
        signature=wallet.sign(manifest.to_json()),
    )


def verify_release_manifest_signature(
    manifest: ReleaseManifest,
    signature: ReleaseSignature,
    expected_public_key_fingerprint: str | None = None,
) -> bool:
    """Verify a detached manifest signature and optional key fingerprint."""

    if (
        expected_public_key_fingerprint is not None
        and signature.public_key_fingerprint != expected_public_key_fingerprint
    ):
        return False
    return verify_signature(signature.public_key, manifest.to_json(), signature.signature)
