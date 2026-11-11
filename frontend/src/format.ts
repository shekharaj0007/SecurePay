export function formatMoney(cents: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(cents / 100)
}

export function formatWhen(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date)
}

const REASONS: Record<string, string> = {
  amount_exceeds_500000_cents: "Amount is above $5,000.00.",
  amount_exceeds_100000_cents: "Amount is above $1,000.00.",
  customer_capture_velocity_3_in_1h: "This customer already has 3 captures in the last hour.",
}

export function reasonText(code: string): string {
  return REASONS[code] ?? code
}

export function dollarsToCents(input: string): number | null {
  const trimmed = input.trim()
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return null
  const [whole, frac = ""] = trimmed.split(".")
  const cents = Number(whole) * 100 + Number((frac + "00").slice(0, 2))
  if (!Number.isSafeInteger(cents) || cents <= 0 || cents > 50_000_000) return null
  return cents
}

export function newIdempotencyKey(): string {
  const uuid =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `idem_${uuid}`
}
