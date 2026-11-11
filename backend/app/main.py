"""SecurePay API. Simulation only: no real cards, no card network, no real money."""

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.database import DATABASE_URL, SessionLocal, engine, get_db, utcnow
from backend.app.limiter import limiter
from backend.app.models import AuditLog, Base, Merchant, User
from backend.app.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    AuditOut,
    DisputeCreate,
    DisputeOut,
    DisputeResolve,
    LoginIn,
    LoginOut,
    MeOut,
    OutboxDeliverOut,
    OutboxOut,
    OverviewOut,
    PaymentCreate,
    PaymentOut,
    PayoutCreate,
    PayoutListOut,
    PayoutOut,
    RefundIn,
)
from backend.app.security import (
    create_token,
    get_current_user,
    get_user_or_api_key,
    hash_password,
    require_admin,
    verify_password,
)
from backend.app.seed import seed
from backend.app import service
from backend.app.schemas import LedgerOut

_IDEMPOTENCY = re.compile(r"[A-Za-z0-9_.:-]{8,200}")


def _clean_idempotency_key(idempotency_key: str | None) -> str:
    key = (idempotency_key or "").strip()
    if not _IDEMPOTENCY.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Idempotency-Key header is required and must be 8-200 characters "
                "(letters, numbers, dot, underscore, colon, or hyphen)"
            ),
        )
    return key


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if DATABASE_URL.startswith("sqlite"):
        Path("data").mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed(db)
    yield


app = FastAPI(
    title="SecurePay",
    version="0.1.0",
    summary="Simulated payment processing and risk. No real cards. No real money.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174", "http://127.0.0.1:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Idempotent-Replay", "X-SecurePay-Mode"],
)


@app.middleware("http")
async def mark_simulation(request, call_next):
    response = await call_next(request)
    response.headers["X-SecurePay-Mode"] = "simulation"
    return response


@app.get("/")
def root() -> dict:
    return {
        "service": "SecurePay",
        "mode": "simulation",
        "docs": "/docs",
        "console": "http://localhost:5174",
        "note": "This API simulates payment processing. It does not accept real cards or move real money.",
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "mode": "simulation", "service": "securepay"}


@app.post("/api/auth/login", response_model=LoginOut)
def login(body: LoginIn, db: Annotated[Session, Depends(get_db)]) -> LoginOut:
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    # Hash a stand-in when the user is missing so the failure path still runs pbkdf2.
    stored = user.password_hash if user is not None else hash_password(body.password)
    password_ok = verify_password(body.password, stored) if user is not None else False
    if user is None or not password_ok:
        db.add(
            AuditLog(
                actor=email,
                action="auth.login_failed",
                entity="user",
                detail="Rejected login",
                ts=utcnow(),
            )
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    db.add(
        AuditLog(
            actor=user.email,
            action="auth.login",
            entity=f"user:{user.id}",
            detail=f"Signed in as {user.role}",
            ts=utcnow(),
        )
    )
    db.commit()
    return LoginOut(access_token=create_token(user), email=user.email, role=user.role)


@app.get("/api/me", response_model=MeOut)
def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> MeOut:
    merchant = db.scalar(select(Merchant).where(Merchant.user_id == user.id))
    return MeOut(
        id=user.id,
        email=user.email,
        role=user.role,
        merchant_id=merchant.id if merchant else None,
        merchant_name=merchant.name if merchant else None,
    )


@app.post("/api/payments", response_model=PaymentOut)
def create_payment_route(
    body: PaymentCreate,
    response: Response,
    user: Annotated[User, Depends(get_user_or_api_key)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PaymentOut:
    allowed, retry_after = limiter.check(str(user.id))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Too many payment requests. Limit is 30 per minute for this user.",
                "limit": "30/minute",
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )
    key = _clean_idempotency_key(idempotency_key)
    # payment_token is validated on the body and intentionally discarded.
    payment, replay = service.create_payment(
        user_id=user.id,
        amount_cents=body.amount_cents,
        currency=body.currency,
        customer_ref=body.customer_ref,
        idempotency_key=key,
        merchant_id=body.merchant_id,
    )
    if replay:
        response.headers["X-Idempotent-Replay"] = "true"
        response.status_code = 200
    else:
        response.status_code = 201
    return payment


@app.post("/api/payments/{payment_id}/capture", response_model=PaymentOut)
def capture_route(
    payment_id: int,
    user: Annotated[User, Depends(get_current_user)],
) -> PaymentOut:
    return service.capture_payment(user.id, payment_id)


@app.post("/api/payments/{payment_id}/refund", response_model=PaymentOut)
def refund_route(
    payment_id: int,
    user: Annotated[User, Depends(get_current_user)],
    body: RefundIn | None = None,
) -> PaymentOut:
    amount = None if body is None else body.amount_cents
    return service.refund_payment(user.id, payment_id, amount)


@app.get("/api/payments", response_model=list[PaymentOut])
def list_payments_route(user: Annotated[User, Depends(get_current_user)]) -> list[PaymentOut]:
    return service.list_payments(user.id)


@app.get("/api/ledger", response_model=LedgerOut)
def ledger_route(user: Annotated[User, Depends(get_current_user)]) -> LedgerOut:
    return service.ledger_view(user.id)


@app.get("/api/overview", response_model=OverviewOut)
def overview_route(user: Annotated[User, Depends(get_current_user)]) -> OverviewOut:
    return service.overview(user.id)


@app.get("/api/audit", response_model=list[AuditOut])
def audit_route(_admin: Annotated[User, Depends(require_admin)]) -> list[AuditOut]:
    return service.list_audit()


@app.post("/api/payments/{payment_id}/disputes", response_model=DisputeOut, status_code=201)
def open_dispute_route(
    payment_id: int,
    body: DisputeCreate,
    user: Annotated[User, Depends(get_current_user)],
) -> DisputeOut:
    return service.open_dispute(user.id, payment_id, body.reason, body.amount_cents)


@app.get("/api/disputes", response_model=list[DisputeOut])
def list_disputes_route(user: Annotated[User, Depends(get_current_user)]) -> list[DisputeOut]:
    return service.list_disputes(user.id)


@app.post("/api/disputes/{dispute_id}/resolve", response_model=DisputeOut)
def resolve_dispute_route(
    dispute_id: int,
    body: DisputeResolve,
    admin: Annotated[User, Depends(require_admin)],
) -> DisputeOut:
    return service.resolve_dispute(admin.id, dispute_id, body.outcome)


@app.post("/api/payouts", response_model=PayoutOut)
def create_payout_route(
    body: PayoutCreate,
    response: Response,
    admin: Annotated[User, Depends(require_admin)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PayoutOut:
    key = _clean_idempotency_key(idempotency_key)
    payout, replay = service.create_payout(
        user_id=admin.id,
        merchant_id=body.merchant_id,
        amount_cents=body.amount_cents,
        currency=body.currency,
        idempotency_key=key,
    )
    if replay:
        response.headers["X-Idempotent-Replay"] = "true"
        response.status_code = 200
    else:
        response.status_code = 201
    return payout


@app.get("/api/payouts", response_model=PayoutListOut)
def list_payouts_route(user: Annotated[User, Depends(get_current_user)]) -> PayoutListOut:
    return service.list_payouts(user.id)


@app.post("/api/api-keys", response_model=ApiKeyCreated, status_code=201)
def create_api_key_route(
    body: ApiKeyCreate,
    user: Annotated[User, Depends(get_current_user)],
) -> ApiKeyCreated:
    return service.create_api_key(user.id, body.name, body.merchant_id)


@app.get("/api/api-keys", response_model=list[ApiKeyOut])
def list_api_keys_route(user: Annotated[User, Depends(get_current_user)]) -> list[ApiKeyOut]:
    return service.list_api_keys(user.id)


@app.post("/api/api-keys/{key_id}/revoke", response_model=ApiKeyOut)
def revoke_api_key_route(
    key_id: int,
    user: Annotated[User, Depends(get_current_user)],
) -> ApiKeyOut:
    return service.revoke_api_key(user.id, key_id)


@app.get("/api/outbox", response_model=list[OutboxOut])
def list_outbox_route(user: Annotated[User, Depends(get_current_user)]) -> list[OutboxOut]:
    return service.list_outbox(user.id)


@app.post("/api/outbox/deliver", response_model=OutboxDeliverOut)
def deliver_outbox_route(user: Annotated[User, Depends(get_current_user)]) -> OutboxDeliverOut:
    return service.deliver_outbox(user.id)
