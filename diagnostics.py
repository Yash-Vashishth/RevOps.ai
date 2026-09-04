"""
diagnostics.py
Diagnostic triage: inspects incoming Razorpay webhook payloads and decides
which recovery rail a failed/overdue transaction should be routed to.
"""
import logging
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger("revops.diagnostics")

# Error codes/descriptions that indicate a *transient* gateway problem
# (worth retrying) vs. a *persistent* customer-side problem (e.g. low balance).
TRANSIENT_ERROR_HINTS = [
    "gateway", "timeout", "network", "issuer_down", "processing_error", "server_error",
]
LOW_BALANCE_HINTS = [
    "insufficient", "low balance", "funds", "limit exceeded", "declined",
]


def diagnose_payment_failed(payload: dict) -> dict:
    """
    payment.failed -> Checkout Drop-off Saver Rail.
    Classifies as checkout drop-off (customer abandoned) vs gateway downtime.
    """
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    error_reason = (entity.get("error_reason") or "").lower()
    error_description = (entity.get("error_description") or "").lower()
    combined = f"{error_reason} {error_description}"

    if any(hint in combined for hint in TRANSIENT_ERROR_HINTS):
        category = "gateway_downtime"
    else:
        category = "checkout_dropoff"

    return {
        "rail": models.RailType.CHECKOUT_DROPOFF.value,
        "category": category,
        "amount": (entity.get("amount") or 0) / 100.0,
        "razorpay_payment_id": entity.get("id"),
        "razorpay_order_id": entity.get("order_id"),
        "dedupe_key": entity.get("order_id") or entity.get("id"),
        "customer_email": entity.get("email"),
        "customer_phone": entity.get("contact"),
        "failure_reason": entity.get("error_description") or entity.get("error_reason") or "unknown",
    }


def diagnose_subscription_charge_failed(payload: dict) -> dict:
    """
    subscription.charged_failed -> Smart Mandate Retry Rail.
    Evaluates the raw error code: transient outage (retry now) vs.
    low balance (schedule retry / notify customer) vs. mandate revoked (stop).
    """
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    error_code = (payment_entity.get("error_code") or "").lower()
    error_description = (payment_entity.get("error_description") or "").lower()
    combined = f"{error_code} {error_description}"

    if any(hint in combined for hint in LOW_BALANCE_HINTS):
        category = "low_balance"
    elif "mandate" in combined and ("revoked" in combined or "cancelled" in combined):
        category = "mandate_revoked"
    else:
        category = "transient_outage"

    return {
        "rail": models.RailType.MANDATE_RETRY.value,
        "category": category,
        "amount": (payment_entity.get("amount") or 0) / 100.0,
        "subscription_id": entity.get("id"),
        # NOTE: each failed charge attempt gets a fresh razorpay_payment_id, but all
        # attempts against the same mandate share the same subscription_id. We key
        # transaction lookup on that (via dedupe_key) so the 3-retry guardrail in
        # guardrails.py actually accumulates across attempts instead of resetting.
        "dedupe_key": entity.get("id"),
        "razorpay_payment_id": payment_entity.get("id"),
        "customer_email": payment_entity.get("email"),
        "customer_phone": payment_entity.get("contact"),
        "failure_reason": payment_entity.get("error_description") or "subscription charge failed",
    }


def diagnose_invoice_overdue(payload: dict) -> dict:
    """
    invoice.overdue -> B2B Receivables Negotiator Rail.
    Reads invoice aging (days overdue) and customer lifetime value (LTV) to
    decide urgency, but routing itself is unconditional: all overdue B2B
    invoices go to the negotiator rail, which then applies its own guardrails.
    """
    entity = payload.get("payload", {}).get("invoice", {}).get("entity", {})
    customer = entity.get("customer_details", {}) or {}

    days_overdue = entity.get("days_overdue", 0)
    ltv = entity.get("customer_ltv", 0)

    if days_overdue >= 30 or ltv >= 500000:
        urgency = "high"
    elif days_overdue >= 10:
        urgency = "medium"
    else:
        urgency = "low"

    return {
        "rail": models.RailType.B2B_NEGOTIATOR.value,
        "category": urgency,
        "amount": (entity.get("amount_due") or 0) / 100.0,
        "razorpay_order_id": entity.get("order_id"),
        "dedupe_key": entity.get("order_id"),
        "customer_email": customer.get("email"),
        "customer_phone": customer.get("contact"),
        "failure_reason": f"invoice overdue by {days_overdue} days",
        "days_overdue": days_overdue,
        "customer_ltv": ltv,
    }


def triage_event(event: str, payload: dict) -> Optional[dict]:
    """Single entry point used by main.py's webhook handler."""
    if event == "payment.failed":
        return diagnose_payment_failed(payload)
    if event == "subscription.charged_failed":
        return diagnose_subscription_charge_failed(payload)
    if event == "invoice.overdue":
        return diagnose_invoice_overdue(payload)

    logger.info("Ignoring unhandled event type: %s", event)
    return None
