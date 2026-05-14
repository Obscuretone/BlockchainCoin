import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


class EndToEndCLITests(unittest.TestCase):
    def run_app(self, *args: str) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "blockchaincoin", *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def run_json(self, *args: str) -> dict[str, Any]:
        return json.loads(self.run_app("--json", *args))

    def test_wallet_to_wallet_utxo_lifecycle_through_application_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            miner_wallet = root / "miner.json"
            alice_wallet = root / "alice.json"
            bob_wallet = root / "bob.json"
            node_db = root / "node.sqlite3"

            miner = self.run_json("wallet", "new", "--out", str(miner_wallet))
            alice = self.run_json("wallet", "new", "--out", str(alice_wallet))
            bob = self.run_json("wallet", "new", "--out", str(bob_wallet))
            self.assertTrue(str(miner["address"]).startswith("bcc_"))
            self.assertTrue(str(alice["address"]).startswith("bcc_"))
            self.assertTrue(str(bob["address"]).startswith("bcc_"))
            self.assertTrue(miner_wallet.exists())
            self.assertTrue(alice_wallet.exists())
            self.assertTrue(bob_wallet.exists())

            init = self.run_json(
                "init",
                "--db",
                str(node_db),
                "--genesis-wallet",
                str(alice_wallet),
                "--amount",
                "100",
                "--node-id",
                "e2e-node",
                "--peer-auth-key",
                "e2e-test-key",
            )
            self.assertEqual(init["height"], 0)
            self.assertEqual(init["network"], "regtest")
            self.assertEqual(init["node_id"], "e2e-node")
            self.assertTrue(node_db.exists())

            alice_status = self.run_json(
                "chain", "--db", str(node_db), "--wallet", str(alice_wallet)
            )
            bob_status = self.run_json("chain", "--db", str(node_db), "--wallet", str(bob_wallet))
            self.assertEqual(alice_status["balance"], 100)
            self.assertEqual(bob_status["balance"], 0)
            self.assertEqual(alice_status["mempool"], 0)

            alice_utxos = self.run_json(
                "utxos", "--db", str(node_db), "--wallet", str(alice_wallet)
            )
            self.assertEqual(alice_utxos["balance"], 100)
            self.assertEqual(alice_utxos["count"], 1)

            sent = self.run_json(
                "send",
                "--db",
                str(node_db),
                "--from-wallet",
                str(alice_wallet),
                "--to-wallet",
                str(bob_wallet),
                "--amount",
                "37",
                "--fee",
                "3",
            )
            self.assertEqual(sent["inputs"], 1)
            self.assertEqual(sent["outputs"], 2)
            txid = str(sent["txid"])

            mempool = self.run_json("mempool", "--db", str(node_db))
            self.assertEqual(mempool["count"], 1)
            self.assertEqual(mempool["transactions"][0]["txid"], txid)
            self.assertEqual(mempool["transactions"][0]["fee"], 3)
            self.assertTrue(mempool["transactions"][0]["valid"])

            pending_tx = self.run_json("tx", "--db", str(node_db), "--txid", txid)
            self.assertEqual(pending_tx["status"], "mempool")
            self.assertEqual(pending_tx["output_value"], 97)

            pruned = self.run_json("mempool", "--db", str(node_db), "--prune")
            self.assertEqual(pruned["removed"], 0)

            mined_text = self.run_app(
                "mine", "--db", str(node_db), "--miner-wallet", str(miner_wallet)
            )
            self.assertIn("mined UTXO block #1", mined_text)

            final_alice = self.run_json(
                "chain", "--db", str(node_db), "--wallet", str(alice_wallet)
            )
            final_bob = self.run_json("chain", "--db", str(node_db), "--wallet", str(bob_wallet))
            final_miner = self.run_json(
                "chain", "--db", str(node_db), "--wallet", str(miner_wallet)
            )
            self.assertEqual(final_alice["balance"], 60)
            self.assertEqual(final_bob["balance"], 37)
            self.assertEqual(final_miner["balance"], 53)
            self.assertEqual(final_alice["mempool"], 0)
            self.assertEqual(final_alice["supply"], 150)

            block = self.run_json("block", "--db", str(node_db), "--height", "1")
            self.assertTrue(block["active"])
            self.assertEqual(block["transaction_count"], 2)
            self.assertIn(txid, block["transactions"])

            confirmed_tx = self.run_json("tx", "--db", str(node_db), "--txid", txid)
            self.assertEqual(confirmed_tx["status"], "confirmed")
            self.assertEqual(confirmed_tx["height"], 1)
            self.assertEqual(confirmed_tx["block_hash"], block["hash"])

            bob_utxos = self.run_json("utxos", "--db", str(node_db), "--wallet", str(bob_wallet))
            self.assertEqual(bob_utxos["balance"], 37)
            self.assertEqual(bob_utxos["count"], 1)
            self.assertEqual(bob_utxos["utxos"][0]["amount"], 37)

            empty_mempool = self.run_app("mempool", "--db", str(node_db))
            self.assertEqual(empty_mempool.strip(), "UTXO mempool is empty")


if __name__ == "__main__":
    unittest.main()
