"""Risk checks run before a capture posts to the ledger.

Block when amount_cents is greater than 500000.
Review when amount_cents is greater than 100000, or when the same customer_ref
already has 3 or more captures in the last hour for this merchant.
"""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.database import utcnow
from backend.app.models import LedgerEntry, Payment

BLOCK_ABOVE_CENTS = 500_000
REVIEW_ABOVE_CENTS = 100_000
VELOCITY_CAPTURES = 3
VELOCITY_WINDOW = timedelta(hours=1)


def assess_risk(
    db: Session,
    merchant_id: int,
    amount_cents: int,
    customer_ref: str,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if amount_cents > BLOCK_ABOVE_CENTS:
        reasons.append("amount_exceeds_500000_cents")
    if amount_cents > REVIEW_ABOVE_CENTS:
        reasons.append("amount_exceeds_100000_cents")

    since = utcnow() - VELOCITY_WINDOW
    velocity = db.scalar(
        select(func.count(LedgerEntry.id))
        .join(Payment, Payment.id == LedgerEntry.payment_id)
        .where(
            Payment.merchant_id == merchant_id,
            Payment.customer_ref == customer_ref,
            LedgerEntry.direction == "credit",
            LedgerEntry.created_at >= since,
        )
    )
    if int(velocity or 0) >= VELOCITY_CAPTURES:
        reasons.append("customer_capture_velocity_3_in_1h")

    if amount_cents > BLOCK_ABOVE_CENTS:
        return "block", reasons
    if reasons:
        return "review", reasons
    return "allow", []
