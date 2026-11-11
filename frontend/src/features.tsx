import { FormEvent, useEffect, useState } from "react"
import {
  ApiError,
  createApiKey,
  createPayout,
  deliverOutbox,
  openDispute,
  resolveDispute,
  revokeApiKey,
} from "./api"
import { centsToDollars, dollarsToCents, formatMoney, formatWhen, newIdempotencyKey } from "./format"
import type {
  ApiKeyCreated,
  ApiKeyRow,
  Dispute,
  OutboxEvent,
  Overview,
  Payment,
  PayoutList,
  Role,
} from "./types"

type Banner = { tone: "ok" | "err"; text: string }

async function runAction(
  setPending: (value: boolean) => void,
  setBanner: (banner: Banner | null) => void,
  onReload: () => Promise<void>,
  action: () => Promise<void>,
) {
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

function remainingCaptured(payment: Payment): number {
  return payment.amount_cents - payment.refunded_cents
}

export function DisputesView({
  role,
  rows,
  disputes,
  onReload,
}: {
  role: Role
  rows: Payment[]
  disputes: Dispute[]
  onReload: () => Promise<void>
}) {
  const captured = rows.filter((payment) => payment.status === "captured" && remainingCaptured(payment) > 0)
  const [paymentId, setPaymentId] = useState<number | null>(captured[0]?.id ?? null)
  const [reason, setReason] = useState("Goods not received")
  const [amount, setAmount] = useState(captured[0] ? centsToDollars(remainingCaptured(captured[0])) : "")
  const [banner, setBanner] = useState<Banner | null>(null)
  const [pending, setPending] = useState(false)

  useEffect(() => {
    if (paymentId != null && captured.some((payment) => payment.id === paymentId)) return
    const next = captured[0]
    setPaymentId(next?.id ?? null)
    if (next) setAmount(centsToDollars(remainingCaptured(next)))
  }, [captured, paymentId])

  const selected = captured.find((payment) => payment.id === paymentId) ?? null

  async function onOpen(event: FormEvent) {
    event.preventDefault()
    if (selected == null) {
      setBanner({ tone: "err", text: "Choose a captured payment." })
      return
    }
    const cents = dollarsToCents(amount)
    if (cents == null) {
      setBanner({ tone: "err", text: "Enter a dollar amount with at most two decimals." })
      return
    }
    if (cents > remainingCaptured(selected)) {
      setBanner({
        tone: "err",
        text: `That amount is above the remaining captured ${formatMoney(remainingCaptured(selected), selected.currency)}.`,
      })
      return
    }
    await runAction(setPending, setBanner, onReload, async () => {
      const dispute = await openDispute(selected.id, { reason: reason.trim(), amount_cents: cents })
      setBanner({
        tone: "ok",
        text: `Dispute #${dispute.id} is open on payment #${selected.id}. Funds stay put until an admin marks it lost.`,
      })
      setReason("Goods not received")
    })
  }

  return (
    <>
      <h1>Disputes</h1>
      <p className="deck">
        A merchant or an admin can open a dispute on a captured payment. The amount cannot exceed what was captured.
        Only an admin can mark it won or lost. Lost posts the refund through the ledger.
      </p>
      {banner ? <div className={banner.tone === "ok" ? "note" : "alert"}>{banner.text}</div> : null}
      <div className="split">
        <form className="panel" onSubmit={onOpen}>
          <h2>Open a dispute</h2>
          {captured.length === 0 ? <p className="muted">No captured payment has an amount left to dispute.</p> : null}
          <label htmlFor="dispute-payment">Captured payment</label>
          <select
            id="dispute-payment"
            value={paymentId ?? ""}
            onChange={(event) => {
              const nextId = Number(event.target.value)
              setPaymentId(nextId)
              const next = captured.find((payment) => payment.id === nextId)
              if (next) setAmount(centsToDollars(remainingCaptured(next)))
            }}
            disabled={captured.length === 0}
          >
            {captured.map((payment) => (
              <option key={payment.id} value={payment.id}>
                #{payment.id} · {payment.customer_ref} · {formatMoney(remainingCaptured(payment), payment.currency)} left
              </option>
            ))}
          </select>
          <label htmlFor="dispute-amount">Amount (USD)</label>
          <input
            id="dispute-amount"
            inputMode="decimal"
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            required
          />
          <p className="hint">
            {selected
              ? `Captured ${formatMoney(selected.amount_cents, selected.currency)}. Remaining ${formatMoney(remainingCaptured(selected), selected.currency)}.`
              : "Capture a payment before opening a dispute."}
          </p>
          <label htmlFor="dispute-reason">Reason</label>
          <textarea
            id="dispute-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            required
          />
          <button className="btn" type="submit" disabled={pending || captured.length === 0}>
            {pending ? "Opening…" : "Open dispute"}
          </button>
        </form>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Id</th>
                <th>Payment</th>
                {role === "admin" ? <th>Merchant</th> : null}
                <th>Amount</th>
                <th>Status</th>
                <th>Reason</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {disputes.length === 0 ? (
                <tr>
                  <td colSpan={role === "admin" ? 7 : 6} className="muted">
                    No disputes yet.
                  </td>
                </tr>
              ) : null}
              {disputes.map((dispute) => (
                <tr key={dispute.id}>
                  <td>#{dispute.id}</td>
                  <td>
                    #{dispute.payment_id}
                    <div className="hint">{formatWhen(dispute.created_at)}</div>
                  </td>
                  {role === "admin" ? <td>{dispute.merchant_name}</td> : null}
                  <td>{formatMoney(dispute.amount_cents, dispute.currency)}</td>
                  <td>
                    <span className={`pill ${dispute.status}`}>{dispute.status}</span>
                  </td>
                  <td>
                    {dispute.reason}
                    <div className="hint">{dispute.opened_by}</div>
                  </td>
                  <td className="actions">
                    {role === "admin" && dispute.status === "open" ? (
                      <>
                        <button
                          type="button"
                          className="btn small"
                          disabled={pending}
                          onClick={() =>
                            runAction(setPending, setBanner, onReload, async () => {
                              await resolveDispute(dispute.id, "won")
                              setBanner({
                                tone: "ok",
                                text: `Dispute #${dispute.id} marked won. The available balance was not debited.`,
                              })
                            })
                          }
                        >
                          Won
                        </button>
                        <button
                          type="button"
                          className="btn secondary small"
                          disabled={pending}
                          onClick={() =>
                            runAction(setPending, setBanner, onReload, async () => {
                              await resolveDispute(dispute.id, "lost")
                              setBanner({
                                tone: "ok",
                                text: `Dispute #${dispute.id} marked lost. ${formatMoney(dispute.amount_cents, dispute.currency)} was refunded from the available balance.`,
                              })
                            })
                          }
                        >
                          Lost
                        </button>
                      </>
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

export function SettlementView({
  role,
  book,
  merchants,
  onReload,
}: {
  role: Role
  book: PayoutList
  merchants: Overview["merchants"]
  onReload: () => Promise<void>
}) {
  const [merchantId, setMerchantId] = useState<number | null>(merchants[0]?.id ?? null)
  const [amount, setAmount] = useState(merchants[0] ? centsToDollars(merchants[0].balance_cents) : "")
  const [idem, setIdem] = useState(newIdempotencyKey)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [pending, setPending] = useState(false)
  const selected = merchants.find((merchant) => merchant.id === merchantId) ?? null

  useEffect(() => {
    if (merchantId == null && merchants[0]) setMerchantId(merchants[0].id)
  }, [merchantId, merchants])

  return (
    <>
      <h1>Settlement</h1>
      <p className="deck">
        An administrator payout moves available balance into a settlement account and marks the entry paid. It is
        simulated. The same idempotency key does not move the balance again, and the available balance cannot go
        negative.
      </p>
      {banner ? <div className={banner.tone === "ok" ? "note" : "alert"}>{banner.text}</div> : null}
      <div className="split">
        {role === "admin" ? (
          <form
            className="panel"
            onSubmit={(event) => {
              event.preventDefault()
              if (merchantId == null) {
                setBanner({ tone: "err", text: "Choose a merchant." })
                return
              }
              const cents = dollarsToCents(amount)
              if (cents == null) {
                setBanner({ tone: "err", text: "Enter a dollar amount with at most two decimals." })
                return
              }
              if (selected && cents > selected.balance_cents) {
                setBanner({
                  tone: "err",
                  text: `That payout is above the available ${formatMoney(selected.balance_cents, selected.currency)}.`,
                })
                return
              }
              void runAction(setPending, setBanner, onReload, async () => {
                const result = await createPayout({
                  merchant_id: merchantId,
                  amount_cents: cents,
                  idempotencyKey: idem.trim(),
                })
                setBanner({
                  tone: "ok",
                  text: result.replay
                    ? `Same idempotency key. Payout #${result.payout.id} was already paid. Available balance was not moved again.`
                    : `Payout #${result.payout.id} paid ${formatMoney(cents, "USD")} into the simulated settlement account.`,
                })
              })
            }}
          >
            <h2>Run a payout</h2>
            <p className="balance-line">
              {selected ? formatMoney(selected.balance_cents, selected.currency) : "—"}
            </p>
            <p className="hint">Available balance. This is not a bank transfer.</p>
            <label htmlFor="payout-merchant">Merchant</label>
            <select
              id="payout-merchant"
              value={merchantId ?? ""}
              onChange={(event) => {
                const nextId = Number(event.target.value)
                setMerchantId(nextId)
                const next = merchants.find((merchant) => merchant.id === nextId)
                if (next) setAmount(centsToDollars(next.balance_cents))
              }}
            >
              {merchants.map((merchant) => (
                <option key={merchant.id} value={merchant.id}>
                  {merchant.name}
                </option>
              ))}
            </select>
            <label htmlFor="payout-amount">Amount (USD)</label>
            <input
              id="payout-amount"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              required
            />
            <label htmlFor="payout-idem">Idempotency key</label>
            <input
              id="payout-idem"
              value={idem}
              spellCheck={false}
              onChange={(event) => setIdem(event.target.value)}
              required
            />
            <div className="demo-row">
              <button className="btn" type="submit" disabled={pending || merchants.length === 0}>
                {pending ? "Moving…" : "Pay out"}
              </button>
              <button type="button" className="btn secondary" onClick={() => setIdem(newIdempotencyKey())}>
                New key
              </button>
            </div>
          </form>
        ) : (
          <section className="panel">
            <h2>How a payout lands</h2>
            <p className="muted">
              An administrator runs the payout. Your available balance drops, and the same amount appears here as paid.
              Nothing is sent to a bank.
            </p>
          </section>
        )}
        <div>
          {book.settlement_accounts.length === 0 ? (
            <p className="muted">No settlement balance yet. Available funds are still on the merchant ledger.</p>
          ) : (
            <section className="stats" aria-label="Settlement balances">
              {book.settlement_accounts.map((account) => (
                <div className="stat" key={account.id}>
                  <div className="label">{account.merchant_name} settlement</div>
                  <div className="value">{formatMoney(account.balance_cents, account.currency)}</div>
                  <div className="hint">Simulated account #{account.id}</div>
                </div>
              ))}
            </section>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Id</th>
                  {role === "admin" ? <th>Merchant</th> : null}
                  <th>Amount</th>
                  <th>Status</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {book.payouts.length === 0 ? (
                  <tr>
                    <td colSpan={role === "admin" ? 5 : 4} className="muted">
                      No payouts yet.
                    </td>
                  </tr>
                ) : null}
                {book.payouts.map((payout) => (
                  <tr key={payout.id}>
                    <td>#{payout.id}</td>
                    {role === "admin" ? <td>{payout.merchant_name}</td> : null}
                    <td>{formatMoney(payout.amount_cents, payout.currency)}</td>
                    <td>
                      <span className={`pill ${payout.status}`}>{payout.status}</span>
                    </td>
                    <td>
                      {formatWhen(payout.created_at)}
                      <div className="hint">{payout.idempotency_key}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  )
}

export function KeysView({
  role,
  keys,
  merchants,
  onReload,
}: {
  role: Role
  keys: ApiKeyRow[]
  merchants: Overview["merchants"]
  onReload: () => Promise<void>
}) {
  const [name, setName] = useState("Desk key")
  const [merchantId, setMerchantId] = useState<number | null>(merchants[0]?.id ?? null)
  const [revealed, setRevealed] = useState<ApiKeyCreated | null>(null)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [pending, setPending] = useState(false)

  return (
    <>
      <h1>API keys</h1>
      <p className="deck">
        A key authorizes <code>POST /api/payments</code> with <code>Authorization: Bearer</code> in place of the user
        JWT. SecurePay stores only a hash. The secret is shown once, on this screen, and a revoked key stops working.
      </p>
      {banner ? <div className={banner.tone === "ok" ? "note" : "alert"}>{banner.text}</div> : null}
      {revealed ? (
        <div className="secret-box" role="status">
          <strong>Secret shown once</strong>
          {revealed.secret}
          <div className="hint light">{revealed.note}</div>
        </div>
      ) : null}
      <div className="split lower">
        <form
          className="panel"
          onSubmit={(event) => {
            event.preventDefault()
            if (role === "admin" && merchantId == null) {
              setBanner({ tone: "err", text: "Choose a merchant." })
              return
            }
            void runAction(setPending, setBanner, onReload, async () => {
              const created = await createApiKey({
                name: name.trim(),
                merchant_id: role === "admin" ? merchantId ?? undefined : undefined,
              })
              setRevealed(created)
              setBanner({
                tone: "ok",
                text: `Key ${created.prefix}… was created. Copy the secret now. It will not be shown again.`,
              })
            })
          }}
        >
          <h2>Create a key</h2>
          <label htmlFor="key-name">Name</label>
          <input id="key-name" value={name} onChange={(event) => setName(event.target.value)} required />
          {role === "admin" ? (
            <>
              <label htmlFor="key-merchant">Merchant</label>
              <select
                id="key-merchant"
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
          <button className="btn" type="submit" disabled={pending}>
            {pending ? "Sealing…" : "Create key"}
          </button>
        </form>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                {role === "admin" ? <th>Merchant</th> : null}
                <th>Prefix</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {keys.length === 0 ? (
                <tr>
                  <td colSpan={role === "admin" ? 5 : 4} className="muted">
                    No keys yet.
                  </td>
                </tr>
              ) : null}
              {keys.map((key) => (
                <tr key={key.id}>
                  <td>
                    {key.name}
                    <div className="hint">{formatWhen(key.created_at)}</div>
                  </td>
                  {role === "admin" ? <td>{key.merchant_name}</td> : null}
                  <td>{key.prefix}…</td>
                  <td>
                    <span className={`pill ${key.revoked_at ? "revoked" : "paid"}`}>
                      {key.revoked_at ? "revoked" : "active"}
                    </span>
                  </td>
                  <td className="actions">
                    {key.revoked_at ? null : (
                      <button
                        type="button"
                        className="btn secondary small"
                        disabled={pending}
                        onClick={() =>
                          runAction(setPending, setBanner, onReload, async () => {
                            await revokeApiKey(key.id)
                            if (revealed?.id === key.id) setRevealed(null)
                            setBanner({ tone: "ok", text: `${key.prefix}… was revoked.` })
                          })
                        }
                      >
                        Revoke
                      </button>
                    )}
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

export function OutboxView({
  rows,
  onReload,
}: {
  rows: OutboxEvent[]
  onReload: () => Promise<void>
}) {
  const [banner, setBanner] = useState<Banner | null>(null)
  const [pending, setPending] = useState(false)
  const waiting = rows.filter((row) => row.status === "pending").length

  return (
    <>
      <h1>Outbox</h1>
      <p className="deck">
        A capture writes <code>payment.captured</code>. A lost dispute writes <code>dispute.lost</code>. Deliver marks
        those pending rows sent on this machine. SecurePay does not call the internet.
      </p>
      {banner ? <div className={banner.tone === "ok" ? "note" : "alert"}>{banner.text}</div> : null}
      <div className="demo-row">
        <button
          type="button"
          className="btn"
          disabled={pending || waiting === 0}
          onClick={() =>
            runAction(setPending, setBanner, onReload, async () => {
              const result = await deliverOutbox()
              setBanner({
                tone: "ok",
                text:
                  result.delivered === 0
                    ? "Nothing was pending. No network call was made."
                    : `Marked ${result.delivered} pending ${result.delivered === 1 ? "row" : "rows"} sent. No network call was made.`,
              })
            })
          }
        >
          {pending ? "Marking…" : `Mark ${waiting} pending as sent`}
        </button>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Id</th>
              <th>Event</th>
              <th>Status</th>
              <th>When</th>
              <th>Payload</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={5} className="muted">
                  The outbox is empty. Capture a payment, or lose a dispute, to write a pending row.
                </td>
              </tr>
            ) : null}
            {rows.map((row) => (
              <tr key={row.id}>
                <td>#{row.id}</td>
                <td>{row.event_type}</td>
                <td>
                  <span className={`pill ${row.status}`}>{row.status}</span>
                </td>
                <td>
                  {formatWhen(row.created_at)}
                  {row.sent_at ? <div className="hint">Sent {formatWhen(row.sent_at)}</div> : null}
                </td>
                <td>{JSON.stringify(row.payload)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
