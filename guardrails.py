"""
guardrails.py
Deterministic, code-level safety checks that wrap every recovery workflow.
These are NOT suggestions to the LLM — they are hard checks evaluated in
plain Python before any Razorpay API call or AI-drafted message goes out.
Nothing in agent.py or main.py may bypass these functions.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger("revops.guardrails")

# --- Tunable constants (kept in one place for auditability) ---
CHECKOUT_MAX_LINKS_PER_SESSION = 1
CHECKOUT_LINK_EXPIRY_MINUTES = 15

MANDATE_MAX_RETRIES = 3

B2B_MAX_DISCOUNT_PCT = 5.0
B2B_DISPUTE_KEYWORDS = ["dispute", "fraud", "wrong invoice", "lawyer", "incorrect amount"]


@dataclass
class GuardrailResult:
    allowed: bool
    reason: str


def _log_audit(
    db: Session,
    transaction_id: Optional[str],
    event_type: str,
    message: str,
    guardrail_triggered: bool = False,
) -> None:
    entry = models.AuditLog(
        transaction_id=transaction_id,
        event_type=event_type,
        guardrail_triggered=guardrail_triggered,
        log_message=message,
    )
    db.add(entry)
    db.commit()
    if guardrail_triggered:
        logger.warning("[GUARDRAIL] %s | %s", event_type, message)
    else:
        logger.info("[AUDIT] %s | %s", event_type, message)


# ---------------------------------------------------------------------- #
# 1. Checkout Drop-off Saver Rail
# ---------------------------------------------------------------------- #
def check_checkout_link_allowed(db: Session, transaction: models.Transaction) -> GuardrailResult:
    """Hard cap of 1 recovery link per cart/transaction session."""
    existing_links = (
        db.query(models.RecoveryAttempt)
        .filter(
            models.RecoveryAttempt.transaction_id == transaction.id,
            models.RecoveryAttempt.ai_action_taken.like("%payment_link_created%"),
        )
        .count()
    )
    if existing_links >= CHECKOUT_MAX_LINKS_PER_SESSION:
        _log_audit(
            db,
            transaction.id,
            "checkout_link_blocked",
            f"Blocked: transaction {transaction.id} already has "
            f"{existing_links} payment link(s) (cap={CHECKOUT_MAX_LINKS_PER_SESSION}).",
            guardrail_triggered=True,
        )
        return GuardrailResult(False, "Per-session payment link cap reached")
    return GuardrailResult(True, "OK")


# ---------------------------------------------------------------------- #
# 2. Smart Mandate Retry Rail
# ---------------------------------------------------------------------- #
def check_mandate_retry_allowed(db: Session, transaction: models.Transaction) -> GuardrailResult:
    """Strict cap of 3 retries per mandate to avoid bank penalty fees."""
    retry_count = (
        db.query(models.RecoveryAttempt)
        .filter(
            models.RecoveryAttempt.transaction_id == transaction.id,
            models.RecoveryAttempt.ai_action_taken.like("%mandate_retry%"),
        )
        .count()
    )
    if retry_count >= MANDATE_MAX_RETRIES:
        _log_audit(
            db,
            transaction.id,
            "mandate_retry_blocked",
            f"Blocked: transaction {transaction.id} already used "
            f"{retry_count}/{MANDATE_MAX_RETRIES} mandate retries.",
            guardrail_triggered=True,
        )
        return GuardrailResult(False, f"Max mandate retries ({MANDATE_MAX_RETRIES}) reached")
    return GuardrailResult(True, "OK")


# ---------------------------------------------------------------------- #
# 3. B2B Receivables Negotiator Rail
# ---------------------------------------------------------------------- #
def check_dispute_keywords(db: Session, transaction: models.Transaction, message_text: str) -> GuardrailResult:
    """
    Scans an inbound customer message for dispute-risk keywords.
    If found: HALT all further outreach immediately and flag the record.
    """
    lowered = (message_text or "").lower()
    hit = next((kw for kw in B2B_DISPUTE_KEYWORDS if kw in lowered), None)
    if hit:
        transaction.status = models.TransactionStatus.LOST.value
        db.add(transaction)
        _log_audit(
            db,
            transaction.id,
            "dispute_keyword_detected",
            f"HALTED outreach for transaction {transaction.id}: matched keyword '{hit}' "
            f"in customer message. Flagged for human review.",
            guardrail_triggered=True,
        )
        return GuardrailResult(False, f"Dispute keyword detected: '{hit}'")
    return GuardrailResult(True, "OK")


def clamp_discount(db: Session, transaction: models.Transaction, proposed_discount_pct: float) -> float:
    """
    Enforces the hard 5% discount ceiling on any AI-negotiated settlement.
    Returns the (possibly clamped) discount — never trusts the LLM's number directly.
    """
    if proposed_discount_pct > B2B_MAX_DISCOUNT_PCT:
        _log_audit(
            db,
            transaction.id,
            "discount_clamped",
            f"AI proposed {proposed_discount_pct:.2f}% discount; clamped to "
            f"hard ceiling of {B2B_MAX_DISCOUNT_PCT}%.",
            guardrail_triggered=True,
        )
        return B2B_MAX_DISCOUNT_PCT
    if proposed_discount_pct < 0:
        return 0.0
    return proposed_discount_pct
