"""Ledger guarantees for the SecurePay simulation.

The database URL is set before the app is imported so tests never open data/securepay.db.
"""

import os
import shutil
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")

_ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="securepay-test-")
_DB = Path(_TMP) / "test.db"
os.environ["DATABASE_URL"] = "sqlite:///" + _DB.as_posix()
os.environ["JWT_SECRET"] = "test-secret-key-at-least-32-bytes"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.database import SessionLocal  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import ApiKey  # noqa: E402
from backend.app.security import hash_api_key  # noqa: E402


class PaymentLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()
        health = cls.client.get("/api/health")
        assert health.status_code == 200, health.text
        cls.token = cls._login("merchant@securepay.test", "merchant123")
        balance, _account = cls._balance(cls.token)
        assert balance == 1_000_000, balance

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client_cm.__exit__(None, None, None)
        shutil.rmtree(_TMP, ignore_errors=True)

    @classmethod
    def _login(cls, email: str, password: str) -> str:
        response = cls.client.post(
            "/api/auth/login",
            json={"email": email, "password": password},
        )
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    @classmethod
    def _headers(cls, token: str, **extra: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        headers.update(extra)
        return headers

    @classmethod
    def _balance(cls, token: str) -> tuple[int, dict]:
        response = cls.client.get("/api/ledger", headers=cls._headers(token))
        assert response.status_code == 200, response.text
        accounts = response.json()["accounts"]
        assert accounts, response.text
        return accounts[0]["balance_cents"], accounts[0]

    def test_01_idempotent_replay_does_not_double_credit(self) -> None:
        token = self.token
        amount = 4200
        key = "idem-replay-4200-key"
        body = {
            "amount_cents": amount,
            "currency": "USD",
            "customer_ref": "cust_idem_1",
            "payment_token": "tok_test_visa_4242",
        }
        start, _account = self._balance(token)

        created = self.client.post(
            "/api/payments",
            headers=self._headers(token, **{"Idempotency-Key": key}),
            json=body,
        )
        self.assertEqual(created.status_code, 201, created.text)
        payment = created.json()
        self.assertEqual(payment["status"], "authorized")
        self.assertEqual(payment["risk_decision"], "allow")
        after_create, _account = self._balance(token)
        self.assertEqual(after_create, start)

        captured = self.client.post(
            f"/api/payments/{payment['id']}/capture",
            headers=self._headers(token),
        )
        self.assertEqual(captured.status_code, 200, captured.text)
        self.assertEqual(captured.json()["status"], "captured")
        after_capture, account = self._balance(token)
        self.assertEqual(after_capture, start + amount)
        credits = [
            entry
            for entry in account["entries"]
            if entry["payment_id"] == payment["id"] and entry["direction"] == "credit"
        ]
        self.assertEqual(len(credits), 1)
        self.assertEqual(credits[0]["amount_cents"], amount)

        replay = self.client.post(
            "/api/payments",
            headers=self._headers(token, **{"Idempotency-Key": key}),
            json=body,
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.headers.get("X-Idempotent-Replay"), "true")
        self.assertEqual(replay.json()["id"], payment["id"])
        after_replay, _account = self._balance(token)
        self.assertEqual(after_replay, start + amount)

        captured_again = self.client.post(
            f"/api/payments/{payment['id']}/capture",
            headers=self._headers(token),
        )
        self.assertEqual(captured_again.status_code, 200, captured_again.text)
        self.assertEqual(captured_again.json()["status"], "captured")
        after_second_capture, account = self._balance(token)
        self.assertEqual(after_second_capture, start + amount)
        credits = [
            entry
            for entry in account["entries"]
            if entry["payment_id"] == payment["id"] and entry["direction"] == "credit"
        ]
        self.assertEqual(len(credits), 1)

        listed = self.client.get("/api/payments", headers=self._headers(token))
        self.assertEqual(listed.status_code, 200, listed.text)
        matches = [row for row in listed.json() if row["idempotency_key"] == key]
        self.assertEqual(len(matches), 1)

    def test_02_blocked_high_amount_does_not_change_balance(self) -> None:
        token = self.token
        amount = 500_001
        key = "block-500001-key"
        start, _account = self._balance(token)

        created = self.client.post(
            "/api/payments",
            headers=self._headers(token, **{"Idempotency-Key": key}),
            json={
                "amount_cents": amount,
                "currency": "USD",
                "customer_ref": "cust_block_1",
                "payment_token": "tok_test_visa_4242",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        payment = created.json()
        self.assertEqual(payment["status"], "failed")
        self.assertEqual(payment["risk_decision"], "block")
        self.assertIn("amount_exceeds_500000_cents", payment["risk_reasons"])
        after_create, account = self._balance(token)
        self.assertEqual(after_create, start)
        self.assertFalse(
            [entry for entry in account["entries"] if entry["payment_id"] == payment["id"]]
        )

        captured = self.client.post(
            f"/api/payments/{payment['id']}/capture",
            headers=self._headers(token),
        )
        self.assertEqual(captured.status_code, 409, captured.text)
        after_capture, account = self._balance(token)
        self.assertEqual(after_capture, start)
        self.assertFalse(
            [entry for entry in account["entries"] if entry["payment_id"] == payment["id"]]
        )

    def test_03_refund_restores_balance(self) -> None:
        token = self.token
        amount = 8800
        key = "refund-8800-key"
        start, _account = self._balance(token)

        created = self.client.post(
            "/api/payments",
            headers=self._headers(token, **{"Idempotency-Key": key}),
            json={
                "amount_cents": amount,
                "currency": "USD",
                "customer_ref": "cust_refund_1",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        payment_id = created.json()["id"]

        captured = self.client.post(
            f"/api/payments/{payment_id}/capture",
            headers=self._headers(token),
        )
        self.assertEqual(captured.status_code, 200, captured.text)
        after_capture, _account = self._balance(token)
        self.assertEqual(after_capture, start + amount)

        too_much = self.client.post(
            f"/api/payments/{payment_id}/refund",
            headers=self._headers(token),
            json={"amount_cents": amount + 1},
        )
        self.assertEqual(too_much.status_code, 409, too_much.text)
        after_reject, _account = self._balance(token)
        self.assertEqual(after_reject, start + amount)

        refunded = self.client.post(
            f"/api/payments/{payment_id}/refund",
            headers=self._headers(token),
            json={},
        )
        self.assertEqual(refunded.status_code, 200, refunded.text)
        self.assertEqual(refunded.json()["status"], "refunded")
        self.assertEqual(refunded.json()["refunded_cents"], amount)
        after_refund, account = self._balance(token)
        self.assertEqual(after_refund, start)

        debits = [
            entry
            for entry in account["entries"]
            if entry["payment_id"] == payment_id and entry["direction"] == "debit"
        ]
        self.assertEqual(len(debits), 1)
        self.assertEqual(debits[0]["amount_cents"], amount)

        again = self.client.post(
            f"/api/payments/{payment_id}/refund",
            headers=self._headers(token),
            json={},
        )
        self.assertEqual(again.status_code, 409, again.text)
        after_second, _account = self._balance(token)
        self.assertEqual(after_second, start)

    def _merchant_id(self, token: str) -> int:
        response = self.client.get("/api/overview", headers=self._headers(token))
        self.assertEqual(response.status_code, 200, response.text)
        merchants = response.json()["merchants"]
        self.assertTrue(merchants, response.text)
        return int(merchants[0]["id"])

    def _settlement_balance(self, token: str) -> int:
        response = self.client.get("/api/payouts", headers=self._headers(token))
        self.assertEqual(response.status_code, 200, response.text)
        return sum(account["balance_cents"] for account in response.json()["settlement_accounts"])

    def test_04_dispute_lost_refunds_through_ledger(self) -> None:
        merchant = self.token
        admin = self._login("admin@securepay.test", "admin123")
        amount = 6400
        dispute_amount = 2500
        start, _account = self._balance(merchant)

        created = self.client.post(
            "/api/payments",
            headers=self._headers(merchant, **{"Idempotency-Key": "dispute-lost-6400"}),
            json={
                "amount_cents": amount,
                "currency": "USD",
                "customer_ref": "cust_dispute_1",
                "payment_token": "tok_test_visa_4242",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        payment_id = created.json()["id"]

        held = self.client.post(
            "/api/payments",
            headers=self._headers(merchant, **{"Idempotency-Key": "dispute-held-1500"}),
            json={
                "amount_cents": 1500,
                "currency": "USD",
                "customer_ref": "cust_dispute_hold",
            },
        )
        self.assertEqual(held.status_code, 201, held.text)
        held_id = held.json()["id"]
        self.assertNotEqual(held.json()["status"], "captured")

        too_soon = self.client.post(
            f"/api/payments/{held_id}/disputes",
            headers=self._headers(merchant),
            json={"reason": "Not captured yet", "amount_cents": 1500},
        )
        self.assertEqual(too_soon.status_code, 409, too_soon.text)

        captured = self.client.post(
            f"/api/payments/{payment_id}/capture",
            headers=self._headers(merchant),
        )
        self.assertEqual(captured.status_code, 200, captured.text)
        after_capture, _account = self._balance(merchant)
        self.assertEqual(after_capture, start + amount)

        above = self.client.post(
            f"/api/payments/{payment_id}/disputes",
            headers=self._headers(merchant),
            json={"reason": "Too much", "amount_cents": amount + 1},
        )
        self.assertEqual(above.status_code, 409, above.text)
        after_reject, _account = self._balance(merchant)
        self.assertEqual(after_reject, after_capture)

        opened = self.client.post(
            f"/api/payments/{payment_id}/disputes",
            headers=self._headers(merchant),
            json={"reason": "Goods not received", "amount_cents": dispute_amount},
        )
        self.assertEqual(opened.status_code, 201, opened.text)
        dispute = opened.json()
        self.assertEqual(dispute["status"], "open")
        self.assertEqual(dispute["amount_cents"], dispute_amount)
        after_open, _account = self._balance(merchant)
        self.assertEqual(after_open, after_capture)

        merchant_lost = self.client.post(
            f"/api/disputes/{dispute['id']}/resolve",
            headers=self._headers(merchant),
            json={"outcome": "lost"},
        )
        self.assertEqual(merchant_lost.status_code, 403, merchant_lost.text)
        after_forbidden, _account = self._balance(merchant)
        self.assertEqual(after_forbidden, after_capture)

        lost = self.client.post(
            f"/api/disputes/{dispute['id']}/resolve",
            headers=self._headers(admin),
            json={"outcome": "lost"},
        )
        self.assertEqual(lost.status_code, 200, lost.text)
        self.assertEqual(lost.json()["status"], "lost")
        after_loss, account = self._balance(merchant)
        self.assertEqual(after_loss, after_capture - dispute_amount)

        listed = self.client.get("/api/payments", headers=self._headers(merchant))
        self.assertEqual(listed.status_code, 200, listed.text)
        payment = next(row for row in listed.json() if row["id"] == payment_id)
        self.assertEqual(payment["status"], "captured")
        self.assertEqual(payment["refunded_cents"], dispute_amount)
        debits = [
            entry
            for entry in account["entries"]
            if entry["payment_id"] == payment_id and entry["direction"] == "debit"
        ]
        self.assertEqual(len(debits), 1)
        self.assertEqual(debits[0]["amount_cents"], dispute_amount)

        audit = self.client.get("/api/audit", headers=self._headers(admin))
        self.assertEqual(audit.status_code, 200, audit.text)
        loss_rows = [
            row
            for row in audit.json()
            if row["action"] == "dispute.lost" and row["entity"] == f"dispute:{dispute['id']}"
        ]
        self.assertEqual(len(loss_rows), 1)

        outbox = self.client.get("/api/outbox", headers=self._headers(merchant))
        self.assertEqual(outbox.status_code, 200, outbox.text)
        pending = [
            row
            for row in outbox.json()
            if row["event_type"] == "dispute.lost" and row["payload"].get("dispute_id") == dispute["id"]
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["payload"]["amount_cents"], dispute_amount)

        again = self.client.post(
            f"/api/disputes/{dispute['id']}/resolve",
            headers=self._headers(admin),
            json={"outcome": "lost"},
        )
        self.assertEqual(again.status_code, 409, again.text)
        after_second, _account = self._balance(merchant)
        self.assertEqual(after_second, after_loss)

        won_open = self.client.post(
            f"/api/payments/{payment_id}/disputes",
            headers=self._headers(admin),
            json={"reason": "Customer kept the goods", "amount_cents": 1000},
        )
        self.assertEqual(won_open.status_code, 201, won_open.text)
        won = self.client.post(
            f"/api/disputes/{won_open.json()['id']}/resolve",
            headers=self._headers(admin),
            json={"outcome": "won"},
        )
        self.assertEqual(won.status_code, 200, won.text)
        self.assertEqual(won.json()["status"], "won")
        after_won, account = self._balance(merchant)
        self.assertEqual(after_won, after_loss)
        debits = [
            entry
            for entry in account["entries"]
            if entry["payment_id"] == payment_id and entry["direction"] == "debit"
        ]
        self.assertEqual(len(debits), 1)

        delivered = self.client.post("/api/outbox/deliver", headers=self._headers(admin))
        self.assertEqual(delivered.status_code, 200, delivered.text)
        self.assertGreaterEqual(delivered.json()["delivered"], 1)
        self.assertIn("No network call", delivered.json()["note"])
        outbox = self.client.get("/api/outbox", headers=self._headers(admin))
        sent = [
            row
            for row in outbox.json()
            if row["event_type"] == "dispute.lost" and row["payload"].get("dispute_id") == dispute["id"]
        ]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], "sent")

    def test_05_payout_is_idempotent_and_cannot_go_negative(self) -> None:
        merchant = self.token
        admin = self._login("admin@securepay.test", "admin123")
        merchant_id = self._merchant_id(merchant)
        start, _account = self._balance(merchant)
        settlement_before = self._settlement_balance(admin)
        amount = 3000
        key = "payout-idem-3000"

        forbidden = self.client.post(
            "/api/payouts",
            headers=self._headers(merchant, **{"Idempotency-Key": "payout-merchant-nope"}),
            json={"merchant_id": merchant_id, "amount_cents": amount, "currency": "USD"},
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        after_forbidden, _account = self._balance(merchant)
        self.assertEqual(after_forbidden, start)
        self.assertEqual(self._settlement_balance(admin), settlement_before)

        oversize = self.client.post(
            "/api/payouts",
            headers=self._headers(admin, **{"Idempotency-Key": "payout-oversize-before"}),
            json={"merchant_id": merchant_id, "amount_cents": start + 1, "currency": "USD"},
        )
        self.assertEqual(oversize.status_code, 409, oversize.text)
        after_oversize, _account = self._balance(merchant)
        self.assertEqual(after_oversize, start)
        self.assertEqual(self._settlement_balance(admin), settlement_before)

        first = self.client.post(
            "/api/payouts",
            headers=self._headers(admin, **{"Idempotency-Key": key}),
            json={"merchant_id": merchant_id, "amount_cents": amount, "currency": "USD"},
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertNotEqual(first.headers.get("X-Idempotent-Replay"), "true")
        payout = first.json()
        self.assertEqual(payout["status"], "paid")
        self.assertTrue(payout["simulation"])
        self.assertEqual(payout["amount_cents"], amount)
        self.assertEqual(payout["available_balance_cents"], start - amount)
        self.assertEqual(payout["settlement_balance_cents"], settlement_before + amount)
        after_first, account = self._balance(merchant)
        self.assertEqual(after_first, start - amount)
        self.assertGreaterEqual(after_first, 0)
        self.assertEqual(self._settlement_balance(admin), settlement_before + amount)
        payout_debits = [
            entry
            for entry in account["entries"]
            if entry.get("source") == "payout" and entry.get("payout_id") == payout["id"]
        ]
        self.assertEqual(len(payout_debits), 1)
        self.assertEqual(payout_debits[0]["direction"], "debit")
        self.assertEqual(payout_debits[0]["amount_cents"], amount)

        replay = self.client.post(
            "/api/payouts",
            headers=self._headers(admin, **{"Idempotency-Key": key}),
            json={"merchant_id": merchant_id, "amount_cents": amount, "currency": "USD"},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.headers.get("X-Idempotent-Replay"), "true")
        self.assertEqual(replay.json()["id"], payout["id"])
        self.assertEqual(replay.json()["status"], "paid")
        after_replay, account = self._balance(merchant)
        self.assertEqual(after_replay, start - amount)
        self.assertEqual(self._settlement_balance(admin), settlement_before + amount)
        payout_debits = [
            entry
            for entry in account["entries"]
            if entry.get("source") == "payout" and entry.get("payout_id") == payout["id"]
        ]
        self.assertEqual(len(payout_debits), 1)

        changed = self.client.post(
            "/api/payouts",
            headers=self._headers(admin, **{"Idempotency-Key": key}),
            json={"merchant_id": merchant_id, "amount_cents": amount + 1, "currency": "USD"},
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        after_changed, _account = self._balance(merchant)
        self.assertEqual(after_changed, start - amount)
        self.assertEqual(self._settlement_balance(admin), settlement_before + amount)

        still_too_much = self.client.post(
            "/api/payouts",
            headers=self._headers(admin, **{"Idempotency-Key": "payout-oversize-after"}),
            json={
                "merchant_id": merchant_id,
                "amount_cents": after_changed + 1,
                "currency": "USD",
            },
        )
        self.assertEqual(still_too_much.status_code, 409, still_too_much.text)
        after_guard, _account = self._balance(merchant)
        self.assertEqual(after_guard, start - amount)
        self.assertGreaterEqual(after_guard, 0)
        self.assertEqual(self._settlement_balance(admin), settlement_before + amount)

        listed = self.client.get("/api/payouts", headers=self._headers(admin))
        matches = [row for row in listed.json()["payouts"] if row["idempotency_key"] == key]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "paid")

    def test_06_api_key_and_capture_outbox(self) -> None:
        merchant = self.token
        created = self.client.post(
            "/api/api-keys",
            headers=self._headers(merchant),
            json={"name": "Desk test key"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        secret = created.json()["secret"]
        self.assertTrue(secret.startswith("sp_test_"))
        self.assertTrue(len(secret) > len(created.json()["prefix"]))

        listed = self.client.get("/api/api-keys", headers=self._headers(merchant))
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn(secret, listed.text)
        self.assertNotIn("secret", listed.json()[0])

        with SessionLocal() as db:
            row = db.get(ApiKey, created.json()["id"])
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.key_hash, hash_api_key(secret))
            self.assertNotEqual(row.key_hash, secret)
            self.assertNotIn(secret, row.key_hash)
            self.assertNotIn(secret, row.prefix)
            self.assertNotIn(secret, row.name)
            self.assertTrue(secret.startswith(row.prefix))

        body = {
            "amount_cents": 2100,
            "currency": "USD",
            "customer_ref": "cust_apikey_1",
            "payment_token": "tok_test_visa_4242",
        }
        via_key = self.client.post(
            "/api/payments",
            headers={"Authorization": f"Bearer {secret}", "Idempotency-Key": "apikey-create-2100"},
            json=body,
        )
        self.assertEqual(via_key.status_code, 201, via_key.text)
        payment_id = via_key.json()["id"]

        capture_with_key = self.client.post(
            f"/api/payments/{payment_id}/capture",
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(capture_with_key.status_code, 401, capture_with_key.text)

        self.client.post(
            f"/api/api-keys/{created.json()['id']}/revoke",
            headers=self._headers(merchant),
        )
        revoked = self.client.post(
            "/api/payments",
            headers={"Authorization": f"Bearer {secret}", "Idempotency-Key": "apikey-revoked-2101"},
            json={**body, "amount_cents": 2101, "customer_ref": "cust_apikey_2"},
        )
        self.assertEqual(revoked.status_code, 401, revoked.text)

        captured = self.client.post(
            f"/api/payments/{payment_id}/capture",
            headers=self._headers(merchant),
        )
        self.assertEqual(captured.status_code, 200, captured.text)
        outbox = self.client.get("/api/outbox", headers=self._headers(merchant))
        matches = [
            row
            for row in outbox.json()
            if row["event_type"] == "payment.captured" and row["payload"].get("payment_id") == payment_id
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "pending")

        captured_again = self.client.post(
            f"/api/payments/{payment_id}/capture",
            headers=self._headers(merchant),
        )
        self.assertEqual(captured_again.status_code, 200, captured_again.text)
        outbox = self.client.get("/api/outbox", headers=self._headers(merchant))
        matches = [
            row
            for row in outbox.json()
            if row["event_type"] == "payment.captured" and row["payload"].get("payment_id") == payment_id
        ]
        self.assertEqual(len(matches), 1)

        delivered = self.client.post("/api/outbox/deliver", headers=self._headers(merchant))
        self.assertEqual(delivered.status_code, 200, delivered.text)
        self.assertGreaterEqual(delivered.json()["delivered"], 1)
        outbox = self.client.get("/api/outbox", headers=self._headers(merchant))
        matches = [
            row
            for row in outbox.json()
            if row["event_type"] == "payment.captured" and row["payload"].get("payment_id") == payment_id
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
