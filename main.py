"""
main.py
FastAPI application exposing:
  POST /api/v1/webhooks/razorpay   - signed webhook ingestion & rail routing
  POST /api/v1/negotiate/{txn_id}  - drive the B2B Hinglish negotiator
  GET  /api/v1/metrics             - aggregate recovery metrics for the dashboard
  GET  /api/v1/transactions        - list transactions
  GET  /api/v1/promises            - list promise-to-pay ledger rows
  GET  /api/v1/audit-log           - list audit log rows
"""
import logging

from fastapi import FastAPI, Request, Depends, HTTPException, Header
from sqlalchemy.orm import Session

import models
import diagnostics
import guardrails
import agent
from database import init_db, get_db
from razorpay_service import razorpay_service
from config import settings

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger("revops.main")

app = FastAPI(title="RevOps.ai — AI Revenue Recovery Mesh", version="1.0.0")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    logger.info("RevOps.ai backend started. Environment: %s", settings.APP_ENV)


# ------------------------------------------------------------------------ #
# Webhook ingestion
# ------------------------------------------------------------------------ #
@app.post("/api/v1/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_razorpay_signature: str | None = Header(default=None),
):
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")

    if x_razorpay_signature:
        valid = razorpay_service.verify_webhook_signature(body_str, x_razorpay_signature)
        if not valid:
            _audit(db, None, "webhook_signature_invalid", "Rejected webhook: bad HMAC signature.", True)
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = await request.json()
    event = payload.get("event", "")
    diagnosis = diagnostics.triage_event(event, payload)

    if diagnosis is None:
        return {"status": "ignored", "event": event}

    txn = _get_or_create_transaction(db, diagnosis)

    if diagnosis["rail"] == models.RailType.CHECKOUT_DROPOFF.value:
        result = _handle_checkout_dropoff(db, txn)
    elif diagnosis["rail"] == models.RailType.MANDATE_RETRY.value:
        result = _handle_mandate_retry(db, txn, diagnosis)
    elif diagnosis["rail"] == models.RailType.B2B_NEGOTIATOR.value:
        result = _handle_b2b_overdue(db, txn, diagnosis)
    else:
        result = {"status": "unhandled_rail"}

    return {"status": "processed", "event": event, "rail": diagnosis["rail"], "result": result}


# ------------------------------------------------------------------------ #
# Rail handlers
# ------------------------------------------------------------------------ #
def _handle_checkout_dropoff(db: Session, txn: models.Transaction) -> dict:
    check = guardrails.check_checkout_link_allowed(db, txn)
    if not check.allowed:
        return {"status": "blocked", "reason": check.reason}

    link = razorpay_service.create_fallback_payment_link(
        amount=txn.amount,
        description=f"Complete your payment — Order {txn.razorpay_order_id or txn.id[:8]}",
        customer_email=txn.customer_email,
        customer_phone=txn.customer_phone,
        expiry_minutes=guardrails.CHECKOUT_LINK_EXPIRY_MINUTES,
    )
    attempt = models.RecoveryAttempt(
        transaction_id=txn.id,
        channel=models.RecoveryChannel.SMS.value if txn.customer_phone else models.RecoveryChannel.EMAIL.value,
        attempt_number=1,
        status=models.AttemptStatus.SENT.value if link.get("id") else models.AttemptStatus.FAILED.value,
        ai_action_taken=f"payment_link_created:{link.get('id')}",
        discount_applied_pct=0.0,
    )
    db.add(attempt)
    txn.status = models.TransactionStatus.RECOVERY_IN_PROGRESS.value
    db.add(txn)
    db.commit()

    _audit(db, txn.id, "checkout_link_sent", f"Fallback payment link {link.get('id')} created.")
    return {"status": "link_created", "short_url": link.get("short_url"), "link_id": link.get("id")}


def _handle_mandate_retry(db: Session, txn: models.Transaction, diagnosis: dict) -> dict:
    check = guardrails.check_mandate_retry_allowed(db, txn)
    if not check.allowed:
        return {"status": "blocked", "reason": check.reason}

    subscription_id = diagnosis.get("subscription_id") or f"sub_synthetic_{txn.id[:8]}"
    result = razorpay_service.trigger_mandate_retry(subscription_id)

    attempt = models.RecoveryAttempt(
        transaction_id=txn.id,
        channel=models.RecoveryChannel.SMS.value,
        attempt_number=guardrails.MANDATE_MAX_RETRIES,  # overwritten below with real count
        status=models.AttemptStatus.SENT.value if "error" not in result else models.AttemptStatus.FAILED.value,
        ai_action_taken=f"mandate_retry:{subscription_id}",
        discount_applied_pct=0.0,
    )
    # Correct the attempt_number to the real sequential count now that we've committed to retrying.
    attempt.attempt_number = (
        db.query(models.RecoveryAttempt)
        .filter(
            models.RecoveryAttempt.transaction_id == txn.id,
            models.RecoveryAttempt.ai_action_taken.like("%mandate_retry%"),
        )
        .count()
        + 1
    )
    db.add(attempt)
    txn.status = models.TransactionStatus.RECOVERY_IN_PROGRESS.value
    db.add(txn)
    db.commit()

    _audit(db, txn.id, "mandate_retry_triggered", f"Retry #{attempt.attempt_number} for {subscription_id}.")
    return {"status": "retry_triggered", "attempt_number": attempt.attempt_number, "result": result}


def _handle_b2b_overdue(db: Session, txn: models.Transaction, diagnosis: dict) -> dict:
    txn.status = models.TransactionStatus.OVERDUE.value
    db.add(txn)
    db.commit()
    _audit(
        db, txn.id, "invoice_routed_b2b",
        f"Invoice routed to B2B negotiator (urgency={diagnosis.get('category')}, "
        f"days_overdue={diagnosis.get('days_overdue')})."
    )
    return {"status": "routed_to_negotiator", "urgency": diagnosis.get("category")}


@app.post("/api/v1/negotiate/{transaction_id}")
def negotiate_endpoint(transaction_id: str, body: dict, db: Session = Depends(get_db)):
    """Manually (or via simulator) drive one turn of the Hinglish B2B negotiator."""
    txn = db.query(models.Transaction).filter(models.Transaction.id == transaction_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    customer_message = body.get("message", "")
    result = agent.negotiate(db, txn, customer_message, txn.amount)
    return result


# ------------------------------------------------------------------------ #
# Read APIs for the dashboard
# ------------------------------------------------------------------------ #
@app.get("/api/v1/metrics")
def get_metrics(db: Session = Depends(get_db)):
    all_txns = db.query(models.Transaction).all()
    at_risk = sum(t.amount for t in all_txns)
    recovered = sum(t.amount for t in all_txns if t.status == models.TransactionStatus.RECOVERED.value)
    active_promises = (
        db.query(models.PromiseToPayLedger)
        .filter(models.PromiseToPayLedger.status == models.PromiseStatus.PENDING.value)
        .count()
    )
    recovery_rate = (recovered / at_risk * 100.0) if at_risk > 0 else 0.0

    return {
        "total_at_risk_revenue": round(at_risk, 2),
        "total_recovered": round(recovered, 2),
        "active_promises_to_pay": active_promises,
        "recovery_rate_pct": round(recovery_rate, 2),
        "total_transactions": len(all_txns),
    }


@app.get("/api/v1/transactions")
def list_transactions(db: Session = Depends(get_db)):
    txns = db.query(models.Transaction).order_by(models.Transaction.created_at.desc()).all()
    return [
        {
            "id": t.id,
            "amount": t.amount,
            "status": t.status,
            "rail_type": t.rail_type,
            "failure_reason": t.failure_reason,
            "customer_email": t.customer_email,
            "customer_phone": t.customer_phone,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in txns
    ]


@app.get("/api/v1/promises")
def list_promises(db: Session = Depends(get_db)):
    rows = db.query(models.PromiseToPayLedger).order_by(models.PromiseToPayLedger.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "transaction_id": r.transaction_id,
            "customer_id": r.customer_id,
            "promised_date": r.promised_date,
            "agreed_amount": r.agreed_amount,
            "status": r.status,
            "notes": r.notes,
        }
        for r in rows
    ]


@app.get("/api/v1/audit-log")
def list_audit_log(db: Session = Depends(get_db)):
    rows = db.query(models.AuditLog).order_by(models.AuditLog.timestamp.desc()).limit(500).all()
    return [
        {
            "id": r.id,
            "transaction_id": r.transaction_id,
            "event_type": r.event_type,
            "guardrail_triggered": r.guardrail_triggered,
            "log_message": r.log_message,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        }
        for r in rows
    ]


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------------ #
def _get_or_create_transaction(db: Session, diagnosis: dict) -> models.Transaction:
    """
    Looks up an existing transaction to attach this event to, preferring the
    stable `dedupe_key` (e.g. subscription_id for mandate retries, order_id for
    checkout/invoices) over `razorpay_payment_id`, since payment_id changes on
    every retry attempt while the underlying mandate/order does not. Without
    this, repeated failures against the same mandate would each spawn a new
    Transaction row and the mandate-retry guardrail would never accumulate.
    """
    existing = None
    dedupe_key = diagnosis.get("dedupe_key")
    if dedupe_key:
        existing = (
            db.query(models.Transaction)
            .filter(
                models.Transaction.dedupe_key == dedupe_key,
                models.Transaction.rail_type == diagnosis.get("rail"),
            )
            .first()
        )
    if not existing and diagnosis.get("razorpay_payment_id"):
        existing = (
            db.query(models.Transaction)
            .filter(models.Transaction.razorpay_payment_id == diagnosis["razorpay_payment_id"])
            .first()
        )
    if existing:
        return existing

    txn = models.Transaction(
        razorpay_payment_id=diagnosis.get("razorpay_payment_id"),
        razorpay_order_id=diagnosis.get("razorpay_order_id"),
        dedupe_key=dedupe_key,
        amount=diagnosis.get("amount", 0.0),
        status=models.TransactionStatus.FAILED.value,
        failure_reason=diagnosis.get("failure_reason"),
        customer_email=diagnosis.get("customer_email"),
        customer_phone=diagnosis.get("customer_phone"),
        rail_type=diagnosis.get("rail"),
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def _audit(db: Session, transaction_id: str | None, event_type: str, message: str, guardrail: bool = False):
    entry = models.AuditLog(
        transaction_id=transaction_id,
        event_type=event_type,
        guardrail_triggered=guardrail,
        log_message=message,
    )
    db.add(entry)
    db.commit()
