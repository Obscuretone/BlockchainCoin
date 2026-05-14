# Production Roadmap

This project should become a real node, not a decorated demo. The immediate direction is to keep moving consensus-critical behavior into explicit node components, then harden persistence, networking, operations, and release processes around that core.

## Phase 1: Durable Local Node

Status: complete for the current local-node target.

Done:

- SQLite WAL block store
- Schema-versioned node database
- Append-only canonical chain storage
- Cumulative work tracking
- Restart validation
- 100% package coverage gate

Remaining:

- None for this phase; future durability work is tracked in later persistence and operations phases.

## Phase 2: Consensus-State Rewrite

Status: in progress, with the core UTXO path implemented and guarded.

Done:

- UTXO consensus-state module
- Signed-input validation
- Explicit output validation
- Fee calculation and double-spend rejection
- Maximum supply checks
- Block-level UTXO validation and state rebuilds
- UTXO-native local node with durable consensus block storage
- Durable chain-parameter records and chain-id derivation for reopen safety
- Consensus node migrated onto fork-aware storage and fork choice
- Persistent UTXO snapshot index for the active tip
- UTXO snapshot cache mismatch detection and rebuild from block-store replay
- Persistent local mempool storage with restart pruning
- Operator CLI for UTXO inspection, mempool inspection, block lookup, and transaction lookup
- Primary CLI verbs for init, send, mine, balance, and chain status can target the UTXO consensus node via `--db` while legacy account-chain usage remains available via `--chain`
- Configurable mempool admission policy for transaction count, transaction byte size, input/output fanout, and minimum relay fee
- Minimum relay fee-rate policy for size-aware mempool admission
- Fee-rate-prioritized block assembly with dependency-safe mempool ordering
- Fee-rate-based mempool eviction at transaction-count capacity, with dependency-safe eviction candidate selection
- Deterministic dynamic minimum fee-rate floor based on local mempool occupancy
- Explicit transaction replacement policy for direct input conflicts with a configurable fee bump
- Separate mempool policy rejection path from consensus validation errors
- Canonical binary serialization for UTXO transaction IDs, Merkle tree leaves, and block header hashes
- Canonical binary peer payloads for UTXO transaction/block relay messages
- Canonical binary local mempool transaction persistence with legacy JSON fallback
- Canonical binary consensus block-store payloads with legacy JSON fallback
- Explicit supported-version gates for consensus transactions and blocks
- Property tests for UTXO supply invariants and replay/double-spend rejection
- Pyright/Pylance clean package and test suite

Remaining:

- Complete account-ledger CLI deprecation/removal after compatibility window
- Broaden fee-market design beyond deterministic local policy

## Phase 3: Fork Choice

Status: in progress.

Done:

- Store competing branches, not only the active tip
- Select active chain by cumulative work
- Reconstruct active chain from selected tip
- Validate selected branches by rebuilding UTXO state
- Persistent invalid-block cache for remembered import rejections
- Explicit reorg detection on imported blocks with active-state rebuild, active-tip index refresh, and mempool revalidation/pruning
- Persist indexed spend/state sets per tip
- Per-tip spent-outpoint indexes for competing branch spend views
- Branch-local indexed queries for exact spent outpoints and address UTXOs

Remaining:

- Broaden per-tip indexes beyond UTXO/spend views as new query surfaces emerge

## Phase 4: Peer Network

Status: in progress.

Done:

- Length-prefixed HMAC-authenticated peer frame protocol with no unauthenticated frame fallback
- Versioned peer message framing
- Inventory, transaction, and block payload messages
- Header-first sync protocol foundation with `getheaders` locators and compact `headers` responses
- Header-chain validation for connected height order, supported versions, and proof-of-work before accepting peer header batches
- Header-driven block download scheduling that turns missing validated headers into deterministic `getdata` requests only when batches connect to known local blocks
- In-flight block download tracking with duplicate suppression, timeout expiry, peer-disconnect cleanup, and block-arrival cleanup
- Peer-selected block download routing that targets the least-busy known peer instead of always requesting from the header sender
- Bounded block download request batches that split large missing-header runs into deterministic `getdata` chunks per selected peer
- Expired block download retry policy that reschedules bounded requests, preferring alternate known peers when available
- Per-peer in-flight block download caps for multi-peer scheduling without overloading a single source
- Bounded peer inventory tracking for session memory safety
- Per-peer sync progress accounting for requested, completed, timed-out, and failed block downloads
- SQLite-backed peer sync progress persistence for durable peer quality inputs
- Peer quality scoring policy based on persisted sync progress, used during block download peer selection
- Version handshake and network IDs
- Peer session state machine
- Ping/pong liveness tracking
- Inventory request tracking
- Peer address manager
- Connection limits, retry backoff, bans, and eviction candidates
- TCP inbound admission enforcement through peer-manager connection limits
- In-process node service for tx/block relay behavior
- TCP transport adapter and client
- Long-lived TCP peer fanout for transaction and block inventory relay
- Runtime lifecycle wrapper for consensus node, service, and TCP server
- Misbehavior scoring with ban-threshold peer eviction enforced by node service and TCP transport disconnects

Remaining:

- None for this phase's current relay foundation; future peer sync work should extend peer sync into richer operational observability and external review.

## Phase 5: Operations and Security

Status: started through local quality gates; release operations remain.

Done:

- Ruff formatting and linting gate
- Pyright/Pylance static type gate
- 100% line and branch coverage gate
- Buildable source distribution and wheel
- Pinned release constraints, reproducible build notes, deterministic artifact manifest helper, and detached release manifest signatures
- Release public-key fingerprints, verifier-side fingerprint checks, and release-key custody/rotation runbook
- GitHub Actions quality workflow for Ruff, Pyright, coverage, compileall, and constrained package build across supported Python versions with SHA-pinned GitHub Actions
- Fuzz harnesses for peer frames and consensus binary codecs
- Incident response runbook for severity, triage, containment, remediation, communications, recovery, and postmortems
- External audit checklist for consensus, cryptography, storage, mempool, peer-network, CLI, operations, evidence, and deliverables

Remaining:

- None for this phase's current release-operations target; future work should track external audit findings and operator feedback.
