import unittest
from pathlib import Path


class CIConfigTests(unittest.TestCase):
    def test_quality_workflow_contains_release_grade_gates(self) -> None:
        workflow = Path(".github/workflows/quality.yml").read_text(encoding="utf-8")

        for python_version in ('"3.11"', '"3.12"', '"3.13"'):
            self.assertIn(python_version, workflow)
        for command in (
            "python -m venv .venv",
            "python -m pip install -c constraints/release.txt -e '.[dev]'",
            "ruff format --check .",
            "ruff check .",
            "pyright",
            "coverage run -m unittest",
            "coverage report",
            "python -m compileall -q blockchaincoin tests",
            "python -m build",
        ):
            self.assertIn(command, workflow)
        self.assertIn("pull_request:", workflow)
        self.assertIn("push:", workflow)
        self.assertIn("actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5", workflow)
        self.assertIn("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065", workflow)
        self.assertIn("PIP_CONSTRAINT: constraints/release.txt", workflow)
        self.assertTrue(Path("constraints/release.txt").exists())


if __name__ == "__main__":
    unittest.main()
