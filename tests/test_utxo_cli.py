import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from blockchaincoin import cli


class UTXOCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> str:
        output = io.StringIO()
        with patch.object(sys, "argv", ["blockchaincoin", *args]), redirect_stdout(output):
            cli.main()
        return output.getvalue()

    def test_utxo_init_status_mine_and_serve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wallet = str(Path(tmp) / "wallet.json")
            bob = str(Path(tmp) / "bob.json")
            db = str(Path(tmp) / "node.sqlite3")

            self.run_cli("wallet", "new", "--out", wallet)
            self.run_cli("wallet", "new", "--out", bob)
            init = json.loads(
                self.run_cli(
                    "--json",
                    "utxo-init",
                    "--db",
                    db,
                    "--genesis-wallet",
                    wallet,
                    "--amount",
                    "25",
                    "--node-id",
                    "cli-node",
                    "--peer-auth-key",
                    "cli-test-key",
                )
            )
            self.assertEqual(init["height"], 0)

            status = json.loads(
                self.run_cli("--json", "utxo-status", "--db", db, "--wallet", wallet)
            )
            self.assertEqual(status["height"], 0)
            self.assertEqual(status["supply"], 25)
            self.assertEqual(status["balance"], 25)
            public_status = json.loads(self.run_cli("--json", "utxo-status", "--db", db))
            self.assertNotIn("balance", public_status)
            wallet_utxos = json.loads(
                self.run_cli("--json", "utxo-utxos", "--db", db, "--wallet", wallet)
            )
            self.assertEqual(wallet_utxos["balance"], 25)
            self.assertEqual(wallet_utxos["count"], 1)
            self.assertEqual(wallet_utxos["utxos"][0]["amount"], 25)

            sent = json.loads(
                self.run_cli(
                    "--json",
                    "utxo-send",
                    "--db",
                    db,
                    "--from-wallet",
                    wallet,
                    "--to-wallet",
                    bob,
                    "--amount",
                    "5",
                    "--fee",
                    "1",
                )
            )
            self.assertEqual(sent["inputs"], 1)
            queued = json.loads(self.run_cli("--json", "utxo-mempool", "--db", db))
            self.assertEqual(queued["count"], 1)
            self.assertEqual(queued["transactions"][0]["fee"], 1)
            self.assertTrue(queued["transactions"][0]["valid"])
            listed = self.run_cli("utxo-mempool", "--db", db)
            self.assertIn(sent["txid"], listed)
            pending_tx = json.loads(
                self.run_cli("--json", "utxo-tx", "--db", db, "--txid", sent["txid"])
            )
            self.assertEqual(pending_tx["status"], "mempool")
            self.assertEqual(pending_tx["output_count"], 2)
            pruned = json.loads(self.run_cli("--json", "utxo-mempool", "--db", db, "--prune"))
            self.assertEqual(pruned["removed"], 0)

            mined = self.run_cli("utxo-mine", "--db", db, "--miner-wallet", wallet)
            self.assertIn("mined UTXO block #1", mined)
            genesis_block = json.loads(
                self.run_cli("--json", "utxo-block", "--db", db, "--hash", init["tip"])
            )
            self.assertEqual(genesis_block["height"], 0)
            self.assertTrue(genesis_block["active"])
            mined_block = json.loads(
                self.run_cli("--json", "utxo-block", "--db", db, "--height", "1")
            )
            self.assertEqual(mined_block["transaction_count"], 2)
            self.assertIn(sent["txid"], mined_block["transactions"])
            confirmed_tx = self.run_cli("utxo-tx", "--db", db, "--txid", sent["txid"])
            self.assertIn("status=confirmed", confirmed_tx)
            self.assertIn("height=1", confirmed_tx)

            status = json.loads(self.run_cli("--json", "utxo-status", "--db", db, "--wallet", bob))
            self.assertEqual(status["height"], 1)
            self.assertEqual(status["balance"], 5)
            bob_utxos = self.run_cli("utxo-utxos", "--db", db, "--address", status["address"])
            self.assertIn("amount=5", bob_utxos)

            served = json.loads(
                self.run_cli(
                    "--json",
                    "utxo-serve",
                    "--db",
                    db,
                    "--node-id",
                    "cli-node",
                    "--peer-auth-key",
                    "cli-test-key",
                )
            )
            self.assertEqual(served["height"], 1)
            self.assertGreaterEqual(served["port"], 0)

            with self.assertRaises(SystemExit):
                self.run_cli(
                    "utxo-send",
                    "--db",
                    db,
                    "--from-wallet",
                    wallet,
                    "--amount",
                    "1",
                )
            with self.assertRaises(SystemExit):
                self.run_cli("utxo-utxos", "--db", db)
            with self.assertRaises(SystemExit):
                self.run_cli("utxo-utxos", "--db", db, "--address", "bad")
            with self.assertRaises(SystemExit):
                self.run_cli("utxo-block", "--db", db, "--height", "99")
            with self.assertRaises(SystemExit):
                self.run_cli("utxo-tx", "--db", db, "--txid", "missing")

    def test_utxo_mempool_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wallet = str(Path(tmp) / "wallet.json")
            bob = str(Path(tmp) / "bob.json")
            db = str(Path(tmp) / "node.sqlite3")

            self.run_cli("wallet", "new", "--out", wallet)
            self.run_cli("wallet", "new", "--out", bob)
            self.run_cli(
                "utxo-init",
                "--db",
                db,
                "--genesis-wallet",
                wallet,
                "--amount",
                "10",
                "--peer-auth-key",
                "cli-test-key",
            )
            self.run_cli(
                "utxo-send",
                "--db",
                db,
                "--from-wallet",
                wallet,
                "--to-wallet",
                bob,
                "--amount",
                "2",
                "--fee",
                "1",
            )

            cleared = json.loads(self.run_cli("--json", "utxo-mempool", "--db", db, "--clear"))
            self.assertEqual(cleared["removed"], 1)
            empty = self.run_cli("utxo-mempool", "--db", db)
            self.assertEqual(empty.strip(), "UTXO mempool is empty")
            empty_utxos = self.run_cli("utxo-utxos", "--db", db, "--wallet", bob)
            self.assertIn("no spendable UTXOs", empty_utxos)

    def test_primary_cli_verbs_can_drive_utxo_node_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alice = str(Path(tmp) / "alice.json")
            bob = str(Path(tmp) / "bob.json")
            miner = str(Path(tmp) / "miner.json")
            db = str(Path(tmp) / "node.sqlite3")

            self.run_cli("wallet", "new", "--out", alice)
            self.run_cli("wallet", "new", "--out", bob)
            self.run_cli("wallet", "new", "--out", miner)
            encrypted = str(Path(tmp) / "encrypted.json")
            self.run_cli("wallet", "new", "--out", encrypted, "--passphrase", "secret")
            encrypted_payload = json.loads(Path(encrypted).read_text(encoding="utf-8"))
            self.assertTrue(encrypted_payload["encrypted"])

            init = json.loads(
                self.run_cli(
                    "--json",
                    "init",
                    "--db",
                    db,
                    "--genesis-wallet",
                    alice,
                    "--amount",
                    "30",
                    "--node-id",
                    "alias-node",
                    "--peer-auth-key",
                    "cli-test-key",
                )
            )
            self.assertEqual(init["height"], 0)
            self.assertEqual(init["node_id"], "alias-node")
            with self.assertRaises(SystemExit):
                self.run_cli(
                    "init",
                    "--db",
                    str(Path(tmp) / "missing-auth.sqlite3"),
                    "--genesis-wallet",
                    alice,
                )

            sent = json.loads(
                self.run_cli(
                    "--json",
                    "send",
                    "--db",
                    db,
                    "--from-wallet",
                    alice,
                    "--to-wallet",
                    bob,
                    "--amount",
                    "4",
                    "--fee",
                    "1",
                )
            )
            self.assertEqual(sent["inputs"], 1)

            mined = json.loads(self.run_cli("--json", "mine", "--db", db, "--miner-wallet", miner))
            self.assertEqual(mined["height"], 1)

            bob_status = json.loads(
                self.run_cli("--json", "utxo-status", "--db", db, "--wallet", bob)
            )
            self.assertEqual(bob_status["balance"], 4)

            bob_balance = json.loads(self.run_cli("--json", "balance", "--db", db, "--wallet", bob))
            self.assertEqual(bob_balance["balance"], 4)
            self.assertEqual(bob_balance["height"], 1)

            chain_status = json.loads(self.run_cli("--json", "chain", "--db", db))
            self.assertEqual(chain_status["height"], 1)
            self.assertEqual(chain_status["mempool"], 0)


if __name__ == "__main__":
    unittest.main()
