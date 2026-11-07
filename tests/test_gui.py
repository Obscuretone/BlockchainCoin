import json
import sys
import tempfile
import time
import tkinter as tk
import unittest
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import patch

from blockchaincoin.crypto import Wallet
from blockchaincoin.gui import DesktopApp, DesktopSession, main
from blockchaincoin.utxo_node import ConsensusNode, ConsensusNodeError


class DesktopSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = DesktopSession()
        self.addCleanup(self.session.close)

    def test_transfer_mining_lookup_and_cleanup(self) -> None:
        session = self.session
        initial = session.execute()
        self.assertEqual(initial["supply"], 1250)
        self.assertEqual(initial["wallets"][0]["balance"], 1000)
        bob = session.wallets["Bob"].address
        pending = session.execute("send", sender="Alice", recipient=bob, amount=12, fee=1)
        txid = pending["detail"]["txid"]
        self.assertEqual(pending["detail"]["status"], "mempool")
        self.assertEqual(len(pending["mempool"]), 1)
        self.assertEqual(session.execute("lookup", query=txid)["detail"]["status"], "mempool")
        mined = session.execute("mine", sender="Alice")
        self.assertEqual(mined["height"], 1)
        self.assertEqual(mined["supply"], 1300)
        self.assertEqual(mined["mempool"], [])
        self.assertEqual(session.execute("lookup", query=bob)["detail"]["balance"], 262)
        self.assertEqual(session.execute("lookup", query="1")["detail"]["hash"], mined["tip"])
        self.assertEqual(session.execute("lookup", query=mined["tip"])["detail"]["height"], 1)
        self.assertEqual(session.execute("lookup", query=txid)["detail"]["status"], "confirmed")
        self.assertNotIn("private_key", json.dumps(mined))
        path = session.db.parent
        session.close()
        self.assertFalse(path.exists())
        self.assertEqual(session.wallets, {})

    def test_ten_person_scenario_reconciles_and_survives_restart(self) -> None:
        session = DesktopSession(populated=True)
        self.addCleanup(session.close)
        state = session.execute()
        expected = dict(
            zip(session.wallets, (2530, 1860, 1060, 990, 990, 990, 990, 920, 920, 920), strict=True)
        )
        self.assertEqual(len(state["wallets"]), 10)
        self.assertEqual(state["height"], 30)
        self.assertEqual(state["supply"], 13700)
        self.assertEqual(state["mempool"], [])
        self.assertEqual({w["name"]: w["balance"] for w in state["wallets"]}, expected)
        payments = [item for item in state["activity"] if item["kind"] != "funding"]
        self.assertEqual(len(payments), 60)
        self.assertEqual(sum(item["amount"] for item in payments), -30)
        self.assertTrue(all(item["confirmations"] > 0 for item in payments))
        self.assertEqual(sum(item["fee"] for item in payments), 30)
        self.assertEqual(state["tip"], session.execute()["tip"])
        with self.assertRaises(ConsensusNodeError):
            session.execute(
                "send",
                sender="Alice",
                recipient=session.wallets["Bob"].address,
                amount=1_000_000,
                fee=1,
            )
        self.assertEqual(session.execute()["tip"], state["tip"])
        self.assertNotIn("private_key", json.dumps(state))

    def test_bad_input_and_unknown_records_leave_chain_intact(self) -> None:
        for amount, fee in ((True, 1), (1, False), (0, 1), (1, -1), (1.5, 1)):
            with self.subTest(amount=amount, fee=fee), self.assertRaises(ValueError):
                self.session.execute(
                    "send", sender="Alice", recipient="bad", amount=amount, fee=fee
                )
        for query in ("999", "bcc_bad", "f" * 64):
            with self.subTest(query=query), self.assertRaises(ConsensusNodeError):
                self.session.execute("lookup", query=query)
        with self.assertRaises(ConsensusNodeError):
            self.session.execute("send", sender="Alice", recipient="bad", amount=1, fee=1)
        with self.assertRaises(ConsensusNodeError):
            self.session.execute(
                "send",
                sender="Alice",
                recipient=self.session.wallets["Bob"].address,
                amount=2000,
                fee=1,
            )
        with self.assertRaises(ValueError):
            self.session.execute("unknown")
        self.assertEqual(self.session.execute()["height"], 0)

    def test_existing_chain_wallet_unlock_and_duplicate_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "node.sqlite3"
            key = Path(tmp) / "wallet.json"
            wallet = Wallet.create()
            wallet.save(key, passphrase="test-only")
            node = ConsensusNode.create(db, {wallet.address: 25}, difficulty=0, block_subsidy=7)
            node.close()
            session = DesktopSession(db, reward=7)
            try:
                self.assertFalse(session.demo)
                self.assertEqual(session.execute()["wallets"], [])
                with self.assertRaises(ValueError):
                    session.execute("load_wallet", path=key, passphrase="incorrect")
                loaded = session.execute("load_wallet", path=key, passphrase="test-only")
                self.assertEqual(loaded["wallets"][0]["balance"], 25)
                session.execute("load_wallet", path=key, passphrase="test-only")
                self.assertEqual(len(session.wallets), 1)
                pending = session.execute(
                    "send",
                    sender=wallet.address,
                    recipient=Wallet.create().address,
                    amount=1,
                    fee=1,
                    confirm=True,
                )
                self.assertEqual(pending["detail"]["status"], "mempool")
                self.assertEqual(pending["height"], 0)
                mined = session.execute("mine", sender=wallet.address)
                self.assertEqual(mined["supply"], 32)
            finally:
                session.close()
            self.assertTrue(db.exists())
            self.assertTrue(key.exists())
            with self.assertRaises(ValueError):
                DesktopSession(Path(tmp) / "missing.sqlite3")

    def test_reimport_demo_wallet_reuses_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wallet.json"
            self.session.wallets["Alice"].save(path)
            self.session.execute("load_wallet", path=path, passphrase=None)
            self.assertEqual(list(self.session.wallets), ["Alice", "Bob"])

    def test_demo_payment_to_self_and_external_account(self) -> None:
        myself = self.session.wallets["Alice"].address
        result = self.session.execute(
            "send", sender="Alice", recipient=myself, amount=10, fee=1, confirm=True
        )
        self.assertEqual(result["activity"][0]["title"], "Sent to yourself")
        self.assertEqual(result["activity"][0]["amount"], -1)
        result = self.session.execute(
            "send", sender="Alice", recipient=Wallet.create().address, amount=4, fee=1, confirm=True
        )
        self.assertEqual(result["activity"][0]["title"], "Sent to another account")
        self.assertEqual(result["wallets"][0]["balance"], 994)


class DesktopWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Native display unavailable: {exc}")
        self.root.withdraw()
        self.root_closed = False
        self.app = DesktopApp(self.root, DesktopSession())
        self.drain()
        self.addCleanup(self.finish)

    def drain(self) -> None:
        deadline = time.monotonic() + 5
        while self.app.future is not None:
            self.root.update()
            if time.monotonic() > deadline:
                self.fail("Desktop operation did not finish")
            time.sleep(0.005)
        self.root.update()

    def finish(self) -> None:
        if not self.root_closed:
            self.drain()
            self.app.close()

    def test_bank_payment_requires_review_and_confirms_demo_automatically(self) -> None:
        app = self.app
        self.assertEqual(app.balance_label.cget("text"), "1,000 BCC")
        self.assertEqual(app.sender.get(), "Alice")
        self.assertEqual(str(app.send_button.cget("state")), "normal")
        self.assertFalse(app.tools.winfo_viewable())
        app.send_button.invoke()
        app.recipient.set("Bob")
        app.amount.set("12")
        app.review_button.invoke()
        self.assertEqual(app.payment["amount"], 12)
        self.assertEqual(app.payment["fee"], 1)
        self.assertIsNone(app.future)
        self.assertEqual(app.state["wallets"][0]["balance"], 1000)
        app.confirm_button.invoke()
        self.drain()
        self.assertEqual(app.balance_label.cget("text"), "987 BCC")
        self.assertEqual(app.state["mempool"], [])
        self.assertEqual(app.state["height"], 1)
        payment = app.state["activity"][0]
        self.assertEqual(payment["title"], "Sent to Bob")
        self.assertEqual(payment["amount"], -13)
        self.assertEqual(payment["status"], "Completed")
        app.activity.selection_set(payment["txid"])
        self.root.update()
        self.assertIsNotNone(app.dialog)
        app.payment_details()
        choice = next(label for label, name in app.account_keys.items() if name == "Bob")
        app.account_choice.set(choice)
        app.wallet_picker.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(app.balance_label.cget("text"), "262 BCC")
        self.assertEqual(app.sender.get(), "Bob")
        app.open_send()
        assert app.dialog is not None
        app.dialog.destroy()
        app.receive()
        with (
            patch.object(app.root, "clipboard_clear") as clear,
            patch.object(app.root, "clipboard_append") as append,
        ):
            app.copy_button.invoke()
            clear.assert_called_once()
            append.assert_called_once_with(app.account()["address"])
        self.assertEqual(app.copy_button.cget("text"), "Address copied")

    def test_summaries_filters_search_and_payee_shortcut(self) -> None:
        app = self.app
        app.payee_buttons[0].invoke()
        self.assertEqual(app.recipient.get(), "Bob")
        app.amount.set("12")
        app.review_button.invoke()
        app.confirm_button.invoke()
        self.drain()
        self.assertEqual(app.summary_labels["Money out"].cget("text"), "13 BCC")
        self.assertEqual(app.summary_labels["Money in"].cget("text"), "0 BCC")
        self.assertEqual(app.summary_labels["Payments"].cget("text"), "1")
        for choice, count in (("Sent", 1), ("Received", 0), ("Pending", 0), ("All activity", 2)):
            app.history_filter.set(choice)
            app.filter_picker.event_generate("<<ComboboxSelected>>")
            self.root.update()
            self.assertEqual(len(app.visible_activity), count)
        app.history_search.set("Bob")
        app.history_entry.event_generate("<KeyRelease>")
        self.root.update()
        app.render_account()
        self.assertEqual(len(app.visible_activity), 1)
        txid = next(iter(app.visible_activity))
        app.activity.selection_set(txid)
        app.payment_details()
        app.history_search.set("does not exist")
        app.render_account()
        self.assertEqual(len(app.visible_activity), 0)
        app.history_search.set("")
        app.sender.set("Bob")
        app.render_account()
        self.assertEqual(app.summary_labels["Money in"].cget("text"), "12 BCC")
        app.run(
            "send", sender="Bob", recipient=app.session.wallets["Alice"].address, amount=2, fee=1
        )
        self.drain()
        app.history_filter.set("Pending")
        app.render_account()
        self.assertEqual(len(app.visible_activity), 1)
        app.activity.selection_set(next(iter(app.visible_activity)))
        app.payment_details()

    def test_payment_form_validation_and_back(self) -> None:
        app = self.app
        app.open_send()
        for amount, recipient, message in (
            ("1.2", "Bob", "whole"),
            ("0", "Bob", "greater"),
            ("2", "invalid", "valid"),
            ("2000", "Bob", "balance"),
        ):
            app.amount.set(amount)
            app.recipient.set(recipient)
            app.send()
            self.assertIn(message, app.form_error.cget("text"))
            self.assertIsNone(app.future)
        app.recipient.set(app.session.wallets["Bob"].address)
        app.amount.set("10")
        app.sender.set("Bob")
        app.send()
        self.assertEqual(app.payment["sender"], "Alice")
        self.assertEqual(app.payment["amount"], 10)
        app._send_form()
        self.assertEqual(app.amount.get(), "10")
        self.assertEqual(app.recipient.get(), app.session.wallets["Bob"].address)
        self.assertEqual(app.state["height"], 0)

    def test_same_named_recipients_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for folder in ("one", "two"):
                path = Path(tmp) / folder / "wallet.json"
                Wallet.create().save(path)
                self.app.run("load_wallet", path=path, passphrase=None)
                self.drain()
            self.app.open_send()
            self.assertEqual(len(self.app.contacts), 3)
            self.assertEqual(len(set(self.app.contacts.values())), 3)

    def test_optional_tools_and_pending_payments(self) -> None:
        app = self.app
        app.tools_button.invoke()
        app.query.set("0")
        app.search_button.invoke()
        self.drain()
        self.assertIn('"height": 0', app.detail.get("1.0", "end"))
        app.tables["Blocks"].selection_set("0")
        self.root.update()
        self.drain()
        self.assertEqual(app.query.get(), "0")
        app.query.set("")
        app.lookup()
        app.select("Blocks")
        self.drain()
        app.run(
            "send", sender="Alice", recipient=app.session.wallets["Bob"].address, amount=12, fee=1
        )
        self.drain()
        self.assertIn("pending", app.balance_note.cget("text"))
        app.mine_button.invoke()
        self.drain()
        self.assertEqual(app.state["height"], 1)
        app.run("lookup", query="does-not-exist")
        self.drain()
        self.assertIn("could not finish", app.status.get())

    def test_busy_state_and_deferred_close(self) -> None:
        pending: Future[dict[str, Any]] = Future()
        self.app.future = pending
        self.app.run()
        self.assertIs(self.app.future, pending)
        self.app.poll()
        self.app.close()
        self.assertTrue(self.app.closing)
        pending.set_result(self.app.state)
        self.app.poll()
        self.root_closed = True

    def test_wallet_dialog_cancel_import_and_empty_account(self) -> None:
        with patch("blockchaincoin.gui.filedialog.askopenfilename", return_value=""):
            self.app.load_wallet()
        with (
            patch("blockchaincoin.gui.filedialog.askopenfilename", return_value="/wallet.json"),
            patch("blockchaincoin.gui.simpledialog.askstring", return_value=None),
        ):
            self.app.load_wallet()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wallet.json"
            Wallet.create().save(path)
            with (
                patch("blockchaincoin.gui.filedialog.askopenfilename", return_value=str(path)),
                patch("blockchaincoin.gui.simpledialog.askstring", return_value=""),
            ):
                self.app.load_button.invoke()
                self.drain()
        self.assertEqual(len(self.app.state["wallets"]), 3)
        self.app.session.wallets.clear()
        self.app.run()
        self.drain()
        self.assertEqual(str(self.app.send_button.cget("state")), "disabled")
        self.assertEqual(self.app.account_choice.get(), "No account open")
        self.assertEqual(self.app.balance_label.cget("text"), "0 BCC")
        self.app.payment_details()

    def test_existing_database_presentation(self) -> None:
        self.app.close()
        self.root_closed = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.sqlite3"
            node = ConsensusNode.create(path, {Wallet.create().address: 10}, difficulty=0)
            node.close()
            self.root = tk.Tk()
            self.root.withdraw()
            self.app = DesktopApp(self.root, DesktopSession(path))
            self.drain()
            self.assertEqual(str(self.app.send_button.cget("state")), "disabled")
            self.app.close()


class DesktopEntrypointTests(unittest.TestCase):
    def test_launch_and_startup_failure(self) -> None:
        with (
            patch.object(sys, "argv", ["blockchaincoin-gui"]),
            patch("blockchaincoin.gui.tk.Tk") as root,
            patch("blockchaincoin.gui.DesktopApp") as app,
        ):
            main()
            root.return_value.mainloop.assert_called_once()
            session = app.call_args.args[1]
            session.close()
        with (
            patch.object(sys, "argv", ["blockchaincoin-gui", "--db", "/missing/node.sqlite3"]),
            patch("blockchaincoin.gui.tk.Tk") as root,
            patch("blockchaincoin.gui.messagebox.showerror") as error,
        ):
            main()
            error.assert_called_once()
            root.return_value.destroy.assert_called_once()
            root.return_value.mainloop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
