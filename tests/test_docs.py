import unittest
from pathlib import Path


class DocumentationTests(unittest.TestCase):
    def test_architecture_document_covers_system_boundaries(self) -> None:
        text = Path("docs/architecture.md").read_text(encoding="utf-8")

        for heading in (
            "# Architecture",
            "## Consensus",
            "## Storage",
            "## Mempool",
            "## Peer Network",
            "## Runtime And CLI",
            "## Invariants",
            "## Boundaries",
        ):
            self.assertIn(heading, text)
        for required in (
            "ChainParameters",
            "chain_id",
            "HMAC-authenticated",
            "anchored to a known",
            "UTXO snapshot",
            "External Audit Checklist",
            "Academic Release Model",
            "controlled academic release",
            "research artifacts",
        ):
            self.assertIn(required, text)

    def test_api_reference_covers_public_components(self) -> None:
        text = Path("docs/api-reference.md").read_text(encoding="utf-8")

        for heading in (
            "# API Reference",
            "## `blockchaincoin.consensus`",
            "## `blockchaincoin.utxo_node`",
            "## `blockchaincoin.storage`",
            "## `blockchaincoin.network`",
            "## `blockchaincoin.service`",
            "## `blockchaincoin.transport`",
            "## `blockchaincoin.runtime`",
        ):
            self.assertIn(heading, text)
        for required in (
            "ConsensusTransaction",
            "ConsensusNode",
            "SQLiteForkStore",
            "NodeService",
            "TCPPeerClient",
            "FrameAuthenticator",
            "NodeRuntimeConfig",
        ):
            self.assertIn(required, text)

    def test_incident_response_runbook_has_required_sections(self) -> None:
        text = Path("docs/incident-response.md").read_text(encoding="utf-8")

        for heading in (
            "# Incident Response",
            "## Severity",
            "## Intake",
            "## Triage",
            "## Containment",
            "## Remediation",
            "## Communication",
            "## Recovery",
            "## Postmortem",
        ):
            self.assertIn(heading, text)
        for required in (
            "artifact hashes",
            "pause releases",
            "failing regression test",
            "Ruff, Pyright, coverage, compileall, and package build",
            "security advisory",
        ):
            self.assertIn(required, text)

    def test_external_audit_checklist_has_required_scope(self) -> None:
        text = Path("docs/external-audit-checklist.md").read_text(encoding="utf-8")

        for heading in (
            "# External Audit Checklist",
            "## Scope",
            "## Required Evidence",
            "## Review Questions",
            "## Reviewer Deliverables",
            "## Out Of Scope",
        ):
            self.assertIn(heading, text)
        for required in (
            "Consensus",
            "Cryptography",
            "Storage",
            "Mempool",
            "Peer network",
            "release manifest",
            "Severity-ranked findings",
            "Claims of mainnet readiness",
        ):
            self.assertIn(required, text)

    def test_release_key_custody_runbook_has_required_controls(self) -> None:
        text = Path("docs/release-key-custody.md").read_text(encoding="utf-8")

        for heading in (
            "# Release Key Custody",
            "## Roles",
            "## Storage",
            "## Signing",
            "## Rotation",
            "## Compromise",
        ):
            self.assertIn(heading, text)
        for required in (
            "public-key fingerprint",
            "encrypted release wallet",
            "Release reviewer",
            "Freeze releases",
            "compromised fingerprint",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
