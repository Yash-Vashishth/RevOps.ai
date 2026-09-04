"""
models.py
SQLAlchemy ORM models: Transaction, RecoveryAttempt, PromiseToPayLedger, AuditLog.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Float, Integer, DateTime, Text, ForeignKey, Boolean, Enum
)
from sqlalchemy.orm import relationship

from database import Base


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RailType(str, enum.Enum):
    CHECKOUT_DROPOFF = "checkout_dropoff_saver"
    MANDATE_RETRY = "smart_mandate_retry"
    B2B_NEGOTIATOR = "b2b_receivables_negotiator"


class TransactionStatus(str, enum.Enum):
    FAILED = "failed"
    RECOVERY_IN_PROGRESS = "recovery_in_progress"
    RECOVERED = "recovered"
    LOST = "lost"
    OVERDUE = "overdue"


class RecoveryChannel(str, enum.Enum):
    SMS = "SMS"
    WHATSAPP = "WhatsApp"
    EMAIL = "Email"


class AttemptStatus(str, enum.Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    RESPONDED = "responded"
    FAILED = "failed"
    BLOCKED_BY_GUARDRAIL = "blocked_by_guardrail"


class PromiseStatus(str, enum.Enum):
    PENDING = "PENDING"
    FULFILLED = "FULFILLED"
    BROKEN = "BROKEN"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=_uid)
    razorpay_payment_id = Column(String, nullable=True, index=True)
    razorpay_order_id = Column(String, nullable=True, index=True)
    # Stable key used to correlate repeated events against the same underlying
    # mandate/order/session across multiple webhook deliveries (e.g. a
    # subscription_id for mandate retries, where razorpay_payment_id changes
    # on every attempt but the mandate itself doesn't).
    dedupe_key = Column(String, nullable=True, index=True)
    amount = Column(Float, nullable=False)  # stored in INR (major unit), not paise
    status = Column(String, default=TransactionStatus.FAILED.value, index=True)
    failure_reason = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    rail_type = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=_now)

    recovery_attempts = relationship(
        "RecoveryAttempt", back_populates="transaction", cascade="all, delete-orphan"
    )
    promises = relationship(
        "PromiseToPayLedger", back_populates="transaction", cascade="all, delete-orphan"
    )
    audit_logs = relationship(
        "AuditLog", back_populates="transaction", cascade="all, delete-orphan"
    )


class RecoveryAttempt(Base):
    __tablename__ = "recovery_attempts"

    id = Column(String, primary_key=True, default=_uid)
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=False, index=True)
    channel = Column(String, nullable=False)  # SMS / WhatsApp / Email
    attempt_number = Column(Integer, nullable=False, default=1)
    status = Column(String, default=AttemptStatus.SENT.value)
    ai_action_taken = Column(Text, nullable=True)
    discount_applied_pct = Column(Float, default=0.0)
    created_at = Column(DateTime, default=_now)

    transaction = relationship("Transaction", back_populates="recovery_attempts")


class PromiseToPayLedger(Base):
    __tablename__ = "promise_to_pay_ledger"

    id = Column(String, primary_key=True, default=_uid)
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=False, index=True)
    customer_id = Column(String, nullable=True)  # email or phone used as identifier
    promised_date = Column(String, nullable=True)  # ISO date string, e.g. "2026-09-12"
    agreed_amount = Column(Float, nullable=True)
    status = Column(String, default=PromiseStatus.PENDING.value, index=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)

    transaction = relationship("Transaction", back_populates="promises")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(String, primary_key=True, default=_uid)
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=True, index=True)
    event_type = Column(String, nullable=False)
    guardrail_triggered = Column(Boolean, default=False)
    log_message = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=_now)

    transaction = relationship("Transaction", back_populates="audit_logs")
