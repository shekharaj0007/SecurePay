import type {
  AuditRow,
  LedgerResponse,
  LoginResponse,
  Me,
  Overview,
  Payment,
} from "./types"

const TOKEN_KEY = "securepay.token"
let token: string | null = localStorage.getItem(TOKEN_KEY)

export function getToken(): string | null {
  return token
}

export function setToken(value: string | null) {
  token = value
  if (value) localStorage.setItem(TOKEN_KEY, value)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, body: unknown) {
    super(messageFrom(body, status))
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

function messageFrom(body: unknown, status: number): string {
  if (status === 0) return "Cannot reach the SecurePay API. Start it on 127.0.0.1:8000."
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === "string") return detail
    if (detail && typeof detail === "object" && "message" in detail) {
      return String((detail as { message: unknown }).message)
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) return String((item as { msg: unknown }).msg)
          return String(item)
        })
        .join(" ")
    }
    return JSON.stringify(detail)
  }
  return `Request failed (${status})`
}

async function request<T>(path: string, options: RequestInit = {}, auth = true): Promise<{ data: T; response: Response }> {
  const headers = new Headers(options.headers)
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  if (auth && token) headers.set("Authorization", `Bearer ${token}`)
  let response: Response
  try {
    response = await fetch(path, { ...options, headers })
  } catch {
    throw new ApiError(0, { detail: "Cannot reach the SecurePay API. Start it on 127.0.0.1:8000." })
  }
  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text }
    }
  }
  if (!response.ok) throw new ApiError(response.status, data)
  return { data: data as T, response }
}

export async function login(email: string, password: string): Promise<LoginResponse> {
  const { data } = await request<LoginResponse>(
    "/api/auth/login",
    { method: "POST", body: JSON.stringify({ email, password }) },
    false,
  )
  return data
}

export async function me(): Promise<Me> {
  const { data } = await request<Me>("/api/me")
  return data
}

export async function overview(): Promise<Overview> {
  const { data } = await request<Overview>("/api/overview")
  return data
}

export async function payments(): Promise<Payment[]> {
  const { data } = await request<Payment[]>("/api/payments")
  return data
}

export async function ledger(): Promise<LedgerResponse> {
  const { data } = await request<LedgerResponse>("/api/ledger")
  return data
}

export async function audit(): Promise<AuditRow[]> {
  const { data } = await request<AuditRow[]>("/api/audit")
  return data
}

export async function createPayment(input: {
  amount_cents: number
  currency: string
  customer_ref: string
  payment_token: string
  idempotencyKey: string
  merchant_id?: number
}): Promise<{ payment: Payment; replay: boolean }> {
  const body: Record<string, unknown> = {
    amount_cents: input.amount_cents,
    currency: input.currency,
    customer_ref: input.customer_ref,
    payment_token: input.payment_token,
  }
  if (input.merchant_id != null) body.merchant_id = input.merchant_id
  const { data, response } = await request<Payment>("/api/payments", {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": input.idempotencyKey },
  })
  return { payment: data, replay: response.headers.get("X-Idempotent-Replay") === "true" }
}

export async function capturePayment(id: number): Promise<Payment> {
  const { data } = await request<Payment>(`/api/payments/${id}/capture`, { method: "POST" })
  return data
}

export async function refundPayment(id: number): Promise<Payment> {
  const { data } = await request<Payment>(`/api/payments/${id}/refund`, {
    method: "POST",
    body: JSON.stringify({}),
  })
  return data
}
