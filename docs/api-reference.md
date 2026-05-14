# API Reference

This document is a source-oriented guide for maintainers and reviewers. It summarizes the public modules, classes, and functions that make up the node. Docstrings in the package provide the closer line-by-line context.

## `blockchaincoin.consensus`

Consensus-critical UTXO rules and canonical codecs.

- `OutPoint`: identifies one transaction output by transaction ID and index.
- `TxInput`: spends an outpoint and optionally carries signature witness data.
- `TxOutput`: assigns a positive amount to a `bcc_` address.
- `ConsensusTransaction`: immutable transaction with canonical `txid`, signing messages, binary encoding, and dictionary compatibility.
- `calculate_transaction_root`: calculates the Merkle root committed by a block.
- `ConsensusBlock`: block header plus ordered transactions.
- `UTXO` and `UTXOSet`: current spendable output representation.
- `ConsensusState`: validates and applies transactions, coinbase transactions, and genesis allocations while enforcing maximum supply.
- `BlockProcessor`: validates and applies blocks against expected height and parent hash.
- `rebuild_state_from_blocks`: replays an ordered chain into a fresh state.

## `blockchaincoin.utxo_node`

Durable local node API.

- `ConsensusNode.create`: initializes a database and genesis block.
- `ConsensusNode.open`: validates stored `ChainParameters`, branches, and UTXO index state before serving the node.
- `submit_transaction`: validates consensus and local mempool policy, then persists the transaction.
- `create_transaction`: selects wallet-owned UTXOs and signs a spend.
- `build_candidate_block` and `mine_block`: assemble and mine fee-aware blocks.
- `import_block`: validates, persists, applies fork choice, refreshes indexes, and prunes the mempool.
- `spendable_summary`, `block_summary`, and `transaction_summary`: operator query surfaces for balances, blocks, and transactions.
- `MempoolPolicy`: local admission, replacement, fee, and eviction policy.
- `ReorgEvent`: records active tip changes that are not simple appends.

## `blockchaincoin.storage`

SQLite-backed durable state.

- `ChainParameters`: persisted consensus parameters and derived `chain_id`.
- `SQLiteForkStore`: fork-aware block store and best-tip selection.
- `SQLiteUTXOIndex`: active and per-tip UTXO snapshots plus spent-outpoint indexes.
- `SQLiteMempoolStore`: restart cache for mempool transactions.
- `SQLiteInvalidBlockCache`: persistent invalid-block hash cache.
- `SQLitePeerSyncStore`: persistent peer sync quality counters.
- `SQLiteBlockStore` and `SQLiteConsensusStore`: legacy and linear block stores.

## `blockchaincoin.network`

Peer wire protocol.

- `FrameAuthenticator`: HMAC-SHA256 tags for peer frames.
- `InventoryVector`: compact transaction or block advertisement.
- `BlockHeader`: serializable header used by header-first sync.
- `PeerMessage`: versioned message with payload validation and frame encoding.
- `version_message`, `inventory_message`, `getheaders_message`, `headers_message`: constructors for protocol messages.
- `headers_from_payload` and `validate_header_chain`: defensive header parsing.
- `transaction_message`, `block_message`, `transaction_from_payload`, `block_from_payload`: canonical binary relay helpers.
- `read_frame`: consumes one authenticated frame from a byte buffer.

## `blockchaincoin.service`

Transport-independent peer behavior.

- `NodeService`: session management, relay, getdata, getheaders, header-driven download scheduling, misbehavior penalties, and peer-quality selection.
- `ServiceResult`: direct, targeted, relayed, and disconnect actions returned to a transport.
- `BlockDownload`: one in-flight block request.
- `PeerSyncProgress`: requested, completed, timed-out, and failed block counters.

## `blockchaincoin.transport`

Socket plumbing for peer messages.

- `ConnectionBuffer`: accumulates TCP bytes until complete authenticated frames can be decoded.
- `NodeTCPServerAdapter`: threaded TCP server adapter around `NodeService`.
- `TCPPeerClient`: blocking client for tests and local workflows.

## `blockchaincoin.runtime`

Lifecycle facade.

- `NodeRuntimeConfig`: validated operator configuration.
- `NodeRuntime.create`: create a new chain, peer service, and TCP server.
- `NodeRuntime.open`: open an existing chain with matching parameters.
- `start`, `stop`, `mine`, and `address`: lifecycle and convenience methods.

## `blockchaincoin.crypto`

Wallet and signature primitives.

- `PublicKey`: serializable public key and derived address.
- `Wallet`: Ed25519 keypair, signing, serialization, encryption, and loading.
- `sha256_hex`, `canonical_json`, `is_valid_address`, and `verify_signature`: shared cryptographic helpers.

## `blockchaincoin.release`

Release artifact helpers.

- Manifest generation records SHA-256 hashes for built artifacts.
- Detached signatures and public-key fingerprints support release verification.
- These helpers are paired with the reproducible-build and key-custody runbooks.
