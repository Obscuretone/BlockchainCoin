import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blockchaincoin import Wallet
from blockchaincoin.crypto import (
    PublicKey,
    atomic_write_json,
    b64decode,
    b64encode,
    is_valid_address,
    sha256_hex,
    verify_signature,
)


class CryptoHardeningTests(unittest.TestCase):
    def test_hash_base64_address_and_atomic_write_helpers(self) -> None:
        self.assertEqual(sha256_hex(b"abc"), sha256_hex("abc"))
        encoded = b64encode(b"release-grade")
        self.assertEqual(b64decode(encoded), b"release-grade")
        self.assertFalse(is_valid_address("not_bcc"))
        self.assertFalse(is_valid_address("bcc_" + "0" * 39))
        self.assertFalse(is_valid_address("bcc_" + "g" * 40))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "payload.json"
            atomic_write_json(path, {"b": 1, "a": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2, "b": 1})

    def test_wallet_roundtrip_validation_and_signature_failures(self) -> None:
        wallet = Wallet.create(bits=256)
        payload = wallet.to_dict()
        self.assertEqual(Wallet.from_dict(payload).address, wallet.address)

        with self.assertRaises(ValueError):
            Wallet.create(bits=255)

        bad_schema = dict(payload)
        bad_schema["schema_version"] = 999
        with self.assertRaises(ValueError):
            Wallet.from_dict(bad_schema)

        other = Wallet.create()
        mismatched = dict(payload)
        mismatched["private_key"] = other.private_key
        with self.assertRaises(ValueError):
            Wallet.from_dict(mismatched)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wallet.json"
            wallet.save(path)
            self.assertEqual(Wallet.load(path).address, wallet.address)
            encrypted = Path(tmp) / "encrypted-wallet.json"
            wallet.save(encrypted, passphrase="correct horse battery staple")
            encrypted_payload = json.loads(encrypted.read_text(encoding="utf-8"))
            self.assertTrue(encrypted_payload["encrypted"])
            self.assertNotIn("private_key", encrypted_payload)
            self.assertEqual(
                Wallet.load(encrypted, passphrase="correct horse battery staple").address,
                wallet.address,
            )
            with self.assertRaises(ValueError):
                Wallet.load(encrypted)
            with self.assertRaises(ValueError):
                Wallet.load(encrypted, passphrase="wrong")
            malformed = Path(tmp) / "malformed-wallet.json"
            malformed.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                Wallet.load(malformed)
            bad_encryption = dict(encrypted_payload)
            bad_encryption["encryption"] = "rot13"
            atomic_write_json(malformed, bad_encryption)
            with self.assertRaises(ValueError):
                Wallet.load(malformed, passphrase="correct horse battery staple")
            bad_kdf = dict(encrypted_payload)
            bad_kdf["kdf"] = "none"
            atomic_write_json(malformed, bad_kdf)
            with self.assertRaises(ValueError):
                Wallet.load(malformed, passphrase="correct horse battery staple")
            bad_iterations = dict(encrypted_payload)
            bad_iterations["iterations"] = 0
            atomic_write_json(malformed, bad_iterations)
            with self.assertRaises(ValueError):
                Wallet.load(malformed, passphrase="correct horse battery staple")
            bad_ciphertext = dict(encrypted_payload)
            bad_ciphertext["ciphertext"] = "not-valid-base64"
            atomic_write_json(malformed, bad_ciphertext)
            with self.assertRaises(ValueError):
                Wallet.load(malformed, passphrase="correct horse battery staple")
            with (
                patch("blockchaincoin.crypto.AESGCM.decrypt", side_effect=ValueError),
                self.assertRaises(ValueError),
            ):
                Wallet.load(encrypted, passphrase="correct horse battery staple")
            with (
                patch("blockchaincoin.crypto.AESGCM.decrypt", return_value=b"not-json"),
                self.assertRaises(ValueError),
            ):
                Wallet.load(encrypted, passphrase="correct horse battery staple")
            with (
                patch("blockchaincoin.crypto.AESGCM.decrypt", return_value=b"[]"),
                self.assertRaises(ValueError),
            ):
                Wallet.load(encrypted, passphrase="correct horse battery staple")
            with self.assertRaises(ValueError):
                wallet.encrypted_dict("")

        self.assertTrue(verify_signature(wallet.public_key, "message", wallet.sign("message")))
        self.assertFalse(verify_signature(wallet.public_key, "message", wallet.sign("other")))
        self.assertFalse(verify_signature(wallet.public_key, "message", "not-base64"))
        unsupported = PublicKey(algorithm="future", key=wallet.public_key.key)
        self.assertFalse(verify_signature(unsupported, "message", wallet.sign("message")))

    def test_wallet_save_ignores_chmod_failures(self) -> None:
        wallet = Wallet.create()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wallet.json"
            with patch("blockchaincoin.crypto.Path.chmod", side_effect=OSError):
                wallet.save(path)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
