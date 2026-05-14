import struct
import unittest
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

from blockchaincoin.chain import MAX_MONEY
from blockchaincoin.consensus import (
    BINARY_CODEC_VERSION,
    COINBASE_PREV_TXID,
    SUPPORTED_BLOCK_VERSIONS,
    SUPPORTED_TRANSACTION_VERSIONS,
    UTXO,
    BlockProcessor,
    ConsensusBlock,
    ConsensusError,
    ConsensusState,
    ConsensusTransaction,
    OutPoint,
    TxInput,
    TxOutput,
    UTXOSet,
    _encode_bytes,
    calculate_transaction_root,
    rebuild_state_from_blocks,
)
from blockchaincoin.crypto import Wallet

SPEND_CASES = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=500),
        st.integers(min_value=0, max_value=25),
    ),
    min_size=1,
    max_size=12,
)


class ConsensusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alice = Wallet.create()
        self.bob = Wallet.create()

    def test_coinbase_and_signed_spend_lifecycle(self) -> None:
        state = ConsensusState(block_subsidy=50)
        coinbase = state.create_coinbase(self.alice.address)
        self.assertTrue(coinbase.is_coinbase)
        self.assertEqual(state.apply_transaction(coinbase), 0)
        self.assertEqual(len(state.utxos), 1)
        self.assertEqual(state.utxos.total(), 50)

        spend = ConsensusTransaction(
            inputs=(TxInput(OutPoint(coinbase.txid, 0)),),
            outputs=(
                TxOutput(20, self.bob.address),
                TxOutput(25, self.alice.address),
            ),
        ).sign_input(0, self.alice)

        self.assertEqual(state.validate_transaction(spend), 5)
        self.assertEqual(state.apply_transaction(spend), 5)
        self.assertFalse(state.utxos.contains(OutPoint(coinbase.txid, 0)))
        self.assertEqual(state.utxos.total(), 45)

        restored = ConsensusTransaction.from_dict(spend.to_dict())
        self.assertEqual(restored.txid, spend.txid)

    def test_binary_consensus_serialization_drives_ids_and_hashes(self) -> None:
        state = ConsensusState(block_subsidy=50)
        coinbase = state.create_coinbase(self.alice.address)
        unsigned = ConsensusTransaction(
            inputs=(TxInput(OutPoint(coinbase.txid, 0)),),
            outputs=(TxOutput(49, self.bob.address),),
        )
        signed = unsigned.sign_input(0, self.alice)
        block = ConsensusBlock(1, "0" * 64, (coinbase, signed), difficulty=0, timestamp=1.5)

        self.assertEqual(BINARY_CODEC_VERSION, 1)
        self.assertTrue(coinbase.to_bytes().startswith(b"bcctx"))
        self.assertNotEqual(unsigned.to_bytes(), signed.to_bytes())
        self.assertEqual(
            unsigned.to_bytes(include_witness=False), signed.to_bytes(include_witness=False)
        )
        self.assertEqual(
            ConsensusTransaction.from_dict(signed.to_dict()).to_bytes(), signed.to_bytes()
        )
        self.assertEqual(ConsensusTransaction.from_bytes(signed.to_bytes()), signed)
        self.assertTrue(block.header_bytes().startswith(b"bccblk"))
        self.assertEqual(ConsensusBlock.from_bytes(block.to_bytes()).hash, block.hash)
        self.assertEqual(ConsensusBlock.from_bytes(block.to_bytes()).to_bytes(), block.to_bytes())
        self.assertEqual(
            ConsensusBlock.from_dict(block.to_dict()).header_bytes(), block.header_bytes()
        )
        self.assertEqual(calculate_transaction_root((coinbase, signed)), block.transaction_root)

    def test_binary_consensus_serialization_rejects_invalid_fields(self) -> None:
        class HugeBytes:
            def __len__(self) -> int:
                return 2**32

        with self.assertRaises(ConsensusError):
            TxOutput(-1, self.alice.address).to_bytes()
        with self.assertRaises(ConsensusError):
            _encode_bytes(cast(bytes, HugeBytes()))
        with self.assertRaises(ConsensusError):
            OutPoint("bad", 0).to_bytes()
        with self.assertRaises(ConsensusError):
            OutPoint("aa" * 31, 0).to_bytes()
        with self.assertRaises(ConsensusError):
            ConsensusTransaction.from_bytes(b"badtx")
        with self.assertRaises(ConsensusError):
            ConsensusTransaction.from_bytes(b"bcctx")
        with self.assertRaises(ConsensusError):
            ConsensusTransaction.from_bytes(
                b"bcctx" + (BINARY_CODEC_VERSION + 1).to_bytes(2, "big")
            )
        with self.assertRaises(ConsensusError):
            ConsensusBlock.from_bytes(b"badblk")
        with self.assertRaises(ConsensusError):
            ConsensusBlock.from_bytes(b"bccblk" + (BINARY_CODEC_VERSION + 1).to_bytes(2, "big"))

        unsigned = ConsensusTransaction(
            inputs=(TxInput(OutPoint("a" * 64, 0)),),
            outputs=(TxOutput(1, self.alice.address),),
        )
        self.assertEqual(
            ConsensusTransaction.from_bytes(
                unsigned.to_bytes(include_witness=False),
                include_witness=False,
            ),
            unsigned,
        )
        with self.assertRaises(ConsensusError):
            ConsensusTransaction.from_bytes(unsigned.to_bytes() + b"x")

        invalid_signature_flag = bytearray(unsigned.to_bytes())
        invalid_signature_flag[63] = 2
        with self.assertRaises(ConsensusError):
            ConsensusTransaction.from_bytes(bytes(invalid_signature_flag))

        invalid_public_key_flag = bytearray(unsigned.to_bytes())
        invalid_public_key_flag[64] = 2
        with self.assertRaises(ConsensusError):
            ConsensusTransaction.from_bytes(bytes(invalid_public_key_flag))

        invalid_utf8_signature = (
            b"bcctx"
            + struct.pack(">H", BINARY_CODEC_VERSION)
            + struct.pack(">q", 1)
            + struct.pack(">Q", 1)
            + OutPoint("a" * 64, 0).to_bytes()
            + b"\x01"
            + struct.pack(">I", 1)
            + b"\xff"
        )
        with self.assertRaises(ConsensusError):
            ConsensusTransaction.from_bytes(invalid_utf8_signature)

    def test_utxo_set_copy_add_get_spend_errors(self) -> None:
        outpoint = OutPoint("a" * 64, 0)
        output = TxOutput(1, self.alice.address)
        utxos = UTXOSet([UTXO(outpoint, output)])
        copy = utxos.copy()

        self.assertEqual(copy.get(outpoint), output)
        with self.assertRaises(ConsensusError):
            copy.add(outpoint, output)
        self.assertEqual(copy.spend(outpoint), output)
        with self.assertRaises(ConsensusError):
            copy.get(outpoint)
        with self.assertRaises(ConsensusError):
            copy.spend(outpoint)

        self.assertEqual(OutPoint.from_dict(outpoint.to_dict()), outpoint)
        self.assertEqual(TxOutput.from_dict(output.to_dict()), output)
        self.assertEqual(TxInput.from_dict(TxInput(outpoint).to_dict()), TxInput(outpoint))

    def test_transaction_validation_errors(self) -> None:
        state = ConsensusState(block_subsidy=50)
        coinbase = state.create_coinbase(self.alice.address)
        state.apply_transaction(coinbase)
        outpoint = OutPoint(coinbase.txid, 0)

        self.assertEqual(SUPPORTED_TRANSACTION_VERSIONS, frozenset({1}))
        with self.assertRaises(ConsensusError):
            ConsensusState(block_subsidy=-1)
        with self.assertRaises(ConsensusError):
            ConsensusState(max_money=0)
        with self.assertRaises(ConsensusError):
            state.validate_transaction(
                ConsensusTransaction(
                    inputs=(TxInput(outpoint),),
                    outputs=(TxOutput(1, self.bob.address),),
                    version=2,
                )
            )
        with self.assertRaises(ConsensusError):
            ConsensusTransaction((), (TxOutput(1, self.bob.address),)).sign_input(0, self.alice)
        with self.assertRaises(ConsensusError):
            state.validate_transaction(ConsensusTransaction((), (TxOutput(1, self.bob.address),)))
        with self.assertRaises(ConsensusError):
            state.validate_transaction(ConsensusTransaction((TxInput(outpoint),), ()))
        with self.assertRaises(ConsensusError):
            state.validate_transaction(
                ConsensusTransaction((TxInput(outpoint),), (TxOutput(0, self.bob.address),))
            )
        with self.assertRaises(ConsensusError):
            state.validate_transaction(
                ConsensusTransaction(
                    (TxInput(outpoint),), (TxOutput(MAX_MONEY + 1, self.bob.address),)
                )
            )
        with self.assertRaises(ConsensusError):
            state.validate_transaction(
                ConsensusTransaction((TxInput(outpoint),), (TxOutput(1, "bad"),))
            )

        missing_witness = ConsensusTransaction(
            (TxInput(outpoint),), (TxOutput(1, self.bob.address),)
        )
        with self.assertRaises(ConsensusError):
            state.validate_transaction(missing_witness)

        wrong_owner = ConsensusTransaction(
            (TxInput(outpoint),), (TxOutput(1, self.bob.address),)
        ).sign_input(0, self.bob)
        with self.assertRaises(ConsensusError):
            state.validate_transaction(wrong_owner)

        malformed_key = ConsensusTransaction(
            (TxInput(outpoint, signature="sig", public_key={"bad": "key"}),),
            (TxOutput(1, self.bob.address),),
        )
        with self.assertRaises(ConsensusError):
            state.validate_transaction(malformed_key)

        signed = ConsensusTransaction(
            (TxInput(outpoint),), (TxOutput(1, self.bob.address),)
        ).sign_input(0, self.alice)
        tampered = ConsensusTransaction(signed.inputs, (TxOutput(2, self.bob.address),))
        with self.assertRaises(ConsensusError):
            state.validate_transaction(tampered)

        overspend = ConsensusTransaction(
            (TxInput(outpoint),), (TxOutput(51, self.bob.address),)
        ).sign_input(0, self.alice)
        with self.assertRaises(ConsensusError):
            state.validate_transaction(overspend)

        duplicate_input = (
            ConsensusTransaction(
                (TxInput(outpoint), TxInput(outpoint)),
                (TxOutput(1, self.bob.address),),
            )
            .sign_input(0, self.alice)
            .sign_input(1, self.alice)
        )
        with self.assertRaises(ConsensusError):
            state.validate_transaction(duplicate_input)

    def test_coinbase_validation_errors(self) -> None:
        state = ConsensusState(block_subsidy=50)

        with self.assertRaises(ConsensusError):
            state.create_coinbase("bad")
        with self.assertRaises(ConsensusError):
            state.create_coinbase(self.alice.address, fees=-1)
        with self.assertRaises(ConsensusError):
            state.create_coinbase(self.alice.address, height=-1)
        self.assertTrue(state.create_coinbase(self.alice.address, height=2).is_coinbase)

        too_much = ConsensusTransaction(
            inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -1)),),
            outputs=(TxOutput(51, self.alice.address),),
        )
        with self.assertRaises(ConsensusError):
            state.validate_transaction(too_much)

        signed_coinbase = ConsensusTransaction(
            inputs=(
                TxInput(
                    OutPoint(COINBASE_PREV_TXID, -1),
                    signature="sig",
                    public_key=self.alice.public_key.to_dict(),
                ),
            ),
            outputs=(TxOutput(50, self.alice.address),),
        )
        with self.assertRaises(ConsensusError):
            state.validate_transaction(signed_coinbase)

        fake_coinbase = ConsensusTransaction(
            inputs=(
                TxInput(OutPoint(COINBASE_PREV_TXID, -1)),
                TxInput(OutPoint(COINBASE_PREV_TXID, -1)),
            ),
            outputs=(TxOutput(1, self.alice.address),),
        )
        with self.assertRaises(ConsensusError):
            state._validate_coinbase(fake_coinbase)

        capped = ConsensusState(block_subsidy=1, max_money=1)
        capped.apply_transaction(capped.create_coinbase(self.alice.address))
        with self.assertRaises(ConsensusError):
            capped.create_coinbase(self.alice.address)

    def test_supply_cap_after_apply_rolls_error(self) -> None:
        state = ConsensusState(block_subsidy=MAX_MONEY, max_money=MAX_MONEY)
        state.apply_transaction(state.create_coinbase(self.alice.address))
        state.block_subsidy = 1
        before_total = state.utxos.total()
        with self.assertRaises(ConsensusError):
            state.apply_transaction(
                ConsensusTransaction(
                    inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -1)),),
                    outputs=(TxOutput(1, self.bob.address),),
                )
            )
        self.assertEqual(state.utxos.total(), before_total)

    @settings(max_examples=25, deadline=None)
    @given(SPEND_CASES)
    def test_property_utxo_supply_never_exceeds_cap_and_fees_burn_until_mined(
        self,
        spend_cases: list[tuple[int, int]],
    ) -> None:
        state = ConsensusState(block_subsidy=10_000, max_money=10_000)
        coinbase = state.create_coinbase(self.alice.address)
        state.apply_transaction(coinbase)
        current_outpoint = OutPoint(coinbase.txid, 0)
        current_amount = 10_000
        total_fees = 0

        for requested_amount, requested_fee in spend_cases:
            previous_total = state.utxos.total()
            spend_total = requested_amount + requested_fee
            if spend_total > current_amount:
                invalid = ConsensusTransaction(
                    inputs=(TxInput(current_outpoint),),
                    outputs=(TxOutput(current_amount + 1, self.bob.address),),
                ).sign_input(0, self.alice)
                with self.assertRaises(ConsensusError):
                    state.validate_transaction(invalid)
                self.assertEqual(state.utxos.total(), previous_total)
                continue

            outputs = [TxOutput(requested_amount, self.bob.address)]
            change = current_amount - spend_total
            if change:
                outputs.append(TxOutput(change, self.alice.address))
            transaction = ConsensusTransaction(
                inputs=(TxInput(current_outpoint),),
                outputs=tuple(outputs),
            ).sign_input(0, self.alice)

            self.assertEqual(state.validate_transaction(transaction), requested_fee)
            self.assertEqual(state.apply_transaction(transaction), requested_fee)
            total_fees += requested_fee
            self.assertLessEqual(state.utxos.total(), state.max_money)
            self.assertEqual(state.utxos.total(), 10_000 - total_fees)
            self.assertFalse(state.utxos.contains(current_outpoint))
            if change:
                current_outpoint = OutPoint(transaction.txid, 1)
                current_amount = change
            else:
                break

    @settings(max_examples=25, deadline=None)
    @given(
        amount=st.integers(min_value=1, max_value=99),
        fee=st.integers(min_value=0, max_value=20),
    )
    def test_property_utxo_replay_is_rejected_without_state_change(
        self,
        amount: int,
        fee: int,
    ) -> None:
        genesis_amount = 120
        state = ConsensusState(block_subsidy=genesis_amount, max_money=genesis_amount)
        coinbase = state.create_coinbase(self.alice.address)
        state.apply_transaction(coinbase)
        change = genesis_amount - amount - fee
        outputs = [TxOutput(amount, self.bob.address)]
        if change:
            outputs.append(TxOutput(change, self.alice.address))
        transaction = ConsensusTransaction(
            inputs=(TxInput(OutPoint(coinbase.txid, 0)),),
            outputs=tuple(outputs),
        ).sign_input(0, self.alice)

        self.assertEqual(state.apply_transaction(transaction), fee)
        entries_after_first_apply = state.utxos.entries()
        total_after_first_apply = state.utxos.total()
        with self.assertRaises(ConsensusError):
            state.apply_transaction(transaction)
        self.assertEqual(state.utxos.entries(), entries_after_first_apply)
        self.assertEqual(state.utxos.total(), total_after_first_apply)

    def test_block_processor_applies_fee_paying_block_and_rebuilds_state(self) -> None:
        state = ConsensusState(block_subsidy=50)
        processor = BlockProcessor(state)
        genesis_coinbase = state.create_coinbase(self.alice.address)
        genesis = ConsensusBlock(
            height=0,
            previous_hash="0" * 64,
            transactions=(genesis_coinbase,),
            difficulty=0,
        )

        self.assertEqual(processor.apply_block(genesis, 0, "0" * 64), 0)

        spend = ConsensusTransaction(
            inputs=(TxInput(OutPoint(genesis_coinbase.txid, 0)),),
            outputs=(
                TxOutput(20, self.bob.address),
                TxOutput(25, self.alice.address),
            ),
        ).sign_input(0, self.alice)
        reward = state.create_coinbase(self.alice.address, fees=5)
        block = ConsensusBlock(
            height=1,
            previous_hash=genesis.hash,
            transactions=(reward, spend),
            difficulty=0,
        )

        self.assertEqual(processor.validate_block(block, 1, genesis.hash), 5)
        self.assertEqual(processor.apply_block(block, 1, genesis.hash), 5)
        self.assertEqual(state.utxos.total(), 100)

        restored = ConsensusBlock.from_dict(block.to_dict())
        self.assertEqual(restored.hash, block.hash)
        rebuilt = rebuild_state_from_blocks([genesis, block], block_subsidy=50)
        self.assertEqual(rebuilt.utxos.total(), 100)
        self.assertEqual(
            calculate_transaction_root(()),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(
            len(calculate_transaction_root((genesis_coinbase, reward, spend))),
            64,
        )

    def test_block_processor_rejects_invalid_blocks(self) -> None:
        state = ConsensusState(block_subsidy=50)
        processor = BlockProcessor(state)
        coinbase = state.create_coinbase(self.alice.address)
        valid = ConsensusBlock(0, "0" * 64, (coinbase,), difficulty=0)

        self.assertEqual(SUPPORTED_BLOCK_VERSIONS, frozenset({1}))
        cases = [
            ConsensusBlock(0, "0" * 64, (coinbase,), difficulty=0, version=2),
            ConsensusBlock(1, "0" * 64, (coinbase,), difficulty=0),
            ConsensusBlock(0, "1" * 64, (coinbase,), difficulty=0),
            ConsensusBlock(0, "0" * 64, (coinbase,), difficulty=-1),
            ConsensusBlock(0, "0" * 64, (coinbase,), difficulty=70),
            ConsensusBlock(0, "0" * 64, (coinbase,), difficulty=0, transaction_root="f" * 64),
            ConsensusBlock(0, "0" * 64, (), difficulty=0),
            ConsensusBlock(
                0,
                "0" * 64,
                (
                    ConsensusTransaction(
                        inputs=(TxInput(OutPoint("a" * 64, 0)),),
                        outputs=(TxOutput(1, self.alice.address),),
                    ),
                ),
                difficulty=0,
            ),
            ConsensusBlock(0, "0" * 64, (coinbase, coinbase), difficulty=0),
        ]
        for block in cases:
            with self.assertRaises(ConsensusError):
                processor.validate_block(block, 0, "0" * 64)

        too_much_coinbase = ConsensusBlock(
            1,
            "a" * 64,
            (
                ConsensusTransaction(
                    inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -1)),),
                    outputs=(TxOutput(51, self.alice.address),),
                ),
            ),
            difficulty=0,
        )
        with self.assertRaises(ConsensusError):
            processor.validate_block(too_much_coinbase, 1, "a" * 64)

        signed_coinbase = ConsensusBlock(
            0,
            "0" * 64,
            (
                ConsensusTransaction(
                    inputs=(
                        TxInput(
                            OutPoint(COINBASE_PREV_TXID, -1),
                            signature="sig",
                            public_key=self.alice.public_key.to_dict(),
                        ),
                    ),
                    outputs=(TxOutput(50, self.alice.address),),
                ),
            ),
            difficulty=0,
        )
        with self.assertRaises(ConsensusError):
            processor.validate_block(signed_coinbase, 0, "0" * 64)

        with self.assertRaises(ConsensusError):
            state.apply_block_coinbase(
                ConsensusTransaction(
                    inputs=(TxInput(OutPoint("a" * 64, 0)),),
                    outputs=(TxOutput(1, self.alice.address),),
                ),
                fees=0,
            )
        with self.assertRaises(ConsensusError):
            state.apply_block_coinbase(coinbase, fees=-1)
        with self.assertRaises(ConsensusError):
            state.apply_block_coinbase(signed_coinbase.transactions[0], fees=0)
        with self.assertRaises(ConsensusError):
            state.apply_genesis_coinbase(
                ConsensusTransaction(
                    inputs=(TxInput(OutPoint("a" * 64, 0)),),
                    outputs=(TxOutput(1, self.alice.address),),
                )
            )
        with self.assertRaises(ConsensusError):
            state.apply_genesis_coinbase(signed_coinbase.transactions[0])

        capped = ConsensusState(block_subsidy=50, max_money=50)
        capped.apply_block_coinbase(coinbase, fees=0)
        capped.block_subsidy = 0
        with self.assertRaises(ConsensusError):
            capped.apply_block_coinbase(
                ConsensusTransaction(
                    inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -1)),),
                    outputs=(TxOutput(1, self.bob.address),),
                ),
                fees=1,
            )
        capped_genesis = ConsensusState(block_subsidy=0, max_money=1)
        capped_genesis.apply_genesis_coinbase(
            ConsensusTransaction(
                inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -1)),),
                outputs=(TxOutput(1, self.alice.address),),
            )
        )
        with self.assertRaises(ConsensusError):
            capped_genesis.apply_genesis_coinbase(
                ConsensusTransaction(
                    inputs=(TxInput(OutPoint(COINBASE_PREV_TXID, -1)),),
                    outputs=(TxOutput(1, self.bob.address),),
                )
            )

        self.assertEqual(processor.validate_block(valid, 0, "0" * 64), 0)


if __name__ == "__main__":
    unittest.main()
