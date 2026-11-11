import { FormEvent, useEffect, useState } from "react"
import {
  ApiError,
  apiKeys,
  audit,
  capturePayment,
  createPayment,
  disputes,
  getToken,
  ledger,
  login,
  me,
  outbox,
  overview,
  payments,
  payouts,
  refundPayment,
  setToken,
} from "./api"
import { DisputesView, KeysView, OutboxView, SettlementView } from "./features"
import { dollarsToCents, formatMoney, formatWhen, newIdempotencyKey, reasonText } from "./format"
import type { ApiKeyRow, AuditRow, Dispute, LedgerAccount, Me, OutboxEvent, Overview, Payment, PayoutList, Role } from "./types"

type View = "overview" | "payments" | "ledger" | "disputes" | "settlement" | "keys" | "outbox" | "audit"
type Banner = { tone: "ok" | "err"; text: string }

function SimulationBar() {
  return (
    <div className="simbar" role="note">
      Simulation only. SecurePay does not process real cards, store card numbers, call a card network, or move real money.
      Use a fake token such as tok_test_visa_4242.
    </div>
  )
}

function Login({ onSuccess }: { onSuccess: (person: Me) => void }) {
  const [email, setEmail] = useState("merchant@securepay.test")
  const [password, setPassword] = useState("merchant123")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    setError("")
    try {
      const session = await login(email.trim(), password)
      setToken(session.access_token)
      onSuccess(await me())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed")
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="app">
      <SimulationBar />
      <div className="login">
        <section className="login-story">
          <div>
            <div className="brand light">
              <span className="seal">SP</span>
              <span>
                <span className="wordmark">SecurePay</span>
                <span className="brand-sub">Merchant risk console</span>
              </span>
            </div>
            <h1>A ledger that does not forget a replay.</h1>
            <p className="lede">
              Create a payment intent, let the risk rules speak, then capture once. The same idempotency key returns
              the original payment and does not credit the balance again. A lost dispute refunds the ledger. A payout
              parks available balance in a simulated settlement account.
            </p>
          </div>
          <p className="fine">Fake tokens only. Nothing here settles at a bank.</p>
        </section>
        <section className="login-panel">
          <form className="login-card" onSubmit={submit}>
            <h2>Sign in</h2>
            <p className="deck tight">Demo passwords are printed here because this desk is a simulation.</p>
            {error ? <div className="alert">{error}</div> : null}
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            <button className="btn" type="submit" disabled={pending}>
              {pending ? "Checking the seal…" : "Enter the desk"}
            </button>
            <div className="demo-row">
              <button
                type="button"
                className="btn secondary small"
                onClick={() => {
                  setEmail("merchant@securepay.test")
                  setPassword("merchant123")
                }}
              >
                Merchant demo
              </button>
              <button
                type="button"
                className="btn secondary small"
                onClick={() => {
                  setEmail("admin@securepay.test")
                  setPassword("admin123")
                }}
              >
                Admin demo
              </button>
            </div>
          </form>
        </section>
      </div>
    </div>
  )
}

function pillClass(payment: Payment): string {
  if (payment.risk_decision === "block" || payment.status === "failed") return "failed"
  if (payment.risk_decision === "review" && payment.status !== "captured" && payment.status !== "refunded") {
    return "review"
  }
  return payment.status
}

function canCapture(payment: Payment, role: Role): boolean {
  if (payment.status === "refunded" || payment.status === "failed" || payment.risk_decision === "block") return false
  if (payment.status === "captured") return true
  if (payment.risk_decision === "review" && role !== "admin") return false
  return payment.status === "authorized" || payment.status === "created"
}

function OverviewView({ data, rows }: { data: Overview; rows: Payment[] }) {
  const recent = rows.slice(0, 5)
  return (
    <>
      <h1>Overview</h1>
      <p className="deck">
        Captured volume is the sum of ledger credits. Available balance is what remains after refunds. Blocked
        payments never post.
      </p>
      <section className="stats" aria-label="Desk totals">
        <div className="stat">
          <div className="label">Captured volume</div>
          <div className="value">{formatMoney(data.volume_cents, data.currency)}</div>
          <div className="hint">{data.payment_count} payments on the book</div>
        </div>
        <div className="stat">
          <div className="label">Blocked</div>
          <div className="value">{data.blocked_count}</div>
          <div className="hint">{data.review_count} still waiting in review</div>
        </div>
        <div className="stat">
          <div className="label">Available balance</div>
          <div className="value">{formatMoney(data.balance_cents, data.currency)}</div>
          <div className="hint">Opening book was $10,000.00</div>
        </div>
      </section>
      <div className="split lower">
        <section className="panel">
          <h2>Desk rules</h2>
          <ul className="rules">
            <li>Above $5,000.00 the payment is blocked. The ledger does not move.</li>
            <li>Above $1,000.00, or 3 captures for the same customer in an hour, the payment waits for review.</li>
            <li>Capture credits available balance once. A second capture does not credit again.</li>
            <li>A refund cannot exceed the captured amount, and it debits the balance back.</li>
            <li>A dispute cannot exceed the captured amount. If an admin marks it lost, that amount is refunded.</li>
            <li>An admin payout moves available balance into a simulated settlement account marked paid.</li>
          </ul>
        </section>
        <section>
          <h2>Recent payments</h2>
          {recent.length === 0 ? <p className="muted">No payments yet. The book is quiet.</p> : null}
          <ul className="recent">
            {recent.map((payment) => (
              <li key={payment.id}>
                <span>#{payment.id}</span>
                <span>{payment.customer_ref}</span>
                <span>{formatMoney(payment.amount_cents, payment.currency)}</span>
                <span className={`pill ${pillClass(payment)}`}>{payment.status}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  )
}

function PaymentsView({
  role,
  rows,
  merchants,
  balanceCents,
  currency,
  onReload,
}: {
  role: Role
  rows: Payment[]
  merchants: Overview["merchants"]
  balanceCents: number
  currency: string
  onReload: () => Promise<void>
}) {
  const [amount, setAmount] = useState("25.00")
  const [customer, setCustomer] = useState("cust_demo")
  const [tokenValue, setTokenValue] = useState("tok_test_visa_4242")
  const [idem, setIdem] = useState(newIdempotencyKey)
  const [merchantId, setMerchantId] = useState<number | null>(merchants[0]?.id ?? null)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [pending, setPending] = useState(false)

  useEffect(() => {
    if (merchantId == null && merchants[0]) setMerchantId(merchants[0].id)
  }, [merchantId, merchants])

  async function run(action: () => Promise<void>) {
    setPending(true)
    setBanner(null)
    try {
      await action()
      await onReload()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err
      setBanner({ tone: "err", text: err instanceof ApiError ? err.message : "Request failed" })
    } finally {
      setPending(false)
    }
  }

  async function onCreate(event: FormEvent) {
    event.preventDefault()
    const cents = dollarsToCents(amount)
    if (cents == null) {
      setBanner({ tone: "err", text: "Enter a dollar amount with at most two decimals, such as 25.00." })
      return
    }
    if (!/^tok_[A-Za-z0-9_]{4,80}$/.test(tokenValue.trim())) {
      setBanner({ tone: "err", text: "Use a fake token such as tok_test_visa_4242. Card numbers are rejected." })
      return
    }
    if (role === "admin" && merchantId == null) {
      setBanner({ tone: "err", text: "Choose a merchant before creating a payment." })
      return
    }
    await run(async () => {
      const result = await createPayment({
        amount_cents: cents,
        currency: "USD",
        customer_ref: customer.trim(),
        payment_token: tokenValue.trim(),
        idempotencyKey: idem.trim(),
        merchant_id: role === "admin" ? merchantId ?? undefined : undefined,
      })
      const payment = result.payment
      if (result.replay) {
        setBanner({
          tone: "ok",
          text: `Same idempotency key. Returned payment #${payment.id} unchanged. The ledger was not moved.`,
        })
        return
      }
      if (payment.risk_decision === "block") {
        setBanner({
          tone: "err",
          text: `Payment #${payment.id} blocked. Nothing was posted to the ledger.`,
        })
        return
      }
      setBanner({
        tone: "ok",
        text:
          payment.risk_decision === "review"
            ? `Payment #${payment.id} is in review. Funds stay put until an admin captures it.`
            : `Payment #${payment.id} authorized. Funds stay put until you capture.`,
      })
    })
  }

  return (
    <>
      <h1>Payments</h1>
      <p className="deck">
        The idempotency key travels on the request. Submit it twice and you get the same payment back. Capture is the
        step that credits the book.
      </p>
      {banner ? <div className={banner.tone === "ok" ? "note" : "alert"}>{banner.text}</div> : null}
      <div className="split">
        <form className="panel" onSubmit={onCreate}>
          <h2>New intent</h2>
          <p className="balance-line">Available {formatMoney(balanceCents, currency)}</p>
          <div className="presets">
            <button type="button" onClick={() => setAmount("25.00")}>
              $25.00 allow
            </button>
            <button type="button" onClick={() => setAmount("1500.00")}>
              $1,500.00 review
            </button>
            <button type="button" onClick={() => setAmount("6000.00")}>
              $6,000.00 block
            </button>
          </div>
          <label htmlFor="amount">Amount (USD)</label>
          <input id="amount" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} />
          <p className="hint">
            {dollarsToCents(amount) == null
              ? "Amount is not ready."
              : `${dollarsToCents(amount)} cents. Creating this does not move money.`}
          </p>
          <label htmlFor="customer">Customer reference</label>
          <input id="customer" value={customer} onChange={(event) => setCustomer(event.target.value)} required />
          <label htmlFor="token">Fake payment token</label>
          <input
            id="token"
            value={tokenValue}
            spellCheck={false}
            onChange={(event) => setTokenValue(event.target.value)}
            required
          />
          <p className="hint">Accepted and discarded. A primary account number is refused and never stored.</p>
          {role === "admin" ? (
            <>
              <label htmlFor="merchant">Merchant</label>
              <select
                id="merchant"
                value={merchantId ?? ""}
                onChange={(event) => setMerchantId(Number(event.target.value))}
              >
                {merchants.map((merchant) => (
                  <option key={merchant.id} value={merchant.id}>
                    {merchant.name}
                  </option>
                ))}
              </select>
            </>
          ) : null}
          <label htmlFor="idem">Idempotency key</label>
          <input id="idem" value={idem} spellCheck={false} onChange={(event) => setIdem(event.target.value)} required />
          <div className="demo-row">
            <button className="btn" type="submit" disabled={pending}>
              {pending ? "Posting…" : "Create payment"}
            </button>
            <button type="button" className="btn secondary" onClick={() => setIdem(newIdempotencyKey())}>
              New key
            </button>
          </div>
        </form>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Id</th>
                {role === "admin" ? <th>Merchant</th> : null}
                <th>Customer</th>
                <th>Amount</th>
                <th>Status</th>
                <th>Risk</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={role === "admin" ? 7 : 6} className="muted">
                    No payments yet.
                  </td>
                </tr>
              ) : null}
              {rows.map((payment) => (
                <tr key={payment.id}>
                  <td>#{payment.id}</td>
                  {role === "admin" ? <td>{payment.merchant_name}</td> : null}
                  <td>
                    {payment.customer_ref}
                    <div className="hint">{formatWhen(payment.created_at)}</div>
                  </td>
                  <td>{formatMoney(payment.amount_cents, payment.currency)}</td>
                  <td>
                    <span className={`pill ${pillClass(payment)}`}>{payment.status}</span>
                  </td>
                  <td>
                    <span className={`pill ${payment.risk_decision}`}>{payment.risk_decision}</span>
                    {payment.risk_reasons.length > 0 ? (
                      <div className="hint">{payment.risk_reasons.map(reasonText).join(" ")}</div>
                    ) : null}
                  </td>
                  <td className="actions">
                    {canCapture(payment, role) ? (
                      <button
                        type="button"
                        className="btn small"
                        disabled={pending}
                        onClick={() =>
                          run(async () => {
                            const already = payment.status === "captured"
                            await capturePayment(payment.id)
                            setBanner({
                              tone: "ok",
                              text: already
                                ? `Payment #${payment.id} was already captured. Available balance was not credited again.`
                                : `Payment #${payment.id} captured. Available balance was credited once.`,
                            })
                          })
                        }
                      >
                        {payment.status === "captured" ? "Replay capture" : "Capture"}
                      </button>
                    ) : null}
                    {payment.risk_decision === "review" && role !== "admin" && payment.status === "created" ? (
                      <span className="hint">Held for review</span>
                    ) : null}
                    {payment.status === "captured" ? (
                      <button
                        type="button"
                        className="btn secondary small"
                        disabled={pending}
                        onClick={() =>
                          run(async () => {
                            await refundPayment(payment.id)
                            setBanner({
                              tone: "ok",
                              text: `Payment #${payment.id} refunded. Available balance was debited back.`,
                            })
                          })
                        }
                      >
                        Refund
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

function LedgerView({ accounts }: { accounts: LedgerAccount[] }) {
  return (
    <>
      <h1>Ledger</h1>
      <p className="deck">
        A capture writes a credit and increases available balance. A refund writes a debit. A payout debits available
        balance and credits a simulated settlement account. Each movement lands in one database transaction.
      </p>
      {accounts.length === 0 ? <p className="muted">No ledger accounts.</p> : null}
      {accounts.map((account) => (
        <section key={`${account.kind ?? "available"}-${account.id}`} className="ledger-account">
          <header>
            <div>
              <div className="label">
                {account.merchant_name} · {account.currency} · {account.kind === "settlement" ? "settlement" : "available"}
              </div>
              <div className="value">{formatMoney(account.balance_cents, account.currency)}</div>
            </div>
            <p className="hint">
              {account.kind === "settlement" ? "Simulated settlement" : "Available"} account #{account.id}
            </p>
          </header>
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Reference</th>
                <th>Direction</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>
              {account.entries.length === 0 ? (
                <tr>
                  <td colSpan={4} className="muted">
                    No entries. Balance is still the opening book.
                  </td>
                </tr>
              ) : null}
              {account.entries.map((entry) => (
                <tr key={`${entry.source ?? "payment"}-${entry.id}`}>
                  <td>{formatWhen(entry.created_at)}</td>
                  <td>{entry.source === "payout" ? `Payout #${entry.payout_id}` : `#${entry.payment_id}`}</td>
                  <td>
                    <span className={`pill ${entry.direction}`}>{entry.direction}</span>
                  </td>
                  <td className={entry.direction}>
                    {entry.direction === "credit" ? "+" : "−"}
                    {formatMoney(entry.amount_cents, account.currency)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </>
  )
}

function AuditView({ rows }: { rows: AuditRow[] }) {
  return (
    <>
      <h1>Audit log</h1>
      <p className="deck">
        Admin only. Sign-ins, payments, disputes, payouts, API keys, and outbox delivery land here.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Entity</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={5} className="muted">
                  The log is empty.
                </td>
              </tr>
            ) : null}
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{formatWhen(row.ts)}</td>
                <td>{row.actor}</td>
                <td>{row.action}</td>
                <td>{row.entity}</td>
                <td>{row.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function Console({ person, onLogout }: { person: Me; onLogout: () => void }) {
  const [view, setView] = useState<View>("overview")
  const [data, setData] = useState<Overview | null>(null)
  const [rows, setRows] = useState<Payment[]>([])
  const [accounts, setAccounts] = useState<LedgerAccount[]>([])
  const [auditRows, setAuditRows] = useState<AuditRow[]>([])
  const [disputeRows, setDisputeRows] = useState<Dispute[]>([])
  const [payoutBook, setPayoutBook] = useState<PayoutList>({ payouts: [], settlement_accounts: [] })
  const [keyRows, setKeyRows] = useState<ApiKeyRow[]>([])
  const [outboxRows, setOutboxRows] = useState<OutboxEvent[]>([])
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)

  async function reload() {
    const [nextOverview, nextPayments, nextLedger, nextDisputes, nextPayouts, nextKeys, nextOutbox] =
      await Promise.all([overview(), payments(), ledger(), disputes(), payouts(), apiKeys(), outbox()])
    setData(nextOverview)
    setRows(nextPayments)
    setAccounts(nextLedger.accounts)
    setDisputeRows(nextDisputes)
    setPayoutBook(nextPayouts)
    setKeyRows(nextKeys)
    setOutboxRows(nextOutbox)
    if (person.role === "admin" && view === "audit") {
      setAuditRows(await audit())
    }
  }

  useEffect(() => {
    let cancel = false
    setLoading(true)
    reload()
      .catch((err) => {
        if (cancel) return
        if (err instanceof ApiError && err.status === 401) {
          onLogout()
          return
        }
        setError(err instanceof ApiError ? err.message : "Could not load the desk")
      })
      .finally(() => {
        if (!cancel) setLoading(false)
      })
    return () => {
      cancel = true
    }
    // Reload when the signed-in person changes. View-specific audit loads below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [person.id])

  useEffect(() => {
    if (view !== "audit" || person.role !== "admin") return
    audit()
      .then(setAuditRows)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) onLogout()
        else setError(err instanceof ApiError ? err.message : "Could not load the audit log")
      })
  }, [view, person.role, person.id, onLogout])

  const links: { id: View; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "payments", label: "Payments" },
    { id: "ledger", label: "Ledger" },
    { id: "disputes", label: "Disputes" },
    { id: "settlement", label: "Settlement" },
    { id: "keys", label: "API keys" },
    { id: "outbox", label: "Outbox" },
  ]
  if (person.role === "admin") links.push({ id: "audit", label: "Audit log" })

  return (
    <div className="app">
      <SimulationBar />
      <div className="shell">
        <aside className="nav">
          <div className="brand light">
            <span className="seal">SP</span>
            <span>
              <span className="wordmark">SecurePay</span>
              <span className="brand-sub">Simulation desk</span>
            </span>
          </div>
          <nav>
            {links.map((link) => (
              <button
                key={link.id}
                type="button"
                className="nav-link"
                aria-current={view === link.id ? "page" : undefined}
                onClick={() => setView(link.id)}
              >
                {link.label}
              </button>
            ))}
          </nav>
          <div className="who">
            <div>{person.email}</div>
            <div className="hint light">{person.role === "admin" ? "Administrator" : person.merchant_name}</div>
            <button type="button" className="btn secondary small light-btn" onClick={onLogout}>
              Log out
            </button>
          </div>
        </aside>
        <main className="main">
          {error ? <div className="alert">{error}</div> : null}
          {loading || !data ? (
            <p className="muted">Loading the desk…</p>
          ) : (
            <>
              {view === "overview" ? <OverviewView data={data} rows={rows} /> : null}
              {view === "payments" ? (
                <PaymentsView
                  role={person.role}
                  rows={rows}
                  merchants={data.merchants}
                  balanceCents={data.balance_cents}
                  currency={data.currency}
                  onReload={async () => {
                    try {
                      await reload()
                    } catch (err) {
                      if (err instanceof ApiError && err.status === 401) onLogout()
                      else throw err
                    }
                  }}
                />
              ) : null}
              {view === "ledger" ? <LedgerView accounts={accounts} /> : null}
              {view === "disputes" ? (
                <DisputesView
                  role={person.role}
                  rows={rows}
                  disputes={disputeRows}
                  onReload={async () => {
                    try {
                      await reload()
                    } catch (err) {
                      if (err instanceof ApiError && err.status === 401) onLogout()
                      else throw err
                    }
                  }}
                />
              ) : null}
              {view === "settlement" ? (
                <SettlementView
                  role={person.role}
                  book={payoutBook}
                  merchants={data.merchants}
                  onReload={async () => {
                    try {
                      await reload()
                    } catch (err) {
                      if (err instanceof ApiError && err.status === 401) onLogout()
                      else throw err
                    }
                  }}
                />
              ) : null}
              {view === "keys" ? (
                <KeysView
                  role={person.role}
                  keys={keyRows}
                  merchants={data.merchants}
                  onReload={async () => {
                    try {
                      await reload()
                    } catch (err) {
                      if (err instanceof ApiError && err.status === 401) onLogout()
                      else throw err
                    }
                  }}
                />
              ) : null}
              {view === "outbox" ? (
                <OutboxView
                  rows={outboxRows}
                  onReload={async () => {
                    try {
                      await reload()
                    } catch (err) {
                      if (err instanceof ApiError && err.status === 401) onLogout()
                      else throw err
                    }
                  }}
                />
              ) : null}
              {view === "audit" && person.role === "admin" ? <AuditView rows={auditRows} /> : null}
            </>
          )}
        </main>
      </div>
    </div>
  )
}

export function App() {
  const [person, setPerson] = useState<Me | null>(null)
  const [booting, setBooting] = useState(true)

  useEffect(() => {
    if (!getToken()) {
      setBooting(false)
      return
    }
    me()
      .then(setPerson)
      .catch(() => setToken(null))
      .finally(() => setBooting(false))
  }, [])

  if (booting) {
    return (
      <div className="app">
        <SimulationBar />
        <p className="boot">Opening SecurePay…</p>
      </div>
    )
  }
  if (!person) return <Login onSuccess={setPerson} />
  return (
    <Console
      person={person}
      onLogout={() => {
        setToken(null)
        setPerson(null)
      }}
    />
  )
}
