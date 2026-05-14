import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from blockchaincoin import Wallet
from blockchaincoin.release import (
    ReleaseArtifact,
    ReleaseManifest,
    ReleaseSignature,
    build_release_manifest,
    release_public_key_fingerprint,
    sha256_file,
    sign_release_manifest,
    verify_release_manifest_signature,
)


class ReleaseManifestTests(unittest.TestCase):
    def test_sha256_file_and_manifest_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / "blockchaincoin-0.1.0-py3-none-any.whl"
            source = root / "blockchaincoin-0.1.0.tar.gz"
            wheel.write_bytes(b"wheel")
            source.write_bytes(b"source")

            self.assertEqual(sha256_file(wheel), hashlib.sha256(b"wheel").hexdigest())

            manifest = build_release_manifest(
                [wheel, source],
                project="bcc",
                version="1.2.3",
                python="3.13.1",
            )

            self.assertEqual(manifest.project, "bcc")
            self.assertEqual(manifest.version, "1.2.3")
            self.assertEqual(manifest.python, "3.13.1")
            self.assertEqual(
                [artifact.filename for artifact in manifest.artifacts],
                ["blockchaincoin-0.1.0-py3-none-any.whl", "blockchaincoin-0.1.0.tar.gz"],
            )
            self.assertEqual(
                manifest.to_dict(),
                {
                    "artifacts": [
                        {
                            "filename": wheel.name,
                            "sha256": hashlib.sha256(b"wheel").hexdigest(),
                            "size": 5,
                        },
                        {
                            "filename": source.name,
                            "sha256": hashlib.sha256(b"source").hexdigest(),
                            "size": 6,
                        },
                    ],
                    "project": "bcc",
                    "python": "3.13.1",
                    "version": "1.2.3",
                },
            )
            self.assertEqual(json.loads(manifest.to_json()), manifest.to_dict())
            self.assertTrue(manifest.to_json().endswith("\n"))

    def test_manifest_default_python_and_dataclass_dicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact.bin"
            artifact.write_bytes(b"x")

            manifest = build_release_manifest([artifact])

            self.assertEqual(manifest.project, "blockchaincoin")
            self.assertEqual(manifest.version, "0.1.0")
            self.assertRegex(manifest.python, r"^\d+\.\d+\.\d+$")
            self.assertEqual(
                ReleaseArtifact("artifact.bin", 1, hashlib.sha256(b"x").hexdigest()).to_dict(),
                manifest.artifacts[0].to_dict(),
            )
            self.assertEqual(
                ReleaseManifest("p", "v", "py", ()).to_dict(),
                {"artifacts": [], "project": "p", "python": "py", "version": "v"},
            )

    def test_missing_artifact_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.whl"

            with self.assertRaises(FileNotFoundError):
                build_release_manifest([missing])

    def test_release_manifest_signature_verifies_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact.bin"
            artifact.write_bytes(b"x")
            manifest = build_release_manifest([artifact])
            wallet = Wallet.create()

            signature = sign_release_manifest(manifest, wallet)
            fingerprint = release_public_key_fingerprint(wallet.public_key)

            self.assertTrue(verify_release_manifest_signature(manifest, signature))
            self.assertTrue(
                verify_release_manifest_signature(
                    manifest,
                    signature,
                    expected_public_key_fingerprint=fingerprint,
                )
            )
            self.assertFalse(
                verify_release_manifest_signature(
                    manifest,
                    signature,
                    expected_public_key_fingerprint="0" * 64,
                )
            )
            self.assertEqual(signature.public_key_fingerprint, fingerprint)
            self.assertEqual(json.loads(signature.to_json()), signature.to_dict())
            self.assertEqual(
                ReleaseSignature.from_dict(signature.to_dict()).to_dict(),
                signature.to_dict(),
            )
            with self.assertRaises(ValueError):
                ReleaseSignature.from_dict({"public_key": "bad", "signature": "sig"})
            tampered_signature_payload = signature.to_dict()
            tampered_signature_payload["public_key_fingerprint"] = "0" * 64
            with self.assertRaises(ValueError):
                ReleaseSignature.from_dict(tampered_signature_payload)
            tampered = ReleaseManifest(
                project=manifest.project,
                version="9.9.9",
                python=manifest.python,
                artifacts=manifest.artifacts,
            )
            self.assertFalse(verify_release_manifest_signature(tampered, signature))


if __name__ == "__main__":
    unittest.main()
