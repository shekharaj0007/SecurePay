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
from backend.app.models import (
    ApiKey,
    AuditLog,
    Dispute,
    LedgerAccount,
    LedgerEntry,
    Merchant,
    OutboxEvent,
    Payment,
    Payout,
    SettlementAccount,
    User,
)
from backend.app.risk import assess_risk
from backend.app.schemas import (
    ApiKeyCreated,
    ApiKeyOut,
    AuditOut,
    DisputeOut,
    LedgerAccountOut,
    LedgerEntryOut,
    LedgerOut,
    MerchantBalance,
    OutboxDeliverOut,
    OutboxOut,
    OverviewOut,
    PaymentOut,
    PayoutListOut,
    PayoutOut,
    SettlementAccountOut,
)
from backend.app.security import generate_api_key, hash_api_key


def iso(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit(db: Session, actor: str, action: str, entity: str, detail: str) -> None:
    db.add(AuditLog(actor=actor, action=action, entity=entity, detail=detail, ts=utcnow()))


def _enqueue(db: Session, merchant_id: int, event_type: str, payload: dict) -> None:
    db.add(
        OutboxEvent(
            merchant_id=merchant_id,
            event_type=event_type,
            payload=payload,
            status="pending",
            created_at=utcnow(),
            sent_at=None,
        )
    )


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
                    _enqueue(
                        db,
                        payment.merchant_id,
                        "payment.captured",
                        {
                            "event": "payment.captured",
                            "payment_id": payment.id,
                            "merchant_id": payment.merchant_id,
                            "amount_cents": payment.amount_cents,
                            "currency": payment.currency,
                            "customer_ref": payment.customer_ref,
                            "mode": "simulation",
                        },
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


def _apply_refund(db: Session, actor: str, payment: Payment, amount: int) -> None:
    """Debit available balance for a refund inside the caller's transaction."""
    if payment.status != "captured":
        if payment.status == "refunded":
            raise HTTPException(status_code=409, detail="Payment is already refunded")
        raise HTTPException(status_code=409, detail="Only captured payments can be refunded")
    remaining = payment.amount_cents - payment.refunded_cents
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
        actor,
        "payment.refunded",
        f"payment:{payment.id}",
        f"Debited {amount} {payment.currency} cents from available balance",
    )


def refund_payment(user_id: int, payment_id: int, amount_cents: int | None) -> PaymentOut:
    with SessionLocal() as db:
        with db.begin():
            user = _require_user(db, user_id)
            payment = _load_payment(db, user, payment_id)
            remaining = payment.amount_cents - payment.refunded_cents
            amount = remaining if amount_cents is None else amount_cents
            _apply_refund(db, user.email, payment, amount)
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
            combined = [
                LedgerEntryOut(
                    id=entry.id,
                    payment_id=entry.payment_id,
                    direction=entry.direction,
                    amount_cents=entry.amount_cents,
                    created_at=iso(entry.created_at),
                    source="payment",
                )
                for entry in entries
            ]
            combined.extend(
                _payout_entry_outs(db, account.merchant_id, account.currency, "debit")
            )
            combined.sort(key=lambda item: item.created_at, reverse=True)
            accounts.append(
                LedgerAccountOut(
                    id=account.id,
                    merchant_id=account.merchant_id,
                    merchant_name=name,
                    currency=account.currency,
                    balance_cents=account.balance_cents,
                    kind="available",
                    entries=combined[:100],
                )
            )
        settle_stmt = (
            select(SettlementAccount, Merchant.name)
            .join(Merchant, Merchant.id == SettlementAccount.merchant_id)
            .order_by(SettlementAccount.id)
        )
        if ids is not None:
            settle_stmt = settle_stmt.where(SettlementAccount.merchant_id.in_(ids))
        for account, name in db.execute(settle_stmt).all():
            accounts.append(
                LedgerAccountOut(
                    id=account.id,
                    merchant_id=account.merchant_id,
                    merchant_name=name,
                    currency=account.currency,
                    balance_cents=account.balance_cents,
                    kind="settlement",
                    entries=_payout_entry_outs(db, account.merchant_id, account.currency, "credit"),
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


def _payout_entry_outs(
    db: Session,
    merchant_id: int,
    currency: str,
    direction: str,
) -> list[LedgerEntryOut]:
    payouts = db.scalars(
        select(Payout)
        .where(Payout.merchant_id == merchant_id, Payout.currency == currency)
        .order_by(Payout.id.desc())
        .limit(100)
    ).all()
    return [
        LedgerEntryOut(
            id=payout.id,
            payment_id=None,
            payout_id=payout.id,
            direction=direction,
            amount_cents=payout.amount_cents,
            created_at=iso(payout.created_at),
            source="payout",
        )
        for payout in payouts
    ]


def _dispute_out(db: Session, dispute: Dispute) -> DisputeOut:
    payment = db.get(Payment, dispute.payment_id)
    merchant_name = db.scalar(select(Merchant.name).where(Merchant.id == dispute.merchant_id)) or ""
    return DisputeOut(
        id=dispute.id,
        payment_id=dispute.payment_id,
        merchant_id=dispute.merchant_id,
        merchant_name=merchant_name,
        amount_cents=dispute.amount_cents,
        currency=payment.currency if payment is not None else "USD",
        reason=dispute.reason,
        status=dispute.status,
        opened_by=dispute.opened_by,
        created_at=iso(dispute.created_at),
        resolved_at=iso(dispute.resolved_at) if dispute.resolved_at is not None else None,
    )


def open_dispute(user_id: int, payment_id: int, reason: str, amount_cents: int) -> DisputeOut:
    with SessionLocal() as db:
        with db.begin():
            user = _require_user(db, user_id)
            payment = _load_payment(db, user, payment_id)
            if payment.status != "captured":
                raise HTTPException(
                    status_code=409,
                    detail="Disputes can only be opened on captured payments",
                )
            if amount_cents > payment.amount_cents:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Dispute amount of {amount_cents} cents is above the captured amount "
                        f"of {payment.amount_cents} cents"
                    ),
                )
            open_sum = int(
                db.scalar(
                    select(func.coalesce(func.sum(Dispute.amount_cents), 0)).where(
                        Dispute.payment_id == payment.id,
                        Dispute.status == "open",
                    )
                )
                or 0
            )
            remaining = payment.amount_cents - payment.refunded_cents - open_sum
            if amount_cents > remaining:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Dispute amount of {amount_cents} cents exceeds the remaining captured "
                        f"amount of {remaining} cents"
                    ),
                )
            dispute = Dispute(
                payment_id=payment.id,
                merchant_id=payment.merchant_id,
                amount_cents=amount_cents,
                reason=reason,
                status="open",
                opened_by=user.email,
                created_at=utcnow(),
                resolved_at=None,
            )
            db.add(dispute)
            db.flush()
            _audit(
                db,
                user.email,
                "dispute.opened",
                f"dispute:{dispute.id}",
                (
                    f"Opened dispute on payment {payment.id} for {amount_cents} "
                    f"{payment.currency} cents. {reason}"
                ),
            )
            snapshot = _dispute_out(db, dispute)
        return snapshot


def resolve_dispute(user_id: int, dispute_id: int, outcome: str) -> DisputeOut:
    with SessionLocal() as db:
        with db.begin():
            user = _require_user(db, user_id)
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="Admin role required")
            dispute = db.get(Dispute, dispute_id)
            if dispute is None:
                raise HTTPException(status_code=404, detail="Dispute not found")
            if dispute.status != "open":
                raise HTTPException(status_code=409, detail=f"Dispute is already {dispute.status}")
            payment = db.get(Payment, dispute.payment_id)
            if payment is None:
                raise HTTPException(status_code=404, detail="Payment not found")
            if outcome == "won":
                dispute.status = "won"
                dispute.resolved_at = utcnow()
                _audit(
                    db,
                    user.email,
                    "dispute.won",
                    f"dispute:{dispute.id}",
                    (
                        f"Dispute on payment {payment.id} marked won. "
                        f"{dispute.amount_cents} cents stayed on the available balance."
                    ),
                )
            elif outcome == "lost":
                _apply_refund(db, user.email, payment, dispute.amount_cents)
                dispute.status = "lost"
                dispute.resolved_at = utcnow()
                _audit(
                    db,
                    user.email,
                    "dispute.lost",
                    f"dispute:{dispute.id}",
                    (
                        f"Dispute on payment {payment.id} marked lost. "
                        f"Refunded {dispute.amount_cents} {payment.currency} cents."
                    ),
                )
                _enqueue(
                    db,
                    payment.merchant_id,
                    "dispute.lost",
                    {
                        "event": "dispute.lost",
                        "dispute_id": dispute.id,
                        "payment_id": payment.id,
                        "merchant_id": payment.merchant_id,
                        "amount_cents": dispute.amount_cents,
                        "currency": payment.currency,
                        "mode": "simulation",
                    },
                )
            else:
                raise HTTPException(status_code=422, detail="Outcome must be won or lost")
            snapshot = _dispute_out(db, dispute)
        return snapshot


def list_disputes(user_id: int) -> list[DisputeOut]:
    with SessionLocal() as db:
        user = _require_user(db, user_id)
        ids = _visible_merchant_ids(db, user)
        if ids is not None and not ids:
            return []
        stmt = select(Dispute).order_by(Dispute.id.desc()).limit(200)
        if ids is not None:
            stmt = stmt.where(Dispute.merchant_id.in_(ids))
        return [_dispute_out(db, dispute) for dispute in db.scalars(stmt).all()]


def _settlement_account(db: Session, merchant_id: int, currency: str) -> SettlementAccount:
    account = db.scalar(
        select(SettlementAccount).where(
            SettlementAccount.merchant_id == merchant_id,
            SettlementAccount.currency == currency,
        )
    )
    if account is None:
        account = SettlementAccount(merchant_id=merchant_id, currency=currency, balance_cents=0)
        db.add(account)
        db.flush()
    return account


def _payout_out(db: Session, payout: Payout) -> PayoutOut:
    merchant_name = db.scalar(select(Merchant.name).where(Merchant.id == payout.merchant_id)) or ""
    available = db.scalar(
        select(LedgerAccount).where(
            LedgerAccount.merchant_id == payout.merchant_id,
            LedgerAccount.currency == payout.currency,
        )
    )
    settlement = db.scalar(
        select(SettlementAccount).where(
            SettlementAccount.merchant_id == payout.merchant_id,
            SettlementAccount.currency == payout.currency,
        )
    )
    return PayoutOut(
        id=payout.id,
        merchant_id=payout.merchant_id,
        merchant_name=merchant_name,
        amount_cents=payout.amount_cents,
        currency=payout.currency,
        status=payout.status,
        idempotency_key=payout.idempotency_key,
        available_balance_cents=available.balance_cents if available is not None else 0,
        settlement_balance_cents=settlement.balance_cents if settlement is not None else 0,
        created_at=iso(payout.created_at),
    )


def _same_payout(payout: Payout, amount_cents: int, currency: str) -> bool:
    return payout.amount_cents == amount_cents and payout.currency == currency


def create_payout(
    *,
    user_id: int,
    merchant_id: int,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
) -> tuple[PayoutOut, bool]:
    with SessionLocal() as db:
        try:
            with db.begin():
                user = _require_user(db, user_id)
                if user.role != "admin":
                    raise HTTPException(status_code=403, detail="Admin role required")
                merchant = db.get(Merchant, merchant_id)
                if merchant is None:
                    raise HTTPException(status_code=404, detail="Merchant not found")
                existing = db.scalar(
                    select(Payout).where(
                        Payout.merchant_id == merchant.id,
                        Payout.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if not _same_payout(existing, amount_cents, currency):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Idempotency-Key was already used with a different amount or currency"
                            ),
                        )
                    return _payout_out(db, existing), True
                available = _account_for(db, merchant.id, currency)
                if amount_cents > available.balance_cents:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Payout exceeds the available balance and would drive it negative. "
                            f"Available is {available.balance_cents} cents."
                        ),
                    )
                settlement = _settlement_account(db, merchant.id, currency)
                available.balance_cents = available.balance_cents - amount_cents
                settlement.balance_cents = settlement.balance_cents + amount_cents
                payout = Payout(
                    merchant_id=merchant.id,
                    idempotency_key=idempotency_key,
                    amount_cents=amount_cents,
                    currency=currency,
                    status="paid",
                    created_at=utcnow(),
                )
                db.add(payout)
                db.flush()
                _audit(
                    db,
                    user.email,
                    "payout.paid",
                    f"payout:{payout.id}",
                    (
                        f"Moved {amount_cents} {currency} cents from available balance "
                        f"to settlement for {merchant.name}. Status paid. Simulated. No bank was contacted."
                    ),
                )
                return _payout_out(db, payout), False
        except IntegrityError as exc:
            existing = db.scalar(
                select(Payout).where(
                    Payout.merchant_id == merchant_id,
                    Payout.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                message = str(getattr(exc, "orig", exc)).lower()
                if "ck_balance_nonneg" in message or "balance" in message:
                    raise HTTPException(
                        status_code=409,
                        detail="Payout exceeds the available balance and would drive it negative.",
                    ) from exc
                raise
            if not _same_payout(existing, amount_cents, currency):
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different amount or currency",
                )
            return _payout_out(db, existing), True


def list_payouts(user_id: int) -> PayoutListOut:
    with SessionLocal() as db:
        user = _require_user(db, user_id)
        ids = _visible_merchant_ids(db, user)
        if ids is not None and not ids:
            return PayoutListOut(payouts=[], settlement_accounts=[])
        payout_stmt = select(Payout).order_by(Payout.id.desc()).limit(200)
        account_stmt = (
            select(SettlementAccount, Merchant.name)
            .join(Merchant, Merchant.id == SettlementAccount.merchant_id)
            .order_by(SettlementAccount.id)
        )
        if ids is not None:
            payout_stmt = payout_stmt.where(Payout.merchant_id.in_(ids))
            account_stmt = account_stmt.where(SettlementAccount.merchant_id.in_(ids))
        payouts = [_payout_out(db, payout) for payout in db.scalars(payout_stmt).all()]
        accounts = [
            SettlementAccountOut(
                id=account.id,
                merchant_id=account.merchant_id,
                merchant_name=name,
                currency=account.currency,
                balance_cents=account.balance_cents,
            )
            for account, name in db.execute(account_stmt).all()
        ]
        return PayoutListOut(payouts=payouts, settlement_accounts=accounts)


def _api_key_out(db: Session, row: ApiKey) -> ApiKeyOut:
    merchant_name = db.scalar(select(Merchant.name).where(Merchant.id == row.merchant_id)) or ""
    return ApiKeyOut(
        id=row.id,
        merchant_id=row.merchant_id,
        merchant_name=merchant_name,
        name=row.name,
        prefix=row.prefix,
        created_at=iso(row.created_at),
        revoked_at=iso(row.revoked_at) if row.revoked_at is not None else None,
    )


def create_api_key(user_id: int, name: str, merchant_id: int | None) -> ApiKeyCreated:
    with SessionLocal() as db:
        with db.begin():
            user = _require_user(db, user_id)
            merchant = _resolve_merchant(db, user, merchant_id)
            secret = generate_api_key()
            prefix = secret[:16]
            row = ApiKey(
                merchant_id=merchant.id,
                name=name,
                prefix=prefix,
                key_hash=hash_api_key(secret),
                created_at=utcnow(),
                revoked_at=None,
            )
            db.add(row)
            db.flush()
            _audit(
                db,
                user.email,
                "api_key.created",
                f"api_key:{row.id}",
                f"Created key {prefix}… for {merchant.name}. Only the hash is stored.",
            )
            created = ApiKeyCreated(
                id=row.id,
                merchant_id=merchant.id,
                merchant_name=merchant.name,
                name=row.name,
                prefix=prefix,
                secret=secret,
                created_at=iso(row.created_at),
                revoked_at=None,
            )
        return created


def list_api_keys(user_id: int) -> list[ApiKeyOut]:
    with SessionLocal() as db:
        user = _require_user(db, user_id)
        ids = _visible_merchant_ids(db, user)
        if ids is not None and not ids:
            return []
        stmt = select(ApiKey).order_by(ApiKey.id.desc()).limit(200)
        if ids is not None:
            stmt = stmt.where(ApiKey.merchant_id.in_(ids))
        return [_api_key_out(db, row) for row in db.scalars(stmt).all()]


def revoke_api_key(user_id: int, key_id: int) -> ApiKeyOut:
    with SessionLocal() as db:
        with db.begin():
            user = _require_user(db, user_id)
            row = db.get(ApiKey, key_id)
            if row is None:
                raise HTTPException(status_code=404, detail="API key not found")
            if user.role != "admin":
                merchant = _merchant_for_user(db, user)
                if merchant is None or row.merchant_id != merchant.id:
                    raise HTTPException(status_code=404, detail="API key not found")
            if row.revoked_at is None:
                row.revoked_at = utcnow()
                _audit(
                    db,
                    user.email,
                    "api_key.revoked",
                    f"api_key:{row.id}",
                    f"Revoked key {row.prefix}… It can no longer create payments.",
                )
            snapshot = _api_key_out(db, row)
        return snapshot


def _outbox_out(row: OutboxEvent) -> OutboxOut:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return OutboxOut(
        id=row.id,
        merchant_id=row.merchant_id,
        event_type=row.event_type,
        payload=payload,
        status=row.status,
        created_at=iso(row.created_at),
        sent_at=iso(row.sent_at) if row.sent_at is not None else None,
    )


def list_outbox(user_id: int) -> list[OutboxOut]:
    with SessionLocal() as db:
        user = _require_user(db, user_id)
        ids = _visible_merchant_ids(db, user)
        if ids is not None and not ids:
            return []
        stmt = select(OutboxEvent).order_by(OutboxEvent.id.desc()).limit(200)
        if ids is not None:
            stmt = stmt.where(OutboxEvent.merchant_id.in_(ids))
        return [_outbox_out(row) for row in db.scalars(stmt).all()]


def deliver_outbox(user_id: int) -> OutboxDeliverOut:
    """Mark pending outbox rows sent. This does not call the internet."""
    with SessionLocal() as db:
        with db.begin():
            user = _require_user(db, user_id)
            ids = _visible_merchant_ids(db, user)
            if ids is not None and not ids:
                return OutboxDeliverOut(delivered=0, events=[])
            stmt = (
                select(OutboxEvent)
                .where(OutboxEvent.status == "pending")
                .order_by(OutboxEvent.id)
            )
            if ids is not None:
                stmt = stmt.where(OutboxEvent.merchant_id.in_(ids))
            rows = list(db.scalars(stmt).all())
            now = utcnow()
            for row in rows:
                row.status = "sent"
                row.sent_at = now
            if rows:
                _audit(
                    db,
                    user.email,
                    "outbox.delivered",
                    "outbox",
                    f"Marked {len(rows)} pending outbox rows sent. No network call was made.",
                )
            events = [_outbox_out(row) for row in rows]
        return OutboxDeliverOut(delivered=len(events), events=events)
