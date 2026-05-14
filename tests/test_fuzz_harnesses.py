import runpy
import unittest
from pathlib import Path


class FuzzHarnessTests(unittest.TestCase):
    def test_fuzz_harnesses_are_present_and_runnable(self) -> None:
        harnesses = (
            Path("fuzz/fuzz_consensus_codecs.py"),
            Path("fuzz/fuzz_network_frames.py"),
        )
        for harness in harnesses:
            self.assertTrue(harness.exists())
            namespace = runpy.run_path(str(harness))
            main = namespace["main"]
            main()


if __name__ == "__main__":
    unittest.main()
