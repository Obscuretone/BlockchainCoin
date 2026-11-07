"""Native Tk desktop explorer and wallet console for local research chains."""

from __future__ import annotations

import argparse
import json
import tempfile
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from .consensus import ConsensusTransaction
from .crypto import Wallet, is_valid_address
from .demo import DEMO_BALANCES, simulate_payments
from .utxo_node import ConsensusNode, ConsensusNodeError


class DesktopSession:
    """Own local wallet keys and open a separate node handle for each operation."""

    def __init__(
        self,
        db: Path | None = None,
        difficulty: int = 0,
        reward: int = 50,
        *,
        populated: bool = False,
    ) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="blockchaincoin-desktop-")
        self.demo = db is None
        self.db = db or Path(self.temporary.name) / "demo.sqlite3"
        self.difficulty = 0 if self.demo else difficulty
        self.reward = 50 if self.demo else reward
        self.wallets: dict[str, Wallet] = {}
        self.labels: dict[str, str] = {}
        self.demo_miner = Wallet.create()
        if self.demo:
            balances = DEMO_BALANCES if populated else {"Alice": 1000, "Bob": 250}
            self.wallets = {name: Wallet.create() for name in balances}
            node = ConsensusNode.create(
                self.db,
                {self.wallets[name].address: amount for name, amount in balances.items()},
                difficulty=0,
            )
            if populated:
                simulate_payments(node, self.wallets, self.demo_miner)
            node.close()
        elif not self.db.is_file():
            self.close()
            raise ValueError("Choose an existing node database")

    def close(self) -> None:
        self.wallets.clear()
        self.temporary.cleanup()

    def execute(self, action: str = "refresh", **values: Any) -> dict[str, Any]:
        """Return public state and operation details, never private wallet material."""
        if action == "load_wallet":
            wallet = Wallet.load(values["path"], passphrase=values["passphrase"])
            name = next(
                (name for name, known in self.wallets.items() if known.address == wallet.address),
                wallet.address,
            )
            self.wallets[name] = wallet
            self.labels[name] = Path(values["path"]).stem
        node = ConsensusNode.open(
            self.db,
            difficulty=self.difficulty,
            block_subsidy=self.reward,
        )
        try:
            detail: dict[str, Any] = {}
            if action == "send":
                amount, fee = values["amount"], values["fee"]
                if type(amount) is not int or type(fee) is not int or amount <= 0 or fee < 0:
                    raise ValueError("Amount must be a positive integer; fee must be nonnegative")
                tx = node.create_transaction(
                    self.wallets[values["sender"]],
                    values["recipient"],
                    amount,
                    fee,
                )
                node.submit_transaction(tx)
                if self.demo and values.get("confirm", False):
                    node.mine_block(self.demo_miner.address)
                detail = node.transaction_summary(tx.txid)
            elif action == "mine":
                block = node.mine_block(self.wallets[values["sender"]].address).block
                detail = node.block_summary(block_hash=block.hash)
            elif action == "lookup":
                query = values["query"].strip()
                if query.isdecimal():
                    detail = node.block_summary(height=int(query))
                elif query.startswith("bcc_"):
                    detail = node.spendable_summary(query)
                else:
                    try:
                        detail = node.block_summary(block_hash=query)
                    except ConsensusNodeError:
                        detail = node.transaction_summary(query)
            elif action not in ("refresh", "load_wallet"):
                raise ValueError("Unknown desktop action")
            blocks = node.fork_choice.active_chain().blocks
            return {
                "height": node.height,
                "tip": node.tip_hash,
                "supply": node.state.utxos.total(),
                "utxos": len(node.state.utxos),
                "transaction_count": sum(len(block.transactions) for block in blocks),
                "blocks": [node.block_summary(height=b.height) for b in blocks[-20:][::-1]],
                "mempool": node.mempool_summary(),
                "wallets": [
                    {
                        "name": name,
                        "label": self.labels.get(name, name),
                        **node.spendable_summary(wallet.address),
                    }
                    for name, wallet in self.wallets.items()
                ],
                "detail": detail,
                "activity": self._activity(node),
            }
        finally:
            node.close()

    def _activity(self, node: ConsensusNode) -> list[dict[str, Any]]:
        """Translate local ledger entries into account-oriented payment history."""
        blocks = node.fork_choice.active_chain().blocks
        entries: list[tuple[ConsensusTransaction, str, float | None, int | None]] = [
            (tx, "Completed", block.timestamp, block.height)
            for block in blocks
            for tx in block.transactions
        ]
        entries.extend((tx, "Pending", None, None) for tx in node.mempool)
        outputs = {
            (tx.txid, index): output
            for tx, *_ in entries
            for index, output in enumerate(tx.outputs)
        }
        names = {
            wallet.address: self.labels.get(name, name) for name, wallet in self.wallets.items()
        }
        activity = []
        for tx, status, timestamp, height in reversed(entries):
            inputs = [
                outputs[(item.previous_output.txid, item.previous_output.index)]
                for item in tx.inputs
                if not tx.is_coinbase
            ]
            for name, wallet in self.wallets.items():
                spent = sum(item.amount for item in inputs if item.address == wallet.address)
                received = sum(item.amount for item in tx.outputs if item.address == wallet.address)
                if not spent and not received:
                    continue
                fee = (
                    sum(item.amount for item in inputs) - sum(item.amount for item in tx.outputs)
                    if not tx.is_coinbase
                    else 0
                )
                counterpart = ""
                if tx.is_coinbase:
                    title = "Opening balance" if height == 0 else "Reward received"
                elif spent:
                    targets = [
                        item.address for item in tx.outputs if item.address != wallet.address
                    ]
                    recipient = names.get(targets[0], "another account") if targets else "yourself"
                    counterpart = recipient
                    title = f"Sent to {recipient}"
                else:
                    sources = list(dict.fromkeys(item.address for item in inputs))
                    counterpart = ", ".join(
                        names.get(address, "another account") for address in sources
                    )
                    title = f"Received from {counterpart}"
                activity.append(
                    {
                        "account": name,
                        "txid": tx.txid,
                        "title": title,
                        "amount": received - spent,
                        "status": status,
                        "timestamp": timestamp,
                        "kind": "funding" if tx.is_coinbase else "sent" if spent else "received",
                        "counterpart": counterpart,
                        "fee": fee if spent else 0,
                        "confirmations": node.height - height + 1 if height is not None else 0,
                    }
                )
        return activity


BG, PANEL, LINE = "#f4f6f5", "#ffffff", "#dfe7e3"
TEXT, MUTED, ACCENT = "#152d29", "#6a7e77", "#176b54"


class DesktopApp:
    """Simple account and payment views, with technical tools kept separate."""

    def __init__(self, root: tk.Tk, session: DesktopSession) -> None:
        self.root, self.session = root, session
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future: Future[dict[str, Any]] | None = None
        self.closing = False
        self.state: dict[str, Any] = {}
        self.dialog: tk.Toplevel | None = None
        self.payment: dict[str, Any] = {}
        self.payee_buttons: list[ttk.Button] = []
        self.sender = tk.StringVar()
        self.recipient = tk.StringVar()
        self.amount, self.fee = tk.StringVar(), tk.StringVar(value="1")
        self.history_search = tk.StringVar()
        self.history_filter = tk.StringVar(value="All activity")
        self.query = tk.StringVar()
        self.status = tk.StringVar(value="Opening your account...")
        self.root.title("BlockchainCoin | Your money")
        self.root.geometry("1120x900")
        self.root.minsize(1040, 850)
        self.root.configure(background=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._build()
        self._build_tools()
        self.run()

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Helvetica", 12))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("Balance.TFrame", background="#e2eee7")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Helvetica", 20, "bold"))
        style.configure("Balance.TLabel", background="#e2eee7", font=("Helvetica", 20, "bold"))
        style.configure("BalanceSmall.TLabel", background="#e2eee7", foreground=MUTED)
        style.configure(
            "TButton", padding=(14, 8), background=PANEL, foreground=TEXT, borderwidth=1
        )
        style.map("TButton", background=[("active", "#e3ebe6")])
        style.configure("Primary.TButton", background=ACCENT, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#12523f")])
        style.configure("Person.TButton", padding=(10, 4), anchor="w", font=("Helvetica", 11))
        style.configure("Link.TButton", background=BG, foreground=MUTED, borderwidth=0, padding=6)
        style.configure(
            "TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=8
        )
        style.configure("TCombobox", fieldbackground=PANEL, foreground=TEXT, padding=6)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL)],
            selectbackground=[("readonly", PANEL)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=38,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading", background=BG, foreground=MUTED, padding=12, relief="flat"
        )
        style.map("Treeview", background=[("selected", "#e0ede5")], foreground=[("selected", TEXT)])

    def _label(self, parent: tk.Misc, text: str, style: str = "TLabel") -> ttk.Label:
        label = ttk.Label(parent, text=text, style=style)
        label.pack(anchor="w", pady=(0, 10))
        return label

    def _table(self, parent: tk.Misc, columns: tuple[str, ...], height: int = 5) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame, columns=columns, show="headings", height=height, selectmode="browse"
        )
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=140, minwidth=80)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        return tree

    def _build(self) -> None:
        shell = ttk.Frame(self.root, padding=(24, 14))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="◈  BlockchainCoin", font=("Helvetica", 16, "bold")).pack(
            side="left"
        )
        self.tools_button = ttk.Button(
            header, text="More", style="Link.TButton", command=self.open_tools
        )
        self.tools_button.pack(side="right")
        account_row = ttk.Frame(shell)
        account_row.pack(fill="x", pady=(0, 12))
        self.account_choice = tk.StringVar()
        self.wallet_picker = ttk.Combobox(
            account_row, textvariable=self.account_choice, state="readonly", width=28
        )
        self.wallet_picker.pack(side="left")
        self.wallet_picker.bind("<<ComboboxSelected>>", lambda event: self.change_account())
        self.refresh_button = ttk.Button(
            account_row, text="Refresh", style="Link.TButton", command=self.run
        )
        self.refresh_button.pack(side="right")
        overview = ttk.Frame(shell)
        overview.pack(fill="x", pady=(0, 14))
        card = ttk.Frame(overview, padding=(14, 8), style="Balance.TFrame")
        card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(card, text="Completed balance", style="BalanceSmall.TLabel").pack(anchor="w")
        self.balance_label = ttk.Label(card, text="... BCC", style="Balance.TLabel")
        self.balance_label.pack(anchor="w", pady=4)
        self.balance_note = ttk.Label(card, text="", style="BalanceSmall.TLabel")

        summaries = ttk.Frame(overview)
        summaries.pack(side="left", fill="both", expand=True)
        self.summary_labels = {}
        for title in ("Money in", "Money out", "Payments"):
            tile = ttk.Frame(summaries, padding=(14, 8), style="Card.TFrame")
            tile.pack(side="left", fill="both", expand=True, padx=(0, 8))
            ttk.Label(tile, text=title, background=PANEL, foreground=MUTED).pack(anchor="w")
            value = ttk.Label(tile, text="—", background=PANEL, font=("Helvetica", 20, "bold"))
            value.pack(anchor="w", pady=(5, 0))
            self.summary_labels[title] = value
        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(0, 18))
        self.send_button = ttk.Button(
            actions, text="↑  Send money", style="Primary.TButton", command=self.open_send
        )
        self.send_button.pack(side="left", padx=(0, 12))
        self.receive_button = ttk.Button(actions, text="↓  Receive money", command=self.receive)
        self.receive_button.pack(side="left")
        self.load_button = ttk.Button(
            actions, text="Open wallet", style="Link.TButton", command=self.load_wallet
        )
        self.load_button.pack(side="right")
        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)
        history = ttk.Frame(body)
        history.pack(side="left", fill="both", expand=True, padx=(0, 24))
        self._label(history, "Recent transactions", "Title.TLabel")
        filters = ttk.Frame(history)
        filters.pack(fill="x", pady=(0, 12))
        self.history_entry = ttk.Entry(filters, textvariable=self.history_search, width=22)
        self.history_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.history_entry.bind("<KeyRelease>", lambda event: self.render_account())
        self.filter_picker = ttk.Combobox(
            filters,
            textvariable=self.history_filter,
            state="readonly",
            width=15,
            values=("All activity", "Sent", "Received", "Pending"),
        )
        self.filter_picker.pack(side="left")
        self.filter_picker.bind("<<ComboboxSelected>>", lambda event: self.render_account())
        self._label(
            history,
            "Search by person, amount or reference · Select a payment for its receipt",
            "Muted.TLabel",
        )
        self.activity = self._table(history, ("Payment", "Date", "Amount", "Status"))
        self.activity.column("Payment", width=215)
        self.activity.column("Date", width=110)
        self.activity.column("Amount", width=105, anchor="e")
        self.activity.column("Status", width=100)
        self.activity.tag_configure("received", foreground=ACCENT)
        self.activity.bind("<<TreeviewSelect>>", lambda event: self.payment_details())
        self.empty_label = ttk.Label(history, text="No matching activity.", style="Muted.TLabel")
        self.payees = ttk.Frame(body, width=200)
        self.payees.pack(side="right", fill="y")
        self._label(self.payees, "People", "Title.TLabel")
        self._label(self.payees, "Choose someone to pay", "Muted.TLabel")
        self.people_list = ttk.Frame(self.payees)
        self.people_list.pack(fill="x")
        ttk.Label(shell, textvariable=self.status, wraplength=820, style="Muted.TLabel").pack(
            anchor="w", pady=(18, 10)
        )
        self._label(
            shell,
            "DEMO · 10 sample people · Test funds only · Payments confirm automatically"
            if self.session.demo
            else "Local account · Balance includes completed payments",
            "Muted.TLabel",
        )

    def open_tools(self) -> None:
        self.tools.deiconify()
        self.tools.lift()

    def _build_tools(self) -> None:
        self.tools = tk.Toplevel(self.root)
        self.tools.title("BlockchainCoin | Advanced tools")
        self.tools.geometry("830x720")
        self.tools.configure(background=BG)
        self.tools.protocol("WM_DELETE_WINDOW", self.tools.withdraw)
        self.tools.withdraw()
        frame = ttk.Frame(self.tools, padding=24)
        frame.pack(fill="both", expand=True)
        self._label(frame, "Advanced tools", "Title.TLabel")
        self._label(
            frame, "Optional diagnostics. You do not need these to make a payment.", "Muted.TLabel"
        )
        search = ttk.Frame(frame)
        search.pack(fill="x", pady=(0, 12))
        entry = ttk.Entry(search, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.bind("<Return>", lambda event: self.lookup())
        self.search_button = ttk.Button(search, text="Inspect", command=self.lookup)
        self.search_button.pack(side="left")
        self.mine_button = ttk.Button(
            frame,
            text="Generate a block",
            command=lambda: self.run("mine", sender=self.sender.get()),
        )
        self.mine_button.pack(anchor="w", pady=(0, 10))
        self.tables = {"Blocks": self._table(frame, ("Height", "Hash", "Transactions"), 3)}
        self.tables["Blocks"].bind("<<TreeviewSelect>>", lambda event: self.select("Blocks"))
        self.detail = ScrolledText(
            frame,
            height=10,
            background=PANEL,
            foreground=TEXT,
            font=("Menlo", 10),
            wrap="word",
            borderwidth=0,
            padx=12,
            pady=12,
            state="disabled",
        )
        self.detail.pack(fill="both", expand=True, pady=(12, 0))

    def account(self) -> dict[str, Any]:
        return next(
            (
                wallet
                for wallet in self.state.get("wallets", [])
                if wallet["name"] == self.sender.get()
            ),
            {},
        )

    def change_account(self) -> None:
        self.sender.set(self.account_keys[self.account_choice.get()])
        self.render_account()

    def render_account(self) -> None:
        account = self.account()
        self.balance_label.configure(text=f"{account.get('balance', 0):,} BCC")
        payments = [
            item for item in self.state.get("activity", []) if item["account"] == self.sender.get()
        ]
        completed = [
            item for item in payments if item["status"] == "Completed" and item["kind"] != "funding"
        ]
        incoming = sum(item["amount"] for item in completed if item["kind"] == "received")
        outgoing = -sum(item["amount"] for item in completed if item["kind"] == "sent")
        self.summary_labels["Money in"].configure(text=f"{incoming:,} BCC")
        self.summary_labels["Money out"].configure(text=f"{outgoing:,} BCC")
        self.summary_labels["Payments"].configure(text=str(len(completed)))
        pending = sum(item["status"] == "Pending" for item in payments)
        self.balance_note.configure(
            text="Open a wallet to get started"
            if not account
            else f"{pending} payment(s) pending · Balance shows completed payments"
            if pending
            else "Completed balance"
        )
        if pending or not account:
            self.balance_note.pack(anchor="w")
        else:
            self.balance_note.pack_forget()
        self.activity.delete(*self.activity.get_children())
        query = self.history_search.get().strip().lower()
        choice = self.history_filter.get()
        filtered = [
            item
            for item in payments
            if query in f"{item['title']} {item['amount']} {item['txid']}".lower()
            and (
                choice == "All activity"
                or choice == "Pending"
                and item["status"] == "Pending"
                or choice == "Sent"
                and item["kind"] == "sent"
                or choice == "Received"
                and item["kind"] == "received"
            )
        ]
        self.visible_activity = {item["txid"]: item for item in filtered}
        for identifier, item in self.visible_activity.items():
            date = (
                "Starting funds"
                if item["title"] == "Opening balance"
                else datetime.fromtimestamp(item["timestamp"]).strftime("%b %d, %H:%M")
                if item["timestamp"] is not None
                else "Just now"
            )
            self.activity.insert(
                "",
                "end",
                iid=identifier,
                values=(item["title"], date, f"{item['amount']:+,} BCC", item["status"]),
                tags=(item["kind"],),
            )
        if filtered:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(anchor="w", pady=8)
        for child in self.people_list.winfo_children():
            child.destroy()
        self.payee_buttons = []
        for wallet in self.state.get("wallets", []):
            if wallet["name"] != self.sender.get():
                label = f"{wallet['label']} · {wallet['address'][-6:]}"
                button = ttk.Button(
                    self.people_list,
                    text=label,
                    style="Person.TButton",
                    command=lambda address=wallet["address"]: self.pay_person(address),
                )
                button.pack(fill="x", pady=(0, 5))
                self.payee_buttons.append(button)
        self._controls(self.future is None)

    def _dialog(self, title: str) -> ttk.Frame:
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()
        self.dialog = tk.Toplevel(self.root)
        self.dialog.title(title)
        self.dialog.geometry("490x530")
        self.dialog.resizable(False, False)
        self.dialog.configure(background=BG)
        self.dialog.transient(self.root)
        frame = ttk.Frame(self.dialog, padding=30)
        frame.pack(fill="both", expand=True)
        self._label(frame, title, "Title.TLabel")
        return frame

    def pay_person(self, address: str) -> None:
        self.open_send()
        self.recipient.set(
            next(label for label, known in self.contacts.items() if known == address)
        )

    def open_send(self) -> None:
        self.payment = {}
        self.payment_from = dict(self.account())
        self.recipient.set("")
        self.amount.set("")
        self._send_form()

    def _send_form(self) -> None:
        frame = self._dialog("Send money")
        self._label(frame, f"From {self.payment_from['label']}", "Muted.TLabel")
        self._label(frame, "Who are you sending to?")
        recipients = [
            wallet
            for wallet in self.state["wallets"]
            if wallet["name"] != self.payment_from["name"]
        ]
        self.contacts = {
            (
                f"{wallet['label']} · {wallet['address'][-6:]}"
                if sum(other["label"] == wallet["label"] for other in recipients) > 1
                else wallet["label"]
            ): wallet["address"]
            for wallet in recipients
        }
        self.recipient_picker = ttk.Combobox(
            frame, textvariable=self.recipient, values=list(self.contacts)
        )
        self.recipient_picker.pack(fill="x", pady=(0, 8))
        self._label(frame, "Choose a person or paste a payment address.", "Muted.TLabel")
        self._label(frame, "Amount · BCC")
        ttk.Entry(frame, textvariable=self.amount).pack(fill="x", pady=(0, 14))
        self._label(frame, "Payment fee: 1 BCC", "Muted.TLabel")
        self.form_error = ttk.Label(frame, text="", foreground="#a23d32", wraplength=420)
        self.form_error.pack(anchor="w", pady=(0, 12))
        self.review_button = ttk.Button(
            frame, text="Review payment", style="Primary.TButton", command=self.send
        )
        self.review_button.pack(fill="x")

    def send(self) -> None:
        try:
            amount = int(self.amount.get())
            recipient = self.contacts.get(self.recipient.get(), self.recipient.get().strip())
            if amount <= 0:
                raise ValueError("Enter an amount greater than zero.")
            if not is_valid_address(recipient):
                raise ValueError("Choose a person or paste a valid payment address.")
            if amount + 1 > self.payment_from["balance"]:
                raise ValueError("Your balance does not cover this payment and its fee.")
        except ValueError as exc:
            self.form_error.configure(
                text=str(exc) if self.amount.get().isdecimal() else "Enter a whole BCC amount."
            )
            return
        self.payment = {
            "sender": self.payment_from["name"],
            "recipient": recipient,
            "amount": amount,
            "fee": 1,
            "confirm": self.session.demo,
        }
        self.payment_name = self.recipient.get()
        frame = self._dialog("Review payment")
        assert self.dialog is not None
        self.dialog.geometry("490x610")
        for label, value in (
            ("From", self.payment_from["label"]),
            ("To", self.payment_name),
            ("Amount", f"{amount:,} BCC"),
            ("Payment fee", "1 BCC"),
            ("Total from your account", f"{amount + 1:,} BCC"),
        ):
            self._label(frame, label, "Muted.TLabel")
            ttk.Label(frame, text=value, wraplength=420, font=("Helvetica", 16, "bold")).pack(
                anchor="w", pady=(0, 18)
            )
        self.confirm_button = ttk.Button(
            frame, text=f"Send {amount:,} BCC", style="Primary.TButton", command=self.confirm_send
        )
        self.confirm_button.pack(fill="x", pady=(0, 8))
        ttk.Button(frame, text="Back", style="Link.TButton", command=self._send_form).pack()

    def confirm_send(self) -> None:
        assert self.dialog is not None
        self.dialog.destroy()
        self.dialog = None
        self.run("send", **self.payment)

    def receive(self) -> None:
        frame = self._dialog("Receive money")
        self._label(frame, "Share this payment address with the sender.", "Muted.TLabel")
        address = self.account()["address"]
        field = ttk.Entry(frame, width=42)
        field.insert(0, address)
        field.configure(state="readonly")
        field.pack(fill="x", pady=(10, 20))
        self.copy_button = ttk.Button(
            frame,
            text="Copy payment address",
            style="Primary.TButton",
            command=lambda: self.copy_address(address),
        )
        self.copy_button.pack(fill="x")
        self._label(frame, "Received payments appear in your recent activity.", "Muted.TLabel")

    def copy_address(self, address: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(address)
        self.copy_button.configure(text="Address copied")

    def payment_details(self) -> None:
        selected = self.activity.selection()
        if not selected:
            return
        item = self.visible_activity[selected[0]]
        frame = self._dialog(item["title"])
        self._label(frame, f"{item['amount']:+,} BCC", "Title.TLabel")
        self._label(frame, item["status"], "Muted.TLabel")
        self._label(frame, "Amounts sent include the payment fee.", "Muted.TLabel")
        for label, value in (
            ("Account", self.account().get("label", "")),
            ("Payment fee", f"{item['fee']:,} BCC"),
            (
                "Date",
                datetime.fromtimestamp(item["timestamp"]).strftime("%b %d, %Y · %H:%M")
                if item["timestamp"] is not None
                else "Awaiting confirmation",
            ),
        ):
            self._label(frame, f"{label}: {value}")
        self._label(frame, "Payment reference", "Muted.TLabel")
        reference = ttk.Entry(frame)
        reference.insert(0, item["txid"])
        reference.configure(state="readonly")
        reference.pack(fill="x", pady=(0, 12))
        assert self.dialog is not None
        ttk.Button(frame, text="Done", command=self.dialog.destroy).pack(fill="x", pady=20)

    def run(self, action: str = "refresh", **values: Any) -> None:
        if self.future is not None:
            return
        self.last_action = action
        self.status.set("Updating your account...")
        self.future = self.executor.submit(self.session.execute, action, **values)
        self._controls(False)
        self.root.after(60, self.poll)

    def _controls(self, enabled: bool) -> None:
        for button in (
            self.search_button,
            self.refresh_button,
            self.load_button,
            self.tools_button,
        ):
            button.configure(state="normal" if enabled else "disabled")
        for button in (self.send_button, self.mine_button, self.receive_button):
            button.configure(state="normal" if enabled and self.account() else "disabled")
        self.wallet_picker.configure(state="readonly" if enabled else "disabled")
        for button in self.payee_buttons:
            button.configure(state="normal" if enabled else "disabled")

    def poll(self) -> None:
        assert self.future is not None
        if not self.future.done():
            self.root.after(60, self.poll)
            return
        try:
            self.state = self.future.result()
            self.render()
            self.status.set(
                "Payment sent. Your activity is up to date."
                if self.last_action == "send"
                else "Your account is up to date."
            )
        except Exception as exc:
            self.status.set(f"We could not finish that. {exc}")
        self.future = None
        self._controls(True)
        if self.closing:
            self.close()

    def render(self) -> None:
        self.account_keys = {
            f"{wallet['label']} · {wallet['address'][-6:]}": wallet["name"]
            for wallet in self.state["wallets"]
        }
        self.wallet_picker.configure(values=list(self.account_keys))
        if self.sender.get() not in self.account_keys.values():
            self.sender.set(next(iter(self.account_keys.values()), ""))
        choice = next(
            (label for label, key in self.account_keys.items() if key == self.sender.get()),
            "No account open",
        )
        self.account_choice.set(choice)
        self.render_account()
        table = self.tables["Blocks"]
        table.delete(*table.get_children())
        for block in self.state["blocks"]:
            table.insert(
                "",
                "end",
                iid=str(block["height"]),
                values=(block["height"], block["hash"][:18] + "…", block["transaction_count"]),
            )
        if self.state["detail"]:
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", json.dumps(self.state["detail"], indent=2))
            self.detail.configure(state="disabled")

    def lookup(self) -> None:
        query = self.query.get().strip()
        if query:
            self.run("lookup", query=query)

    def select(self, name: str) -> None:
        selected = self.tables[name].selection()
        if selected:
            self.query.set(selected[0])
            self.lookup()

    def load_wallet(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Open a wallet", filetypes=[("Wallet", "*.json")]
        )
        if path:
            passphrase = simpledialog.askstring(
                "Open wallet",
                "Wallet password (leave blank if it has none):",
                show="*",
                parent=self.root,
            )
            if passphrase is not None:
                self.run("load_wallet", path=path, passphrase=passphrase or None)

    def close(self) -> None:
        if self.future is not None:
            self.closing = True
            self.status.set("Finishing your payment before closing...")
            return
        self.executor.shutdown(wait=True)
        self.session.close()
        self.root.destroy()


def main() -> None:
    """Launch the native desktop application, using a disposable demo by default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="existing node database; defaults to isolated demo")
    parser.add_argument("--difficulty", type=int, default=0)
    parser.add_argument("--reward", type=int, default=50)
    args = parser.parse_args()
    root = tk.Tk()
    try:
        session = DesktopSession(args.db, args.difficulty, args.reward, populated=True)
    except (ValueError, OSError) as exc:
        messagebox.showerror("Cannot open the node", str(exc), parent=root)
        root.destroy()
        return
    DesktopApp(root, session)
    root.mainloop()


if __name__ == "__main__":
    main()
