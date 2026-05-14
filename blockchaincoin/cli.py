"""Command-line interface for UTXO research workflows.

The CLI keeps human output and JSON output side by side so the same command can
support demonstrations, tests, and scripted academic experiments. All chain
commands use the UTXO consensus node and a SQLite database.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

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


def cmd_wallet_new(args: argparse.Namespace) -> None:
    """Create a plaintext or passphrase-encrypted wallet file."""

    wallet = Wallet.create()
    wallet.save(args.out, passphrase=args.passphrase)
    _emit(
        args,
        {"address": wallet.address, "wallet": args.out},
        f"created wallet {wallet.address}",
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


def cmd_init(args: argparse.Namespace) -> None:
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


def cmd_chain(args: argparse.Namespace) -> None:
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
        if getattr(args, "wallet", None):
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


def cmd_balance(args: argparse.Namespace) -> None:
    """Report a wallet balance from UTXO state."""

    node = ConsensusNode.open(
        args.db,
        difficulty=args.difficulty,
        block_subsidy=args.reward,
    )
    try:
        wallet = Wallet.load(args.wallet)
        summary = node.spendable_summary(wallet.address)
        summary["height"] = node.height
        summary["tip"] = node.tip_hash
        _emit(
            args,
            summary,
            f"address:   {wallet.address}\nconfirmed: {summary['balance']}",
        )
    finally:
        node.close()


def cmd_utxos(args: argparse.Namespace) -> None:
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


def cmd_send(args: argparse.Namespace) -> None:
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


def cmd_mempool(args: argparse.Namespace) -> None:
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


def cmd_block(args: argparse.Namespace) -> None:
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


def cmd_tx(args: argparse.Namespace) -> None:
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


def cmd_mine(args: argparse.Namespace) -> None:
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


def cmd_serve(args: argparse.Namespace) -> None:
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


def _add_node_args(parser: argparse.ArgumentParser) -> None:
    """Add node database and consensus-parameter flags."""

    parser.add_argument("--db", required=True)
    parser.add_argument("--difficulty", type=int, default=0)
    parser.add_argument("--reward", type=int, default=50)


def _add_runtime_args(parser: argparse.ArgumentParser) -> None:
    """Add runtime flags used by initialization and serving commands."""

    _add_node_args(parser)
    parser.add_argument("--network", default="regtest")
    parser.add_argument("--node-id", default="node")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--peer-auth-key", required=True)


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
    _add_runtime_args(init)
    init.add_argument("--genesis-wallet", required=True)
    init.add_argument("--amount", type=int, default=100)
    init.set_defaults(func=cmd_init)

    balance = subparsers.add_parser("balance")
    _add_node_args(balance)
    balance.add_argument("--wallet", required=True)
    balance.set_defaults(func=cmd_balance)

    send = subparsers.add_parser("send")
    _add_node_args(send)
    send.add_argument("--from-wallet", required=True)
    send.add_argument("--to-wallet")
    send.add_argument("--to-address")
    send.add_argument("--amount", type=int, required=True)
    send.add_argument("--fee", type=int, default=1)
    send.set_defaults(func=cmd_send)

    mine = subparsers.add_parser("mine")
    _add_node_args(mine)
    mine.add_argument("--miner-wallet", required=True)
    mine.set_defaults(func=cmd_mine)

    show_chain = subparsers.add_parser("chain")
    _add_node_args(show_chain)
    show_chain.add_argument("--wallet")
    show_chain.set_defaults(func=cmd_chain)

    utxos = subparsers.add_parser("utxos")
    _add_node_args(utxos)
    utxos.add_argument("--wallet")
    utxos.add_argument("--address")
    utxos.set_defaults(func=cmd_utxos)

    mempool = subparsers.add_parser("mempool")
    _add_node_args(mempool)
    mempool_actions = mempool.add_mutually_exclusive_group()
    mempool_actions.add_argument("--prune", action="store_true")
    mempool_actions.add_argument("--clear", action="store_true")
    mempool.set_defaults(func=cmd_mempool)

    block = subparsers.add_parser("block")
    _add_node_args(block)
    block_selector = block.add_mutually_exclusive_group(required=True)
    block_selector.add_argument("--hash")
    block_selector.add_argument("--height", type=int)
    block.set_defaults(func=cmd_block)

    tx = subparsers.add_parser("tx")
    _add_node_args(tx)
    tx.add_argument("--txid", required=True)
    tx.set_defaults(func=cmd_tx)

    serve = subparsers.add_parser("serve")
    _add_runtime_args(serve)
    serve.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    """CLI entry point used by the installed console script."""

    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (ConsensusNodeError, NodeRuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
