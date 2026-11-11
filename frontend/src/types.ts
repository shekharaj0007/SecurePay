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
  payment_id: number
  direction: "debit" | "credit"
  amount_cents: number
  created_at: string
}

export type LedgerAccount = {
  id: number
  merchant_id: number
  merchant_name: string
  currency: string
  balance_cents: number
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
