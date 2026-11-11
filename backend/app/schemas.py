import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def looks_like_pan(value: str) -> bool:
    """True when a string is a primary account number, including spaced groups.

    Fake tokens such as tok_test_visa_4242 are not PANs.
    """
    if value.startswith("tok_"):
        return False
    digits = re.sub(r"\D", "", value)
    if 13 <= len(digits) <= 19 and value[:1].isdigit():
        return True
    return re.search(r"\d(?:[ -]?\d){12,18}", value) is not None and len(digits) >= 13


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    role: str


class MeOut(BaseModel):
    id: int
    email: str
    role: str
    merchant_id: int | None
    merchant_name: str | None


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: int = Field(gt=0, le=50_000_000)
    currency: str = "USD"
    customer_ref: str = Field(min_length=1, max_length=64)
    payment_token: str | None = Field(
        default=None,
        description="Fake token only, for example tok_test_visa_4242. Validated then discarded. Never stored.",
    )
    merchant_id: int | None = Field(default=None, gt=0)

    @field_validator("currency")
    @classmethod
    def currency_usd(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized != "USD":
            raise ValueError("Only USD is supported in this simulation")
        return normalized

    @field_validator("customer_ref")
    @classmethod
    def customer_ref_shape(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", normalized):
            raise ValueError(
                "customer_ref must be 1-64 characters: letters, numbers, dot, underscore, or hyphen"
            )
        return normalized

    @field_validator("payment_token")
    @classmethod
    def token_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized == "":
            return None
        if looks_like_pan(normalized):
            raise ValueError(
                "Card numbers are not accepted. Use a fake token such as tok_test_visa_4242."
            )
        if not re.fullmatch(r"tok_[A-Za-z0-9_]{4,80}", normalized):
            raise ValueError("payment_token must look like tok_test_visa_4242")
        return normalized

    @model_validator(mode="after")
    def reject_pans(self) -> "PaymentCreate":
        for label, value in (
            ("customer_ref", self.customer_ref),
            ("payment_token", self.payment_token),
            ("currency", self.currency),
        ):
            if value and looks_like_pan(value):
                raise ValueError(
                    f"{label} looks like a card number. Card numbers are not accepted. "
                    "Use a fake token such as tok_test_visa_4242."
                )
        return self


class RefundIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: int | None = Field(default=None, gt=0, le=50_000_000)


class PaymentOut(BaseModel):
    id: int
    merchant_id: int
    merchant_name: str
    idempotency_key: str
    amount_cents: int
    currency: str
    status: str
    risk_decision: str
    risk_reasons: list[str]
    customer_ref: str
    refunded_cents: int
    created_at: str


class LedgerEntryOut(BaseModel):
    id: int
    payment_id: int | None = None
    payout_id: int | None = None
    direction: str
    amount_cents: int
    created_at: str
    source: str = "payment"


class LedgerAccountOut(BaseModel):
    id: int
    merchant_id: int
    merchant_name: str
    currency: str
    balance_cents: int
    kind: str = "available"
    entries: list[LedgerEntryOut]


class LedgerOut(BaseModel):
    accounts: list[LedgerAccountOut]


class MerchantBalance(BaseModel):
    id: int
    name: str
    currency: str
    balance_cents: int


class OverviewOut(BaseModel):
    simulation: bool = True
    payment_count: int
    volume_cents: int
    blocked_count: int
    review_count: int
    balance_cents: int
    currency: str
    merchants: list[MerchantBalance]


class AuditOut(BaseModel):
    id: int
    actor: str
    action: str
    entity: str
    detail: str
    ts: str


class DisputeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    amount_cents: int = Field(gt=0, le=50_000_000)

    @field_validator("reason")
    @classmethod
    def reason_text(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("A dispute reason is required")
        if looks_like_pan(normalized):
            raise ValueError("Card numbers are not accepted.")
        return normalized


class DisputeResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["won", "lost"]


class DisputeOut(BaseModel):
    id: int
    payment_id: int
    merchant_id: int
    merchant_name: str
    amount_cents: int
    currency: str
    reason: str
    status: str
    opened_by: str
    created_at: str
    resolved_at: str | None


class PayoutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merchant_id: int = Field(gt=0)
    amount_cents: int = Field(gt=0, le=50_000_000)
    currency: str = "USD"

    @field_validator("currency")
    @classmethod
    def currency_usd(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized != "USD":
            raise ValueError("Only USD is supported in this simulation")
        return normalized


class PayoutOut(BaseModel):
    id: int
    merchant_id: int
    merchant_name: str
    amount_cents: int
    currency: str
    status: str
    idempotency_key: str
    available_balance_cents: int
    settlement_balance_cents: int
    created_at: str
    simulation: bool = True


class SettlementAccountOut(BaseModel):
    id: int
    merchant_id: int
    merchant_name: str
    currency: str
    balance_cents: int


class PayoutListOut(BaseModel):
    payouts: list[PayoutOut]
    settlement_accounts: list[SettlementAccountOut]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Desk key", min_length=1, max_length=80)
    merchant_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def name_text(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("A key name is required")
        if looks_like_pan(normalized):
            raise ValueError("Card numbers are not accepted.")
        return normalized


class ApiKeyOut(BaseModel):
    id: int
    merchant_id: int
    merchant_name: str
    name: str
    prefix: str
    created_at: str
    revoked_at: str | None


class ApiKeyCreated(ApiKeyOut):
    secret: str
    note: str = "Copy this secret now. SecurePay stores only a hash and cannot show it again."


class OutboxOut(BaseModel):
    id: int
    merchant_id: int
    event_type: str
    payload: dict
    status: str
    created_at: str
    sent_at: str | None


class OutboxDeliverOut(BaseModel):
    delivered: int
    events: list[OutboxOut]
    mode: str = "simulation"
    note: str = "Marked sent locally. No network call was made."
