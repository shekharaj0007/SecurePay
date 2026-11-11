from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.database import utcnow
from backend.app.models import AuditLog, LedgerAccount, Merchant, User
from backend.app.security import hash_password

ADMIN_EMAIL = "admin@securepay.test"
ADMIN_PASSWORD = "admin123"
MERCHANT_EMAIL = "merchant@securepay.test"
MERCHANT_PASSWORD = "merchant123"
MERCHANT_NAME = "Lumen Supply Co."
OPENING_BALANCE_CENTS = 1_000_000


def seed(db: Session) -> None:
    existing = db.scalar(select(User).where(User.email == ADMIN_EMAIL))
    if existing is not None:
        return

    admin = User(email=ADMIN_EMAIL, password_hash=hash_password(ADMIN_PASSWORD), role="admin")
    merchant_user = User(
        email=MERCHANT_EMAIL,
        password_hash=hash_password(MERCHANT_PASSWORD),
        role="merchant",
    )
    db.add_all([admin, merchant_user])
    db.flush()

    merchant = Merchant(name=MERCHANT_NAME, user_id=merchant_user.id)
    db.add(merchant)
    db.flush()

    db.add(
        LedgerAccount(
            merchant_id=merchant.id,
            currency="USD",
            balance_cents=OPENING_BALANCE_CENTS,
        )
    )
    db.add(
        AuditLog(
            actor="system",
            action="seed",
            entity=f"merchant:{merchant.id}",
            detail=(
                f"Seeded {MERCHANT_NAME} with {OPENING_BALANCE_CENTS} cents available. "
                "Simulation only."
            ),
            ts=utcnow(),
        )
    )
    db.commit()
