from __future__ import annotations

import os

from blockchaincoin.consensus import ConsensusBlock, ConsensusError, ConsensusTransaction


def fuzz_transaction(data: bytes) -> None:
    try:
        transaction = ConsensusTransaction.from_bytes(data)
    except ConsensusError:
        return
    if ConsensusTransaction.from_bytes(transaction.to_bytes()) != transaction:
        raise AssertionError("transaction codec is not stable")


def fuzz_block(data: bytes) -> None:
    try:
        block = ConsensusBlock.from_bytes(data)
    except ConsensusError:
        return
    if ConsensusBlock.from_bytes(block.to_bytes()) != block:
        raise AssertionError("block codec is not stable")


def main() -> None:
    samples = [b"", b"\x00", b"\xff" * 64]
    samples.extend(os.urandom(size) for size in range(0, 256, 19))
    for sample in samples:
        fuzz_transaction(sample)
        fuzz_block(sample)


if __name__ == "__main__":
    main()
