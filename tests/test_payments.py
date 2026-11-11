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

from backend.app.main import app  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
