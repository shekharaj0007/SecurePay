# SecurePay

**This is a simulation.** SecurePay does not process real cards, store primary account numbers, call a card network, or move real money. Payment methods are fake tokens such as `tok_test_visa_4242`. Balances live in a local SQLite file.

The console is a merchant desk for payment intent, risk, and an available-balance ledger.

## What it demonstrates

- JWT sign-in and roles (`admin`, `merchant`). Merchants see only their payments. Admins see every payment and the audit log.
- Idempotency keys. `POST /api/payments` requires `Idempotency-Key`. The same key returns the original payment and does not post the ledger again.
- ACID-style updates. Capture and refund change the payment status and the ledger balance in one SQLAlchemy transaction.
- Risk before capture. Amounts above 500000 cents are blocked and do not move the ledger. Amounts above 100000 cents, or 3 captures for the same customer in the last hour, are held for review.
- A token bucket on `POST /api/payments`: 30 requests per minute per user. Over the limit the API returns HTTP 429.
- An audit log of sign-ins, creates, blocks, captures, and refunds.

Capture credits the merchant available balance once. Refund debits it back and cannot exceed the captured amount. Replaying capture on an already captured payment does not credit again.

## Demo accounts

| Email | Password | Role |
| --- | --- | --- |
| `admin@securepay.test` | `admin123` | admin |
| `merchant@securepay.test` | `merchant123` | merchant |

The merchant is **Lumen Supply Co.**, with an opening available balance of `1_000_000` cents (`$10,000.00`) in USD.

## Run on Windows (PowerShell)

From the repo root. Python 3.13. Docker is not required.

```powershell
cd "C:\Users\raj shekhar\Downloads\SecurePay"
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```powershell
cd "C:\Users\raj shekhar\Downloads\SecurePay\frontend"
npm install
npm run dev
```

Open [http://localhost:5174](http://localhost:5174). The Vite dev server proxies `/api` to `http://127.0.0.1:8000`. API docs are at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

If port 8000 is already taken, start uvicorn on another port and point Vite at it:

```powershell
$env:SECUREPAY_API = "http://127.0.0.1:8011"
npm run dev
```

`DATABASE_URL` is optional. When it is unset, the API uses `sqlite:///data/securepay.db`. Set `JWT_SECRET` if you do not want the local development default.

Delete `data\securepay.db` (and any `securepay.db-wal` / `securepay.db-shm` files) to reset the book. The next startup seeds the demo accounts again.

### Try it from PowerShell

```powershell
$login = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/auth/login -ContentType "application/json" -Body '{"email":"merchant@securepay.test","password":"merchant123"}'
$headers = @{ Authorization = "Bearer $($login.access_token)"; "Idempotency-Key" = "demo-key-001" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/payments -Headers $headers -ContentType "application/json" -Body '{"amount_cents":2500,"currency":"USD","customer_ref":"cust_demo","payment_token":"tok_test_visa_4242"}'
```

Sending that second command again with the same `Idempotency-Key` returns the original payment.

## Tests

```powershell
cd "C:\Users\raj shekhar\Downloads\SecurePay"
.\.venv\Scripts\python.exe -m unittest tests.test_payments -v
```

The suite uses a temporary SQLite file, not `data\securepay.db`. It checks that:

1. Replaying an idempotency key, including a second capture, does not double-credit.
2. A blocked amount above 500000 cents does not change the balance.
3. A refund restores the balance, and a refund above the captured amount is refused.

## API

| Method | Path | Who |
| --- | --- | --- |
| POST | `/api/auth/login` | public |
| GET | `/api/health` | public |
| GET | `/api/me` | signed in |
| POST | `/api/payments` | signed in, `Idempotency-Key` required |
| POST | `/api/payments/{id}/capture` | signed in |
| POST | `/api/payments/{id}/refund` | signed in |
| GET | `/api/payments` | signed in, scoped by role |
| GET | `/api/ledger` | signed in, scoped by role |
| GET | `/api/overview` | signed in, scoped by role |
| GET | `/api/audit` | admin |

Risk reason codes stored on the payment: `amount_exceeds_500000_cents`, `amount_exceeds_100000_cents`, `customer_capture_velocity_3_in_1h`.

## Limits

This is a single-process SQLite demo. The rate limiter is in memory and resets when the process stops. There is no bank payout, no 3-D Secure, and no production secret management. Do not point it at real card data.
