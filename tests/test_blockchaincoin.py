import tempfile
import unittest
from pathlib import Path

from blockchaincoin import Blockchain, Wallet
from blockchaincoin.chain import BlockchainError


class BlockchainCoinTests(unittest.TestCase):
    def test_signed_transaction_can_be_mined(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        chain = Blockchain.create(
            {alice.address: 100},
            difficulty=2,
            mining_reward=10,
        )

        tx = chain.create_transaction(alice, bob.address, amount=25, fee=2)
        chain.add_transaction(tx)
        block = chain.mine_pending_transactions(alice.address)

        self.assertTrue(block.hash.startswith("00"))
        self.assertTrue(chain.is_valid())
        self.assertEqual(chain.balance_of(bob.address), 25)
        self.assertEqual(chain.balance_of(alice.address), 85)

    def test_tampered_transaction_signature_is_rejected(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        chain = Blockchain.create({alice.address: 50}, difficulty=1)

        tx = chain.create_transaction(alice, bob.address, amount=10, fee=1)
        tx.amount = 11

        with self.assertRaises(BlockchainError):
            chain.add_transaction(tx)

    def test_insufficient_funds_are_rejected(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        chain = Blockchain.create({alice.address: 5}, difficulty=1)

        tx = chain.create_transaction(alice, bob.address, amount=5, fee=1)

        with self.assertRaises(BlockchainError):
            chain.add_transaction(tx)

    def test_chain_persists_to_json(self) -> None:
        alice = Wallet.create()
        chain = Blockchain.create({alice.address: 42}, difficulty=1)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chain.json"
            chain.save(path)
            loaded = Blockchain.load(path)

        self.assertTrue(loaded.is_valid())
        self.assertEqual(loaded.balance_of(alice.address), 42)

    def test_genesis_can_allocate_to_multiple_wallets(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        chain = Blockchain.create(
            {alice.address: 40, bob.address: 60},
            difficulty=1,
        )

        self.assertTrue(chain.is_valid())
        self.assertEqual(chain.balance_of(alice.address), 40)
        self.assertEqual(chain.balance_of(bob.address), 60)

    def test_duplicate_mempool_transaction_is_rejected(self) -> None:
        alice = Wallet.create()
        bob = Wallet.create()
        chain = Blockchain.create({alice.address: 50}, difficulty=1)
        tx = chain.create_transaction(alice, bob.address, amount=5, fee=1)

        chain.add_transaction(tx)

        with self.assertRaises(BlockchainError):
            chain.add_transaction(tx)

    def test_invalid_loaded_chain_is_rejected(self) -> None:
        alice = Wallet.create()
        chain = Blockchain.create({alice.address: 50}, difficulty=1)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chain.json"
            chain.save(path)
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace(alice.address, "bcc_bad"), encoding="utf-8")

            with self.assertRaises(BlockchainError):
                Blockchain.load(path)


if __name__ == "__main__":
    unittest.main()
