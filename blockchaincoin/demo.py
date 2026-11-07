"""A repeatable, synthetic ten-person payment workload using real node operations."""

from __future__ import annotations

from typing import Any

from .crypto import Wallet
from .utxo_node import ConsensusNode, ConsensusNodeError

DEMO_BALANCES = {
    "Alice": 2400,
    "Bob": 1800,
    "Carla": 1000,
    "Daniel": 1000,
    "Emma": 1000,
    "Felix": 1000,
    "Grace": 1000,
    "Hassan": 1000,
    "Isla": 1000,
    "James": 1000,
}


def simulate_payments(
    node: ConsensusNode, wallets: dict[str, Wallet], miner: Wallet
) -> dict[str, Any]:
    """Run 30 payments, check accounting and rejection, and return public evidence."""
    expected = dict(DEMO_BALANCES)
    names = list(expected)
    total_sent = 0
    for round_index, offset in enumerate((1, 3, 7)):
        for index, sender in enumerate(names):
            recipient = names[(index + offset) % len(names)]
            amount = 15 + index * 7 + round_index * 20
            tx = node.create_transaction(wallets[sender], wallets[recipient].address, amount, 1)
            node.submit_transaction(tx)
            node.mine_block(miner.address)
            expected[sender] -= amount + 1
            expected[recipient] += amount
            total_sent += amount
    rejected = False
    try:
        node.create_transaction(wallets["Alice"], wallets["Bob"].address, 1_000_000, 1)
    except ConsensusNodeError:
        rejected = True
    balances = {
        name: node.spendable_summary(wallet.address)["balance"] for name, wallet in wallets.items()
    }
    assert balances == expected, "Payment balances did not reconcile"
    assert rejected, "Overspending was accepted"
    assert not node.mempool, "Confirmed payments remained pending"
    assert node.state.utxos.total() == sum(DEMO_BALANCES.values()) + 30 * 50
    assert node.spendable_summary(miner.address)["balance"] == 30 * 51
    return {
        "scenario": "10 synthetic people; 3 rounds of 10 signed payments",
        "users": 10,
        "completed_payments": 30,
        "payment_volume_bcc": total_sent,
        "fees_bcc": 30,
        "opening_user_balances_bcc": DEMO_BALANCES,
        "closing_user_balances_bcc": balances,
        "user_funds_bcc": sum(expected.values()),
        "confirmation_wallet_bcc": 1530,
        "total_supply_bcc": node.state.utxos.total(),
        "height": node.height,
        "pending_payments": 0,
        "overspend_rejected": rejected,
        "accounting_reconciled": True,
    }
