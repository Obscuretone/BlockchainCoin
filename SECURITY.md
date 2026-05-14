# Security Policy

BlockchainCoin is not ready to secure public funds.

The codebase is moving toward production-grade node architecture, but it has not had external cryptographic review, economic review, adversarial protocol testing, or operational hardening.

## Reporting Issues

For now, report suspected vulnerabilities privately to the repository owner. Include:

- Affected version or commit
- Reproduction steps
- Impact assessment
- Any suggested mitigation

Security incidents should follow the runbook in `docs/incident-response.md`.

## Current Security Boundaries

- Wallet signatures use Ed25519 from `cryptography`.
- UTXO transaction IDs, Merkle leaves, and block header hashes use canonical binary serialization.
- Consensus node databases persist chain parameters and reject reopen attempts with mismatched consensus settings.
- Peer relay messages for UTXO transactions/blocks, persisted local mempool transactions, and consensus block-store payloads use the canonical binary consensus codec.
- TCP peer frames require an HMAC authentication tag; unauthenticated frames are rejected before peer-message dispatch.
- Header-first peer sync messages validate block locator hashes, stop hashes, compact header hashes, and response size limits before dispatch.
- Peer header batches must form a connected, sequential header chain with supported block versions and valid proof-of-work before session acceptance.
- Validated peer headers schedule block downloads only for hashes absent from the local block store and service cache, and only when the header batch connects to a known local block.
- In-flight block download tracking suppresses duplicate block requests and clears peer-owned work on timeout, disconnect, ban, or successful import.
- Targeted block download requests prefer less-busy connected peers known to have advertised the needed header.
- Block download requests are emitted in bounded batches rather than unbounded `getdata` payloads.
- Expired block downloads can be retried as fresh bounded requests, preferring alternate peers that advertised the same header.
- Per-peer in-flight block download caps prevent unbounded request assignment to one connected peer.
- Peer inventory messages and long-lived session inventory sets are bounded.
- Per-peer sync progress counters expose requested, completed, timed-out, and failed block download work for operational inspection.
- Peer sync progress counters can be persisted in SQLite so quality signals survive service restarts.
- Block download peer selection uses persisted sync quality scores so peers with more timeouts or failures are deprioritized.
- UTXO consensus transactions and blocks are rejected when they declare an unsupported consensus version.
- UTXO supply invariants and replay/double-spend rejection have property-test coverage in addition to example-based tests.
- The node store uses SQLite WAL for durable local block persistence.
- Loaded chains and imported blocks are validated before use.
- Imported blocks that change the active branch trigger active-state rebuild, active UTXO index refresh, and mempool revalidation against the new branch.
- UTXO snapshots are persisted per tip so inactive competing branches can retain indexed state without becoming active.
- Spent-outpoint indexes are persisted per tip so competing branches can retain branch-local spend views.
- Branch-local UTXO and spent-outpoint queries can inspect inactive competing tips without mutating active state.
- Blocks that fail import validation are remembered in a persistent invalid-block cache and rejected before repeat validation work.
- The UTXO node applies configurable local mempool policy limits before storing or relaying pending transactions, including absolute minimum fees and size-aware minimum fee rates.
- Candidate block assembly prioritizes higher fee-rate mempool transactions while preserving dependency-valid ordering.
- When the mempool transaction-count cap is reached, higher fee-rate transactions can evict lower fee-rate entries without invalidating remaining parent/child dependencies.
- The local minimum relay fee-rate can rise deterministically with mempool occupancy; this is node policy, not consensus.
- Local mempool policy rejections use a dedicated error path and peer-service rejection reason instead of being treated as consensus failures.
- Pending UTXO transactions that directly conflict on inputs can only replace existing mempool entries when replacement is enabled and the configured fee bump is met.
- Peer misbehavior scores can trigger bans, remove in-process sessions, and instruct the TCP transport to close the offending connection.
- TCP relay fanout sends transaction and block inventory to other connected long-lived peers while excluding the source connection.
- TCP inbound connections are admitted through peer-manager inbound limits before message dispatch.
- UTXO snapshot indexes are treated as caches and rebuilt when they do not match replayed block-store state.
- The package and tests are kept Pyright/Pylance-clean.
- The package test suite enforces 100% line and branch coverage.
- Release artifacts can be recorded in deterministic SHA-256 manifests and signed with detached Ed25519 manifest signatures for rebuild comparison. Verifiers can pin the expected release public-key fingerprint.
- CI is configured to run Ruff, Pyright, 100% coverage, compileall, and package builds across Python 3.11-3.13 with pinned release constraints and SHA-pinned GitHub Actions.
- Fuzz harnesses cover consensus binary codecs and authenticated peer frames.
- Incident response handling is documented for severity, triage, containment, remediation, communication, recovery, and postmortems.
- External audit scope and reviewer deliverables are documented in `docs/external-audit-checklist.md`.
- Release-key custody and public-key rotation are documented in `docs/release-key-custody.md`.

## Out of Scope Today

- Public mainnet value
- Large-scale peer-to-peer adversarial networking
- Custodial wallet operations
- Consensus compatibility with other implementations
- Resistance to large-scale denial-of-service attacks
