"""Payment, ledger, and read models.

Capture and refund each change the payment and the ledger account inside one
SQLAlchemy transaction. A replayed idempotency key returns the original payment
and does not post another ledger entry.
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.database import SessionLocal, utcnow
from backend.app.models import AuditLog, LedgerAccount, LedgerEntry, Merchant, Payment, User
from backend.app.risk import assess_risk
from backend.app.schemas import (
    AuditOut,
    LedgerAccountOut,
    LedgerEntryOut,
    LedgerOut,
    MerchantBalance,
    OverviewOut,
    PaymentOut,
)


def iso(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit(db: Session, actor: str, action: str, entity: str, detail: str) -> None:
    db.add(AuditLog(actor=actor, action=action, entity=entity, detail=detail, ts=utcnow()))


def _out(db: Session, payment: Payment) -> PaymentOut:
    name = db.scalar(select(Merchant.name).where(Merchant.id == payment.merchant_id)) or ""
    reasons = payment.risk_reasons or []
    if not isinstance(reasons, list):
        reasons = []
    return PaymentOut(
        id=payment.id,
        merchant_id=payment.merchant_id,
        merchant_name=name,
        idempotency_key=payment.idempotency_key,
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        status=payment.status,
        risk_decision=payment.risk_decision,
        risk_reasons=[str(item) for item in reasons],
        customer_ref=payment.customer_ref,
        refunded_cents=payment.refunded_cents,
        created_at=iso(payment.created_at),
    )


def _same_request(payment: Payment, amount_cents: int, currency: str, customer_ref: str) -> bool:
    return (
        payment.amount_cents == amount_cents
        and payment.currency == currency
        and payment.customer_ref == customer_ref
    )


def _merchant_for_user(db: Session, user: User) -> Merchant | None:
    return db.scalar(select(Merchant).where(Merchant.user_id == user.id))


def _resolve_merchant(db: Session, user: User, merchant_id: int | None) -> Merchant:
    if user.role == "merchant":
        merchant = _merchant_for_user(db, user)
        if merchant is None:
            raise HTTPException(status_code=403, detail="No merchant profile is linked to this user")
        if merchant_id is not None and merchant_id != merchant.id:
            raise HTTPException(status_code=403, detail="Merchants can only create payments for their own account")
        return merchant
    if merchant_id is None:
        raise HTTPException(status_code=400, detail="Admin payment requests must include merchant_id")
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found")
    return merchant


def _load_payment(db: Session, user: User, payment_id: int) -> Payment:
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    if user.role != "admin":
        merchant = _merchant_for_user(db, user)
        if merchant is None or payment.merchant_id != merchant.id:
            raise HTTPException(status_code=404, detail="Payment not found")
    return payment


def _account_for(db: Session, merchant_id: int, currency: str) -> LedgerAccount:
    account = db.scalar(
        select(LedgerAccount).where(
            LedgerAccount.merchant_id == merchant_id,
            LedgerAccount.currency == currency,
        )
    )
    if account is None:
        raise HTTPException(status_code=400, detail=f"No {currency} ledger account for this merchant")
    return account


def _require_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


def create_payment(
    *,
    user_id: int,
    amount_cents: int,
    currency: str,
    customer_ref: str,
    idempotency_key: str,
    merchant_id: int | None,
) -> tuple[PaymentOut, bool]:
    merchant_pk: int | None = None
    with SessionLocal() as db:
        try:
            with db.begin():
                user = _require_user(db, user_id)
                merchant = _resolve_merchant(db, user, merchant_id)
                merchant_pk = merchant.id
                existing = db.scalar(
                    select(Payment).where(
                        Payment.merchant_id == merchant.id,
                        Payment.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if not _same_request(existing, amount_cents, currency, customer_ref):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Idempotency-Key was already used with a different amount, "
                                "currency, or customer_ref"
                            ),
                        )
                    return _out(db, existing), True

                decision, reasons = assess_risk(db, merchant.id, amount_cents, customer_ref)
                if decision == "block":
                    status = "failed"
                elif decision == "review":
                    status = "created"
                else:
                    status = "authorized"
                payment = Payment(
                    merchant_id=merchant.id,
                    idempotency_key=idempotency_key,
                    amount_cents=amount_cents,
                    currency=currency,
                    status=status,
                    risk_decision=decision,
                    risk_reasons=reasons,
                    customer_ref=customer_ref,
                    refunded_cents=0,
                    created_at=utcnow(),
                )
                db.add(payment)
                db.flush()
                action = "payment.blocked" if decision == "block" else "payment.created"
                _audit(
                    db,
                    user.email,
                    action,
                    f"payment:{payment.id}",
                    (
                        f"{status} {amount_cents} {currency} cents for {customer_ref}. "
                        f"Risk {decision}. Ledger was not moved."
                    ),
                )
                return _out(db, payment), False
        except IntegrityError:
            if merchant_pk is None:
                raise
            existing = db.scalar(
                select(Payment).where(
                    Payment.merchant_id == merchant_pk,
                    Payment.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                raise
            if not _same_request(existing, amount_cents, currency, customer_ref):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Idempotency-Key was already used with a different amount, "
                        "currency, or customer_ref"
                    ),
                )
            return _out(db, existing), True


def capture_payment(user_id: int, payment_id: int) -> PaymentOut:
    with SessionLocal() as db:
        outcome = "captured"
        with db.begin():
            user = _require_user(db, user_id)
            payment = _load_payment(db, user, payment_id)
            if payment.status == "captured":
                outcome = "already"
            elif payment.status == "refunded":
                raise HTTPException(status_code=409, detail="Refunded payments cannot be captured")
            elif payment.status == "failed":
                detail = (
                    "Blocked payments cannot be captured and do not move the ledger"
                    if payment.risk_decision == "block"
                    else "Failed payments cannot be captured"
                )
                raise HTTPException(status_code=409, detail=detail)
            else:
                decision, reasons = assess_risk(
                    db,
                    payment.merchant_id,
                    payment.amount_cents,
                    payment.customer_ref,
                )
                payment.risk_decision = decision
                payment.risk_reasons = reasons
                if decision == "block":
                    payment.status = "failed"
                    _audit(
                        db,
                        user.email,
                        "payment.blocked",
                        f"payment:{payment.id}",
                        "Risk block on capture. Ledger was not moved. Reasons: " + ", ".join(reasons),
                    )
                    outcome = "blocked"
                elif decision == "review" and user.role != "admin":
                    outcome = "review"
                else:
                    account = _account_for(db, payment.merchant_id, payment.currency)
                    account.balance_cents = account.balance_cents + payment.amount_cents
                    db.add(
                        LedgerEntry(
                            payment_id=payment.id,
                            account_id=account.id,
                            direction="credit",
                            amount_cents=payment.amount_cents,
                            created_at=utcnow(),
                        )
                    )
                    payment.status = "captured"
                    _audit(
                        db,
                        user.email,
                        "payment.captured",
                        f"payment:{payment.id}",
                        f"Credited {payment.amount_cents} {payment.currency} cents to available balance",
                    )
                    outcome = "captured"
            snapshot = _out(db, payment)

        if outcome == "blocked":
            raise HTTPException(
                status_code=409,
                detail="Payment blocked by risk rules. The ledger was not changed.",
            )
        if outcome == "review":
            raise HTTPException(
                status_code=409,
                detail="This payment is held for review. An admin must capture it. No funds were moved.",
            )
        return snapshot


def refund_payment(user_id: int, payment_id: int, amount_cents: int | None) -> PaymentOut:
    with SessionLocal() as db:
        with db.begin():
            user = _require_user(db, user_id)
            payment = _load_payment(db, user, payment_id)
            if payment.status != "captured":
                if payment.status == "refunded":
                    raise HTTPException(status_code=409, detail="Payment is already refunded")
                raise HTTPException(status_code=409, detail="Only captured payments can be refunded")
            remaining = payment.amount_cents - payment.refunded_cents
            amount = remaining if amount_cents is None else amount_cents
            if amount <= 0:
                raise HTTPException(status_code=422, detail="Refund amount must be positive")
            if amount > remaining:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Refund of {amount} cents exceeds the remaining captured amount "
                        f"of {remaining} cents"
                    ),
                )
            account = _account_for(db, payment.merchant_id, payment.currency)
            if account.balance_cents < amount:
                raise HTTPException(status_code=409, detail="Insufficient available balance to refund")
            account.balance_cents = account.balance_cents - amount
            payment.refunded_cents = payment.refunded_cents + amount
            if payment.refunded_cents == payment.amount_cents:
                payment.status = "refunded"
            db.add(
                LedgerEntry(
                    payment_id=payment.id,
                    account_id=account.id,
                    direction="debit",
                    amount_cents=amount,
                    created_at=utcnow(),
                )
            )
            _audit(
                db,
                user.email,
                "payment.refunded",
                f"payment:{payment.id}",
                f"Debited {amount} {payment.currency} cents from available balance",
            )
            snapshot = _out(db, payment)
        return snapshot


def _visible_merchant_ids(db: Session, user: User) -> list[int] | None:
    if user.role == "admin":
        return None
    merchant = _merchant_for_user(db, user)
    if merchant is None:
        return []
    return [merchant.id]


def list_payments(user_id: int) -> list[PaymentOut]:
    with SessionLocal() as db:
        user = _require_user(db, user_id)
        ids = _visible_merchant_ids(db, user)
        if ids is not None and not ids:
            return []
        stmt = (
            select(Payment, Merchant.name)
            .join(Merchant, Merchant.id == Payment.merchant_id)
            .order_by(Payment.id.desc())
            .limit(200)
        )
        if ids is not None:
            stmt = stmt.where(Payment.merchant_id.in_(ids))
        rows = db.execute(stmt).all()
        payments: list[PaymentOut] = []
        for payment, name in rows:
            item = _out(db, payment)
            item.merchant_name = name
            payments.append(item)
        return payments


def ledger_view(user_id: int) -> LedgerOut:
    with SessionLocal() as db:
        user = _require_user(db, user_id)
        ids = _visible_merchant_ids(db, user)
        if ids is not None and not ids:
            return LedgerOut(accounts=[])
        stmt = (
            select(LedgerAccount, Merchant.name)
            .join(Merchant, Merchant.id == LedgerAccount.merchant_id)
            .order_by(LedgerAccount.id)
        )
        if ids is not None:
            stmt = stmt.where(LedgerAccount.merchant_id.in_(ids))
        accounts: list[LedgerAccountOut] = []
        for account, name in db.execute(stmt).all():
            entries = db.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.account_id == account.id)
                .order_by(LedgerEntry.id.desc())
                .limit(100)
            ).all()
            accounts.append(
                LedgerAccountOut(
                    id=account.id,
                    merchant_id=account.merchant_id,
                    merchant_name=name,
                    currency=account.currency,
                    balance_cents=account.balance_cents,
                    entries=[
                        LedgerEntryOut(
                            id=entry.id,
                            payment_id=entry.payment_id,
                            direction=entry.direction,
                            amount_cents=entry.amount_cents,
                            created_at=iso(entry.created_at),
                        )
                        for entry in entries
                    ],
                )
            )
        return LedgerOut(accounts=accounts)


def overview(user_id: int) -> OverviewOut:
    with SessionLocal() as db:
        user = _require_user(db, user_id)
        ids = _visible_merchant_ids(db, user)
        if ids is not None and not ids:
            return OverviewOut(
                payment_count=0,
                volume_cents=0,
                blocked_count=0,
                review_count=0,
                balance_cents=0,
                currency="USD",
                merchants=[],
            )

        def scoped_payments(stmt):
            if ids is not None:
                return stmt.where(Payment.merchant_id.in_(ids))
            return stmt

        payment_count = int(
            db.scalar(scoped_payments(select(func.count(Payment.id)))) or 0
        )
        blocked_count = int(
            db.scalar(
                scoped_payments(
                    select(func.count(Payment.id)).where(Payment.risk_decision == "block")
                )
            )
            or 0
        )
        review_count = int(
            db.scalar(
                scoped_payments(
                    select(func.count(Payment.id)).where(
                        Payment.risk_decision == "review",
                        Payment.status.in_(("created", "authorized")),
                    )
                )
            )
            or 0
        )

        volume_stmt = (
            select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
            .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
            .where(LedgerEntry.direction == "credit")
        )
        balance_stmt = select(func.coalesce(func.sum(LedgerAccount.balance_cents), 0))
        merchant_stmt = (
            select(Merchant, LedgerAccount)
            .join(LedgerAccount, LedgerAccount.merchant_id == Merchant.id)
            .order_by(Merchant.id)
        )
        if ids is not None:
            volume_stmt = volume_stmt.where(LedgerAccount.merchant_id.in_(ids))
            balance_stmt = balance_stmt.where(LedgerAccount.merchant_id.in_(ids))
            merchant_stmt = merchant_stmt.where(Merchant.id.in_(ids))

        volume_cents = int(db.scalar(volume_stmt) or 0)
        balance_cents = int(db.scalar(balance_stmt) or 0)
        merchants = [
            MerchantBalance(
                id=merchant.id,
                name=merchant.name,
                currency=account.currency,
                balance_cents=account.balance_cents,
            )
            for merchant, account in db.execute(merchant_stmt).all()
        ]
        currency = merchants[0].currency if merchants else "USD"
        return OverviewOut(
            payment_count=payment_count,
            volume_cents=volume_cents,
            blocked_count=blocked_count,
            review_count=review_count,
            balance_cents=balance_cents,
            currency=currency,
            merchants=merchants,
        )


def list_audit(limit: int = 300) -> list[AuditOut]:
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
        return [
            AuditOut(
                id=row.id,
                actor=row.actor,
                action=row.action,
                entity=row.entity,
                detail=row.detail,
                ts=iso(row.ts),
            )
            for row in rows
        ]
