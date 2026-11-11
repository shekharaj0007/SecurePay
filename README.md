# SecurePay

**This is a simulation.** SecurePay does not process real cards, store primary account numbers, call a card network, or move real money. Payment methods are fake tokens such as `tok_test_visa_4242`. Balances live in a local SQLite file.

The console is a merchant desk for payment intent, risk, and an available-balance ledger.

## What it demonstrates

- JWT sign-in and roles (`admin`, `merchant`). Merchants see only their payments. Admins see every payment and the audit log.
- Idempotency keys. `POST /api/payments` requires `Idempotency-Key`. The same key returns the original payment and does not post the ledger again.
- ACID-style updates. Capture and refund change the payment status and the ledger balance in one SQLAlchemy transaction.
- Risk before capture. Amounts above 500000 cents are blocked and do not move the ledger. Amounts above 100000 cents, or 3 captures for the same customer in the last hour, are held for review.
- A token bucket on `POST /api/payments`: 30 requests per minute per user. Over the limit the API returns HTTP 429.
- An audit log of sign-ins, creates, blocks, captures, refunds, disputes, payouts, API keys, and outbox delivery.
- Disputes on captured payments. A merchant or an admin opens one. An admin marks it won or lost. Lost refunds that amount through the same ledger debit as a refund, inside one database transaction.
- Simulated settlement. An admin payout moves available balance into a settlement account and stores the entry as `paid`. The same `Idempotency-Key` does not move funds again. The payout is refused if it would make the available balance negative.
- Merchant API keys. The secret is returned once. The database stores only its SHA-256 hash. `POST /api/payments` accepts `Authorization: Bearer <api key>` instead of the user JWT. Revoking the key stops that.
- A webhook outbox. `payment.captured` and `dispute.lost` insert a pending row. `POST /api/outbox/deliver` marks pending rows sent. Nothing is sent over the network.

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

Capture it, then open a dispute and have the admin mark it lost. The lost amount is refunded from the available balance:

```powershell
$admin = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/auth/login -ContentType "application/json" -Body '{"email":"admin@securepay.test","password":"admin123"}'
$adminHeaders = @{ Authorization = "Bearer $($admin.access_token)" }
# Use the payment id returned above after it has been captured.
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/payments/1/disputes -Headers $headers -ContentType "application/json" -Body '{"reason":"Goods not received","amount_cents":500}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/disputes/1/resolve -Headers $adminHeaders -ContentType "application/json" -Body '{"outcome":"lost"}'
```

An admin payout is also idempotent. Replace the merchant id and the amount with values from `GET /api/overview`. The amount cannot exceed the available balance.

```powershell
$payHeaders = @{ Authorization = "Bearer $($admin.access_token)"; "Idempotency-Key" = "payout-demo-0001" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/payouts -Headers $payHeaders -ContentType "application/json" -Body '{"merchant_id":1,"amount_cents":2500,"currency":"USD"}'
```

## Tests

```powershell
cd "C:\Users\raj shekhar\Downloads\SecurePay"
.\.venv\Scripts\python.exe -m unittest tests.test_payments -v
```

The suite uses a temporary SQLite file, not `data\securepay.db`. It checks that:

1. Replaying an idempotency key, including a second capture, does not double-credit.
2. A blocked amount above 500000 cents does not change the balance.
3. A refund restores the balance, and a refund above the captured amount is refused.
4. A lost dispute refunds its amount through the ledger, writes an audit row, and leaves a `dispute.lost` outbox row. Won does not move the balance. An amount above the capture is refused.
5. A payout with the same idempotency key does not move the available balance twice, and a payout larger than the available balance is refused.
6. An API key creates a payment, only its hash is stored, a revoked key is rejected, and delivering the outbox marks `payment.captured` sent without a network call.

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
| POST | `/api/payments/{id}/disputes` | signed in, captured payments only |
| GET | `/api/disputes` | signed in, scoped by role |
| POST | `/api/disputes/{id}/resolve` | admin, body `{"outcome":"won"}` or `{"outcome":"lost"}` |
| POST | `/api/payouts` | admin, `Idempotency-Key` required |
| GET | `/api/payouts` | signed in, scoped by role |
| POST | `/api/api-keys` | signed in. Response includes `secret` once |
| GET | `/api/api-keys` | signed in, scoped by role. No secret |
| POST | `/api/api-keys/{id}/revoke` | signed in, own key or admin |
| GET | `/api/outbox` | signed in, scoped by role |
| POST | `/api/outbox/deliver` | signed in. Marks pending rows sent. No outbound HTTP |

Risk reason codes stored on the payment: `amount_exceeds_500000_cents`, `amount_exceeds_100000_cents`, `customer_capture_velocity_3_in_1h`.

## Limits

This is a single-process SQLite demo. The rate limiter is in memory and resets when the process stops. Payouts update a simulated settlement account only. There is no bank file, no 3-D Secure, and no production secret management. API key secrets cannot be recovered after the create response. The outbox never calls a webhook URL. Do not point it at real card data.

New tables are created on the next startup. Delete `data\securepay.db` if an older file is missing them and you want a clean book.
