"""Command-line interface for compatibility and UTXO research workflows.

The CLI intentionally keeps human output and JSON output side by side so the
same command can support demonstrations, tests, and scripted academic
experiments. Commands that accept ``--db`` use the UTXO node path; commands that
accept ``--chain`` use the compatibility account-ledger path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from .chain import Blockchain, BlockchainError
from .crypto import Wallet, is_valid_address
from .runtime import NodeRuntime, NodeRuntimeConfig
from .runtime import RuntimeError as NodeRuntimeError
from .utxo_node import ConsensusNode, ConsensusNodeError


def _emit(args: argparse.Namespace, payload: dict[str, object], text: str) -> None:
    """Emit a command result as stable JSON or compact human text."""

    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(text)


def _load_chain(path: str) -> Blockchain:
    """Load a compatibility chain file or exit with an operator-facing message."""

    chain_path = Path(path)
    if not chain_path.exists():
        raise SystemExit(f"chain file does not exist: {path}")
    return Blockchain.load(chain_path)


def cmd_wallet_new(args: argparse.Namespace) -> None:
    """Create a plaintext or passphrase-encrypted wallet file."""

    wallet = Wallet.create()
    wallet.save(args.out, passphrase=args.passphrase)
    _emit(
        args,
        {"address": wallet.address, "wallet": args.out},
        f"created wallet {wallet.address}",
    )


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize either a compatibility chain file or UTXO node database."""

    if getattr(args, "db", None):
        cmd_utxo_init(args)
        return
    wallet = Wallet.load(args.genesis_wallet)
    chain = Blockchain.create(
        genesis_allocations={wallet.address: args.amount},
        difficulty=args.difficulty,
        mining_reward=args.reward,
    )
    chain.save(args.chain)
    _emit(
        args,
        {"address": wallet.address, "amount": args.amount, "chain": args.chain},
        f"initialized chain with {args.amount} coins for {wallet.address}",
    )


def cmd_balance(args: argparse.Namespace) -> None:
    """Report a wallet balance from the selected chain backend."""

    if getattr(args, "db", None):
        cmd_utxo_status(args)
        return
    chain = _load_chain(args.chain)
    wallet = Wallet.load(args.wallet)
    confirmed = chain.balance_of(wallet.address)
    pending = chain.balance_of(wallet.address, include_mempool=True)
    _emit(
        args,
        {"address": wallet.address, "confirmed": confirmed, "pending": pending},
        f"address:   {wallet.address}\nconfirmed: {confirmed}\npending:   {pending}",
    )


def cmd_send(args: argparse.Namespace) -> None:
    """Create and submit a transaction on the selected chain backend."""

    if getattr(args, "db", None):
        cmd_utxo_send(args)
        return
    chain = _load_chain(args.chain)
    sender = Wallet.load(args.from_wallet)
    recipient = Wallet.load(args.to_wallet).address if args.to_wallet else args.to_address
    if not recipient:
        raise SystemExit("provide --to-wallet or --to-address")
    if not is_valid_address(recipient):
        raise SystemExit("recipient address is invalid")
    tx = chain.create_transaction(sender, recipient, args.amount, args.fee)
    txid = chain.add_transaction(tx)
    chain.save(args.chain)
    _emit(args, {"txid": txid}, f"queued transaction {txid}")


def cmd_mine(args: argparse.Namespace) -> None:
    """Mine pending transactions on the selected chain backend."""

    if getattr(args, "db", None):
        cmd_utxo_mine(args)
        return
    chain = _load_chain(args.chain)
    miner = Wallet.load(args.miner_wallet)
    block = chain.mine_pending_transactions(miner.address)
    chain.save(args.chain)
    _emit(
        args,
        {"block": block.index, "hash": block.hash, "transactions": len(block.transactions)},
        f"mined block #{block.index}\nhash: {block.hash}\ntransactions: {len(block.transactions)}",
    )


def cmd_chain(args: argparse.Namespace) -> None:
    """Print high-level chain status for compatibility or UTXO storage."""

    if getattr(args, "db", None):
        cmd_utxo_status(args)
        return
    chain = _load_chain(args.chain)
    if args.json:
        print(
            json.dumps(
                {
                    "valid": chain.is_valid(),
                    "difficulty": chain.difficulty,
                    "mining_reward": chain.mining_reward,
                    "blocks": len(chain.blocks),
                    "mempool": len(chain.mempool),
                    "tip": chain.last_block.hash,
                },
                sort_keys=True,
            )
        )
        return
    print(f"valid: {chain.is_valid()}")
    print(f"difficulty: {chain.difficulty}")
    print(f"mining reward: {chain.mining_reward}")
    print(f"blocks: {len(chain.blocks)}")
    print(f"mempool: {len(chain.mempool)}")
    for block in chain.blocks:
        print(
            f"#{block.index} {block.hash[:16]}... txs={len(block.transactions)} nonce={block.nonce}"
        )


def _runtime_config(args: argparse.Namespace) -> NodeRuntimeConfig:
    """Translate argparse fields into validated runtime configuration."""

    peer_auth_key = getattr(args, "peer_auth_key", None)
    return NodeRuntimeConfig(
        db_path=Path(args.db),
        network=args.network,
        node_id=args.node_id,
        host=args.host,
        port=args.port,
        peer_auth_key=(peer_auth_key or "").encode("utf-8"),
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )


def _wallet_or_address(args: argparse.Namespace) -> str:
    """Resolve a query target from either a wallet file or literal address."""

    if args.wallet:
        return Wallet.load(args.wallet).address
    if args.address:
        if not is_valid_address(args.address):
            raise SystemExit("address is invalid")
        return args.address
    raise SystemExit("provide --wallet or --address")


def cmd_utxo_init(args: argparse.Namespace) -> None:
    """Initialize a UTXO node database with one genesis allocation."""

    wallet = Wallet.load(args.genesis_wallet)
    runtime = NodeRuntime.create(
        _runtime_config(args),
        {wallet.address: args.amount},
    )
    try:
        payload = {
            "db": args.db,
            "network": args.network,
            "node_id": args.node_id,
            "tip": runtime.node.tip_hash,
            "height": runtime.node.height,
        }
        _emit(
            args,
            payload,
            f"initialized UTXO node {args.node_id} at height {runtime.node.height}",
        )
    finally:
        runtime.stop()


def cmd_utxo_status(args: argparse.Namespace) -> None:
    """Report UTXO node height, tip, supply, mempool, and optional balance."""

    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        payload = {
            "height": node.height,
            "tip": node.tip_hash,
            "utxos": len(node.state.utxos),
            "supply": node.state.utxos.total(),
            "mempool": len(node.mempool),
        }
        if args.wallet:
            wallet = Wallet.load(args.wallet)
            balance = sum(output.output.amount for output in node.spendable_outputs(wallet.address))
            payload["address"] = wallet.address
            payload["balance"] = balance
        _emit(
            args,
            payload,
            (
                f"height: {payload['height']}\n"
                f"tip: {payload['tip']}\n"
                f"utxos: {payload['utxos']}\n"
                f"supply: {payload['supply']}\n"
                f"mempool: {payload['mempool']}"
            ),
        )
    finally:
        node.close()


def cmd_utxo_utxos(args: argparse.Namespace) -> None:
    """List spendable UTXOs for a wallet or address."""

    address = _wallet_or_address(args)
    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        payload = node.spendable_summary(address)
        utxos = cast(list[dict[str, object]], payload["utxos"])
        if utxos:
            text = "\n".join(
                f"{item['txid']}:{item['index']} amount={item['amount']}" for item in utxos
            )
        else:
            text = f"no spendable UTXOs for {address}"
        _emit(args, payload, text)
    finally:
        node.close()


def cmd_utxo_send(args: argparse.Namespace) -> None:
    """Create, sign, validate, and persist a UTXO mempool transaction."""

    sender = Wallet.load(args.from_wallet)
    recipient = Wallet.load(args.to_wallet).address if args.to_wallet else args.to_address
    if not recipient:
        raise SystemExit("provide --to-wallet or --to-address")
    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        transaction = node.create_transaction(sender, recipient, args.amount, args.fee)
        txid = node.submit_transaction(transaction)
        payload = {
            "txid": txid,
            "inputs": len(transaction.inputs),
            "outputs": len(transaction.outputs),
        }
        _emit(args, payload, f"queued UTXO transaction {txid}")
    finally:
        node.close()


def cmd_utxo_mempool(args: argparse.Namespace) -> None:
    """Inspect, prune, or clear the persistent UTXO mempool."""

    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        if args.clear:
            removed = node.clear_mempool()
            _emit(args, {"removed": removed}, f"cleared {removed} UTXO mempool transaction(s)")
            return
        if args.prune:
            removed = node.prune_mempool()
            _emit(args, {"removed": removed}, f"pruned {removed} UTXO mempool transaction(s)")
            return
        transactions = node.mempool_summary()
        payload = {"count": len(transactions), "transactions": transactions}
        if transactions:
            text = "\n".join(
                (
                    f"{item['txid']} inputs={item['inputs']} outputs={item['outputs']} "
                    f"fee={item['fee']} valid={item['valid']}"
                )
                for item in transactions
            )
        else:
            text = "UTXO mempool is empty"
        _emit(args, payload, text)
    finally:
        node.close()


def cmd_utxo_block(args: argparse.Namespace) -> None:
    """Inspect a UTXO block by active-chain height or block hash."""

    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        try:
            payload = node.block_summary(block_hash=args.hash, height=args.height)
        except ConsensusNodeError as exc:
            raise SystemExit(str(exc)) from exc
        text = (
            f"block #{payload['height']} {payload['hash']}\n"
            f"transactions: {payload['transaction_count']}\n"
            f"active: {payload['active']}"
        )
        _emit(args, payload, text)
    finally:
        node.close()


def cmd_utxo_tx(args: argparse.Namespace) -> None:
    """Inspect a confirmed or mempool UTXO transaction by txid."""

    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        try:
            payload = node.transaction_summary(args.txid)
        except ConsensusNodeError as exc:
            raise SystemExit(str(exc)) from exc
        location = f" height={payload['height']}" if payload["status"] == "confirmed" else ""
        text = (
            f"{payload['txid']} status={payload['status']}{location} "
            f"outputs={payload['output_count']} value={payload['output_value']}"
        )
        _emit(args, payload, text)
    finally:
        node.close()


def cmd_utxo_mine(args: argparse.Namespace) -> None:
    """Mine a UTXO block paying fees and subsidy to a wallet."""

    wallet = Wallet.load(args.miner_wallet)
    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        stored = node.mine_block(wallet.address)
        payload = {
            "height": stored.block.height,
            "hash": stored.block.hash,
            "cumulative_work": stored.cumulative_work,
        }
        _emit(
            args,
            payload,
            f"mined UTXO block #{stored.block.height}\nhash: {stored.block.hash}",
        )
    finally:
        node.close()


def cmd_utxo_serve(args: argparse.Namespace) -> None:
    """Start the UTXO TCP peer service for the lifetime of the command."""

    runtime = NodeRuntime.open(_runtime_config(args))
    try:
        runtime.start()
        host, port = runtime.address
        _emit(
            args,
            {"host": host, "port": port, "height": runtime.node.height},
            f"serving UTXO node on {host}:{port}",
        )
    finally:
        runtime.stop()


def _add_utxo_runtime_args(parser: argparse.ArgumentParser) -> None:
    """Add runtime flags shared by UTXO initialization and serving commands."""

    parser.add_argument("--db", required=True)
    parser.add_argument("--network", default="regtest")
    parser.add_argument("--node-id", default="node")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--peer-auth-key", required=True)
    parser.add_argument("--difficulty", type=int, default=0)
    parser.add_argument("--reward", type=int, default=50)


def build_parser() -> argparse.ArgumentParser:
    """Build the full CLI parser and attach command handlers."""

    parser = argparse.ArgumentParser(prog="blockchaincoin")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    subparsers = parser.add_subparsers(required=True)

    wallet = subparsers.add_parser("wallet")
    wallet_subparsers = wallet.add_subparsers(required=True)
    wallet_new = wallet_subparsers.add_parser("new")
    wallet_new.add_argument("--out", required=True)
    wallet_new.add_argument("--passphrase")
    wallet_new.set_defaults(func=cmd_wallet_new)

    init = subparsers.add_parser("init")
    init_store = init.add_mutually_exclusive_group(required=True)
    init_store.add_argument("--chain")
    init_store.add_argument("--db")
    init.add_argument("--genesis-wallet", required=True)
    init.add_argument("--amount", type=int, default=100)
    init.add_argument("--difficulty", type=int, default=0)
    init.add_argument("--reward", type=int, default=50)
    init.add_argument("--network", default="regtest")
    init.add_argument("--node-id", default="node")
    init.add_argument("--host", default="127.0.0.1")
    init.add_argument("--port", type=int, default=0)
    init.add_argument("--peer-auth-key", required=False)
    init.set_defaults(func=cmd_init)

    balance = subparsers.add_parser("balance")
    balance_store = balance.add_mutually_exclusive_group(required=True)
    balance_store.add_argument("--chain")
    balance_store.add_argument("--db")
    balance.add_argument("--wallet", required=True)
    balance.add_argument("--difficulty", type=int, default=0)
    balance.add_argument("--reward", type=int, default=50)
    balance.set_defaults(func=cmd_balance)

    send = subparsers.add_parser("send")
    send_store = send.add_mutually_exclusive_group(required=True)
    send_store.add_argument("--chain")
    send_store.add_argument("--db")
    send.add_argument("--from-wallet", required=True)
    send.add_argument("--to-wallet")
    send.add_argument("--to-address")
    send.add_argument("--amount", type=int, required=True)
    send.add_argument("--fee", type=int, default=1)
    send.add_argument("--difficulty", type=int, default=0)
    send.add_argument("--reward", type=int, default=50)
    send.set_defaults(func=cmd_send)

    mine = subparsers.add_parser("mine")
    mine_store = mine.add_mutually_exclusive_group(required=True)
    mine_store.add_argument("--chain")
    mine_store.add_argument("--db")
    mine.add_argument("--miner-wallet", required=True)
    mine.add_argument("--difficulty", type=int, default=0)
    mine.add_argument("--reward", type=int, default=50)
    mine.set_defaults(func=cmd_mine)

    show_chain = subparsers.add_parser("chain")
    chain_store = show_chain.add_mutually_exclusive_group(required=True)
    chain_store.add_argument("--chain")
    chain_store.add_argument("--db")
    show_chain.add_argument("--wallet")
    show_chain.add_argument("--difficulty", type=int, default=0)
    show_chain.add_argument("--reward", type=int, default=50)
    show_chain.set_defaults(func=cmd_chain)

    utxo_init = subparsers.add_parser("utxo-init")
    _add_utxo_runtime_args(utxo_init)
    utxo_init.add_argument("--genesis-wallet", required=True)
    utxo_init.add_argument("--amount", type=int, default=100)
    utxo_init.set_defaults(func=cmd_utxo_init)

    utxo_status = subparsers.add_parser("utxo-status")
    utxo_status.add_argument("--db", required=True)
    utxo_status.add_argument("--wallet")
    utxo_status.add_argument("--difficulty", type=int, default=0)
    utxo_status.add_argument("--reward", type=int, default=50)
    utxo_status.set_defaults(func=cmd_utxo_status)

    utxo_utxos = subparsers.add_parser("utxo-utxos")
    utxo_utxos.add_argument("--db", required=True)
    utxo_utxos.add_argument("--wallet")
    utxo_utxos.add_argument("--address")
    utxo_utxos.add_argument("--difficulty", type=int, default=0)
    utxo_utxos.add_argument("--reward", type=int, default=50)
    utxo_utxos.set_defaults(func=cmd_utxo_utxos)

    utxo_send = subparsers.add_parser("utxo-send")
    utxo_send.add_argument("--db", required=True)
    utxo_send.add_argument("--from-wallet", required=True)
    utxo_send.add_argument("--to-wallet")
    utxo_send.add_argument("--to-address")
    utxo_send.add_argument("--amount", type=int, required=True)
    utxo_send.add_argument("--fee", type=int, default=1)
    utxo_send.add_argument("--difficulty", type=int, default=0)
    utxo_send.add_argument("--reward", type=int, default=50)
    utxo_send.set_defaults(func=cmd_utxo_send)

    utxo_mempool = subparsers.add_parser("utxo-mempool")
    utxo_mempool.add_argument("--db", required=True)
    utxo_mempool.add_argument("--difficulty", type=int, default=0)
    utxo_mempool.add_argument("--reward", type=int, default=50)
    mempool_actions = utxo_mempool.add_mutually_exclusive_group()
    mempool_actions.add_argument("--prune", action="store_true")
    mempool_actions.add_argument("--clear", action="store_true")
    utxo_mempool.set_defaults(func=cmd_utxo_mempool)

    utxo_block = subparsers.add_parser("utxo-block")
    utxo_block.add_argument("--db", required=True)
    block_selector = utxo_block.add_mutually_exclusive_group(required=True)
    block_selector.add_argument("--hash")
    block_selector.add_argument("--height", type=int)
    utxo_block.add_argument("--difficulty", type=int, default=0)
    utxo_block.add_argument("--reward", type=int, default=50)
    utxo_block.set_defaults(func=cmd_utxo_block)

    utxo_tx = subparsers.add_parser("utxo-tx")
    utxo_tx.add_argument("--db", required=True)
    utxo_tx.add_argument("--txid", required=True)
    utxo_tx.add_argument("--difficulty", type=int, default=0)
    utxo_tx.add_argument("--reward", type=int, default=50)
    utxo_tx.set_defaults(func=cmd_utxo_tx)

    utxo_mine = subparsers.add_parser("utxo-mine")
    utxo_mine.add_argument("--db", required=True)
    utxo_mine.add_argument("--miner-wallet", required=True)
    utxo_mine.add_argument("--difficulty", type=int, default=0)
    utxo_mine.add_argument("--reward", type=int, default=50)
    utxo_mine.set_defaults(func=cmd_utxo_mine)

    utxo_serve = subparsers.add_parser("utxo-serve")
    _add_utxo_runtime_args(utxo_serve)
    utxo_serve.set_defaults(func=cmd_utxo_serve)

    return parser


def main() -> None:
    """CLI entry point used by the installed console script."""

    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (BlockchainError, ConsensusNodeError, NodeRuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
