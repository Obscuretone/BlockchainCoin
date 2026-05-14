# External Audit Checklist

BlockchainCoin must not be represented as safe for public-value use until an independent review has been completed and findings have been addressed. This checklist defines the minimum audit scope and evidence package.

## Scope

- Consensus: transaction validation, block validation, fork choice, reorg handling, maximum supply enforcement, coinbase and fee accounting, supported version gates, durable chain parameters, chain-id derivation, and canonical serialization.
- Cryptography: wallet key generation, signing, address derivation, signature verification, canonical signing payloads, and private-key file handling.
- Storage: SQLite schema migration, WAL usage, block stores, invalid-block cache, mempool persistence, UTXO snapshots, per-tip indexes, and peer quality persistence.
- Mempool: relay policy separation from consensus, replacement policy, fee-rate sorting, eviction, dependency ordering, and restart pruning.
- Peer network: authenticated frame parsing, message validation, handshake, liveness, bounded inventory relay, anchored header sync, block download scheduling, retry policy, misbehavior scoring, bans, inbound admission, and TCP fanout.
- CLI and operations: UTXO workflow commands, JSON outputs, runtime lifecycle, release manifests, release-key custody, CI quality gates, and incident response.

## Required Evidence

- Architecture and API documentation: `docs/architecture.md` and `docs/api-reference.md`.
- Exact source revision and release manifest.
- `pyproject.toml`, `constraints/release.txt`, generated wheel, generated source distribution, deterministic SHA-256 artifact manifest, and detached release manifest signature.
- Output from Ruff, Pyright, coverage, compileall, and package build.
- Current `docs/production-roadmap.md`, `SECURITY.md`, `docs/reproducible-builds.md`, `docs/release-key-custody.md`, and `docs/incident-response.md`.
- Any known unresolved findings, TODOs, or deliberately out-of-scope areas.

## Review Questions

- Can invalid transactions or blocks mutate durable state?
- Can competing branches corrupt active state, indexes, mempool contents, or peer download state?
- Can serialization ambiguity change transaction IDs, Merkle roots, block hashes, signing messages, or peer payloads?
- Can local mempool policy reject valid consensus transactions without confusing consensus validation?
- Can peer messages exhaust memory, bypass validation, trigger duplicate downloads, or keep stale work in flight forever?
- Can release artifacts be rebuilt and matched by digest?
- Are incident response and disclosure steps sufficient for credible operator guidance?

## Reviewer Deliverables

- Severity-ranked findings with reproduction steps.
- Affected components and threat model assumptions.
- Recommended fixes and regression tests.
- Confirmation of which areas were reviewed and which were not.
- Final acceptance or residual-risk statement after remediation.

## Out Of Scope

- Claims of mainnet readiness.
- Custodial wallet operations.
- Economic parameter adequacy.
- Compatibility with other implementations.
- Formal verification.
- Exchange, bridge, or smart-contract integrations.
