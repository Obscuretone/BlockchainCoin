import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

from blockchaincoin import Blockchain, Wallet, cli
from blockchaincoin.chain import CHAIN_SCHEMA_VERSION, MAX_MONEY, BlockchainError
from blockchaincoin.crypto import (
    PublicKey,
    atomic_write_json,
    b64decode,
    b64encode,
    is_valid_address,
    sha256_hex,
    verify_signature,
)
from blockchaincoin.models import COINBASE, Block, Transaction, calculate_merkle_root


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


class ModelHardeningTests(unittest.TestCase):
    def test_transaction_signing_and_signature_edge_cases(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        tx = Transaction(alice.address, bob.address, 1)

        with self.assertRaises(TypeError):
            tx.sign_with(cast(Wallet, object()))

        with self.assertRaises(ValueError):
            tx.sign_with(bob)

        self.assertFalse(tx.has_valid_signature())
        tx.public_key = {"bad": "shape"}
        tx.signature = "abc"
        self.assertFalse(tx.has_valid_signature())
        tx.public_key = bob.public_key.to_dict()
        tx.signature = bob.sign(tx.signing_message())
        self.assertFalse(tx.has_valid_signature())

        tx.sign_with(alice)
        restored = Transaction.from_dict(tx.to_dict())
        self.assertEqual(restored.txid, tx.txid)
        self.assertTrue(restored.has_valid_signature())

        coinbase = Transaction(COINBASE, alice.address, 10)
        self.assertTrue(coinbase.has_valid_signature())
        self.assertTrue(coinbase.is_coinbase)

    def test_block_and_merkle_roundtrip(self) -> None:
        wallet = Wallet.create()
        txs = [
            Transaction(COINBASE, wallet.address, 1),
            Transaction(COINBASE, wallet.address, 2),
            Transaction(COINBASE, wallet.address, 3),
        ]
        root = calculate_merkle_root([tx.txid for tx in txs])
        self.assertEqual(calculate_merkle_root([]), sha256_hex(""))
        block = Block(0, "0" * 64, txs, difficulty=0)
        self.assertEqual(block.merkle_root, root)
        restored = Block.from_dict(block.to_dict())
        self.assertEqual(restored.hash, block.hash)


class ChainHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Wallet.create()
        self.bob = Wallet.create()

    def test_constructor_and_genesis_validation(self) -> None:
        with self.assertRaises(BlockchainError):
            Blockchain(difficulty=-1)
        with self.assertRaises(BlockchainError):
            Blockchain(mining_reward=-1)
        with self.assertRaises(BlockchainError):
            Blockchain.create({"bad": 1})
        with self.assertRaises(BlockchainError):
            Blockchain.create({self.alice.address: 0})
        with self.assertRaises(BlockchainError):
            Blockchain.create({self.alice.address: MAX_MONEY + 1})
        with self.assertRaises(BlockchainError):
            _ = Blockchain().last_block

    def test_nonce_balances_and_pending_mining(self) -> None:
        chain = Blockchain.create({self.alice.address: 100}, difficulty=0, mining_reward=5)
        tx = chain.create_transaction(self.alice, self.bob.address, 10, 1)
        chain.add_transaction(tx)

        self.assertEqual(chain.next_nonce(self.alice.address, include_mempool=False), 0)
        self.assertEqual(chain.next_nonce(self.alice.address), 1)
        self.assertEqual(chain.next_nonce(self.bob.address), 0)
        self.assertEqual(chain.balance_of(self.alice.address, include_mempool=True), 89)
        self.assertEqual(chain.circulating_supply(), 100)

        block = chain.mine_pending_transactions(self.alice.address)
        self.assertEqual(block.index, 1)
        self.assertEqual(chain.next_nonce(self.alice.address), 1)

    def test_transaction_validation_errors(self) -> None:
        chain = Blockchain.create({self.alice.address: 100}, difficulty=0)

        with self.assertRaises(TypeError):
            chain.create_transaction(cast(Wallet, object()), self.bob.address, 1)

        cases = [
            Transaction(COINBASE, self.bob.address, 1),
            Transaction(self.alice.address, self.bob.address, 0),
            Transaction(self.alice.address, self.bob.address, 1, fee=-1),
            Transaction(self.alice.address, self.bob.address, MAX_MONEY + 1),
            Transaction("bad", self.bob.address, 1),
            Transaction(self.alice.address, "bad", 1),
            Transaction(self.alice.address, self.alice.address, 1),
            Transaction(self.alice.address, self.bob.address, 1, nonce=-1),
            Transaction(self.alice.address, self.bob.address, 1, nonce=1),
        ]
        for tx in cases:
            if (
                tx.sender == self.alice.address
                and tx.recipient == self.bob.address
                and tx.amount > 0
            ):
                tx.sign_with(self.alice)
            with self.assertRaises(BlockchainError):
                chain.add_transaction(tx)

        unsigned = Transaction(self.alice.address, self.bob.address, 1)
        with self.assertRaises(BlockchainError):
            chain.add_transaction(unsigned)

        with self.assertRaises(BlockchainError):
            chain._validate_transaction(
                Transaction(COINBASE, self.bob.address, 1),
                balances={},
                expected_nonce=0,
                allow_coinbase=False,
            )
        chain._validate_transaction(
            Transaction(COINBASE, self.bob.address, 1),
            balances={},
            expected_nonce=0,
            allow_coinbase=True,
        )

    def test_mining_and_block_validation_errors(self) -> None:
        chain = Blockchain.create({self.alice.address: 100}, difficulty=0)
        with self.assertRaises(BlockchainError):
            chain.mine_pending_transactions("bad")

        empty = Block(1, chain.last_block.hash, [], difficulty=0)
        with self.assertRaises(BlockchainError):
            chain._validate_block(empty)

        invalid_merkle = Block(
            1,
            chain.last_block.hash,
            [Transaction(COINBASE, self.alice.address, 1)],
            difficulty=0,
            merkle_root="bad",
        )
        with self.assertRaises(BlockchainError):
            chain._validate_block(invalid_merkle)

        two_coinbases = Block(
            1,
            chain.last_block.hash,
            [
                Transaction(COINBASE, self.alice.address, 50),
                Transaction(COINBASE, self.bob.address, 1),
            ],
            difficulty=0,
        )
        with self.assertRaises(BlockchainError):
            chain._validate_block(two_coinbases)

        tx = chain.create_transaction(self.alice, self.bob.address, 1, 0)
        first_not_coinbase = Block(1, chain.last_block.hash, [tx], difficulty=0)
        with self.assertRaises(BlockchainError):
            chain._validate_block(first_not_coinbase)

        bad_reward = Block(
            1,
            chain.last_block.hash,
            [Transaction(COINBASE, self.alice.address, 1)],
            difficulty=0,
        )
        with self.assertRaises(BlockchainError):
            chain._validate_block(bad_reward)

        bad_coinbase = Transaction(COINBASE, "bad", 50)
        with self.assertRaises(BlockchainError):
            chain._validate_coinbase(bad_coinbase)
        with self.assertRaises(BlockchainError):
            chain._validate_coinbase(Transaction(COINBASE, self.alice.address, -1))
        with self.assertRaises(BlockchainError):
            chain._validate_coinbase(Transaction(COINBASE, self.alice.address, MAX_MONEY + 1))
        signed_coinbase = Transaction(COINBASE, self.alice.address, 50, signature="sig")
        with self.assertRaises(BlockchainError):
            chain._validate_coinbase(signed_coinbase)
        with self.assertRaises(BlockchainError):
            chain._validate_coinbase(Transaction(self.alice.address, self.bob.address, 1))

        capped = Blockchain.create({self.alice.address: MAX_MONEY}, difficulty=0)
        with self.assertRaises(BlockchainError):
            capped.mine_pending_transactions(self.alice.address)

        oversized_genesis_block = Block(
            0,
            "0" * 64,
            [
                Transaction(COINBASE, self.alice.address, MAX_MONEY),
                Transaction(COINBASE, self.bob.address, 1),
            ],
            difficulty=0,
        )
        with self.assertRaises(BlockchainError):
            Blockchain(difficulty=0)._validate_block(oversized_genesis_block)

    def test_chain_validity_detects_tampering(self) -> None:
        chain = Blockchain.create({self.alice.address: 100}, difficulty=0)
        tx = chain.create_transaction(self.alice, self.bob.address, 1, 0)
        chain.add_transaction(tx)
        chain.mine_pending_transactions(self.alice.address)
        tx2 = chain.create_transaction(self.alice, self.bob.address, 1, 0)
        chain.add_transaction(tx2)
        chain.mine_pending_transactions(self.alice.address)
        self.assertTrue(chain.is_valid())

        self.assertFalse(Blockchain().is_valid())

        variants = []
        changed_index = copy.deepcopy(chain)
        changed_index.blocks[1].index = 99
        variants.append(changed_index)

        changed_pow = copy.deepcopy(chain)
        changed_pow.blocks[1].difficulty = 70
        variants.append(changed_pow)

        changed_merkle = copy.deepcopy(chain)
        changed_merkle.blocks[1].merkle_root = "0"
        variants.append(changed_merkle)

        changed_genesis_previous = copy.deepcopy(chain)
        changed_genesis_previous.blocks[0].previous_hash = "1" * 64
        variants.append(changed_genesis_previous)

        changed_previous = copy.deepcopy(chain)
        changed_previous.blocks[1].previous_hash = "1" * 64
        variants.append(changed_previous)

        changed_reward = copy.deepcopy(chain)
        changed_reward.blocks[1].transactions[0].amount = 999
        changed_reward.blocks[1].merkle_root = calculate_merkle_root(
            [tx.txid for tx in changed_reward.blocks[1].transactions]
        )
        variants.append(changed_reward)

        for variant in variants:
            self.assertFalse(variant.is_valid())

    def test_chain_load_and_mempool_validation(self) -> None:
        chain = Blockchain.create({self.alice.address: 100}, difficulty=0)
        tx = chain.create_transaction(self.alice, self.bob.address, 1, 0)
        chain.add_transaction(tx)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chain.json"
            chain.save(path)
            loaded = Blockchain.load(path)
            self.assertEqual(len(loaded.mempool), 1)

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = CHAIN_SCHEMA_VERSION + 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(BlockchainError):
                Blockchain.load(path)

            chain.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["mempool"].append(payload["mempool"][0])
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(BlockchainError):
                Blockchain.load(path)


class CliHardeningTests(unittest.TestCase):
    def run_cli(self, *args: str) -> str:
        output = io.StringIO()
        with patch.object(sys, "argv", ["blockchaincoin", *args]), redirect_stdout(output):
            cli.main()
        return output.getvalue()

    def test_full_cli_flow_text_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alice = str(Path(tmp) / "alice.json")
            bob = str(Path(tmp) / "bob.json")
            chain = str(Path(tmp) / "chain.json")

            created = self.run_cli("wallet", "new", "--out", alice)
            self.assertIn("created wallet", created)
            self.run_cli("--json", "wallet", "new", "--out", bob)
            init = self.run_cli(
                "init",
                "--chain",
                chain,
                "--genesis-wallet",
                alice,
                "--amount",
                "100",
                "--difficulty",
                "0",
                "--reward",
                "5",
            )
            self.assertIn("initialized chain", init)
            send = json.loads(
                self.run_cli(
                    "--json",
                    "send",
                    "--chain",
                    chain,
                    "--from-wallet",
                    alice,
                    "--to-wallet",
                    bob,
                    "--amount",
                    "10",
                    "--fee",
                    "1",
                )
            )
            self.assertIn("txid", send)
            mine = self.run_cli("mine", "--chain", chain, "--miner-wallet", alice)
            self.assertIn("mined block #1", mine)
            balance = self.run_cli("balance", "--chain", chain, "--wallet", bob)
            self.assertIn("confirmed: 10", balance)
            summary = json.loads(self.run_cli("--json", "chain", "--chain", chain))
            self.assertTrue(summary["valid"])
            text_summary = self.run_cli("chain", "--chain", chain)
            self.assertIn("valid: True", text_summary)

    def test_cli_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chain = str(Path(tmp) / "missing.json")
            with self.assertRaises(SystemExit):
                cli._load_chain(chain)

            alice = str(Path(tmp) / "alice.json")
            chain_path = str(Path(tmp) / "chain.json")
            self.run_cli("wallet", "new", "--out", alice)
            self.run_cli(
                "init",
                "--chain",
                chain_path,
                "--genesis-wallet",
                alice,
                "--amount",
                "1",
                "--difficulty",
                "0",
            )

            with self.assertRaises(SystemExit):
                self.run_cli(
                    "send",
                    "--chain",
                    chain_path,
                    "--from-wallet",
                    alice,
                    "--amount",
                    "1",
                )

            with self.assertRaises(SystemExit):
                self.run_cli(
                    "send",
                    "--chain",
                    chain_path,
                    "--from-wallet",
                    alice,
                    "--to-address",
                    "bad",
                    "--amount",
                    "1",
                )

            bob = Wallet.create()
            with self.assertRaises(SystemExit):
                self.run_cli(
                    "send",
                    "--chain",
                    chain_path,
                    "--from-wallet",
                    alice,
                    "--to-address",
                    bob.address,
                    "--amount",
                    "2",
                )


if __name__ == "__main__":
    unittest.main()
