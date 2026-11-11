export type Role = "admin" | "merchant"

export type Me = {
  id: number
  email: string
  role: Role
  merchant_id: number | null
  merchant_name: string | null
}

export type LoginResponse = {
  access_token: string
  token_type: string
  email: string
  role: Role
}

export type Payment = {
  id: number
  merchant_id: number
  merchant_name: string
  idempotency_key: string
  amount_cents: number
  currency: string
  status: "created" | "authorized" | "captured" | "failed" | "refunded"
  risk_decision: "allow" | "review" | "block"
  risk_reasons: string[]
  customer_ref: string
  refunded_cents: number
  created_at: string
}

export type Overview = {
  simulation: boolean
  payment_count: number
  volume_cents: number
  blocked_count: number
  review_count: number
  balance_cents: number
  currency: string
  merchants: { id: number; name: string; currency: string; balance_cents: number }[]
}

export type LedgerEntry = {
  id: number
  payment_id: number | null
  payout_id?: number | null
  direction: "debit" | "credit"
  amount_cents: number
  created_at: string
  source?: "payment" | "payout"
}

export type LedgerAccount = {
  id: number
  merchant_id: number
  merchant_name: string
  currency: string
  balance_cents: number
  kind?: "available" | "settlement"
  entries: LedgerEntry[]
}

export type LedgerResponse = {
  accounts: LedgerAccount[]
}

export type AuditRow = {
  id: number
  actor: string
  action: string
  entity: string
  detail: string
  ts: string
}

export type Dispute = {
  id: number
  payment_id: number
  merchant_id: number
  merchant_name: string
  amount_cents: number
  currency: string
  reason: string
  status: "open" | "won" | "lost"
  opened_by: string
  created_at: string
  resolved_at: string | null
}

export type Payout = {
  id: number
  merchant_id: number
  merchant_name: string
  amount_cents: number
  currency: string
  status: string
  idempotency_key: string
  available_balance_cents: number
  settlement_balance_cents: number
  created_at: string
  simulation: boolean
}

export type SettlementAccount = {
  id: number
  merchant_id: number
  merchant_name: string
  currency: string
  balance_cents: number
}

export type PayoutList = {
  payouts: Payout[]
  settlement_accounts: SettlementAccount[]
}

export type ApiKeyRow = {
  id: number
  merchant_id: number
  merchant_name: string
  name: string
  prefix: string
  created_at: string
  revoked_at: string | null
}

export type ApiKeyCreated = ApiKeyRow & {
  secret: string
  note: string
}

export type OutboxEvent = {
  id: number
  merchant_id: number
  event_type: string
  payload: Record<string, unknown>
  status: "pending" | "sent"
  created_at: string
  sent_at: string | null
}

export type OutboxDeliver = {
  delivered: number
  events: OutboxEvent[]
  mode: string
  note: string
}
