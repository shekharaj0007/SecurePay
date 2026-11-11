import re

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
    payment_id: int
    direction: str
    amount_cents: int
    created_at: str


class LedgerAccountOut(BaseModel):
    id: int
    merchant_id: int
    merchant_name: str
    currency: str
    balance_cents: int
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
