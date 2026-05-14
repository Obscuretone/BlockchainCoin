from __future__ import annotations

import os

from blockchaincoin.network import MessageType, NetworkError, PeerMessage

AUTH_KEY = b"fuzz-peer-auth-key"


def fuzz_frame(data: bytes) -> None:
    try:
        PeerMessage.from_frame(data, AUTH_KEY)
    except NetworkError:
        return


def main() -> None:
    samples = [
        b"",
        b"BCCN\x00\x00\x00\x00",
        PeerMessage(MessageType.PING, {"nonce": 1}).to_frame(AUTH_KEY),
        PeerMessage(MessageType.REJECT, {"reason": "fuzz"}).to_frame(AUTH_KEY),
    ]
    samples.extend(os.urandom(size) for size in range(0, 256, 17))
    for sample in samples:
        fuzz_frame(sample)


if __name__ == "__main__":
    main()
