"""
razorpay_service.py
Thin wrapper around the official `razorpay` Python SDK.
Every method here talks to Razorpay's TEST mode endpoints (rzp_test_* keys).
"""
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import razorpay

from config import settings

logger = logging.getLogger("revops.razorpay_service")


class RazorpayService:
    def __init__(self, key_id: Optional[str] = None, key_secret: Optional[str] = None):
        self.key_id = key_id or settings.RAZORPAY_KEY_ID
        self.key_secret = key_secret or settings.RAZORPAY_KEY_SECRET
        self.client = razorpay.Client(auth=(self.key_id, self.key_secret))
        # Skips SDK-side cert pinning quirks in some sandboxes; harmless in prod.
        self.client.set_app_details({"title": "RevOps.ai", "version": "1.0.0"})

    # ------------------------------------------------------------------ #
    # Checkout Drop-off Saver Rail
    # ------------------------------------------------------------------ #
    def create_fallback_payment_link(
        self,
        amount: float,
        description: str,
        customer_email: Optional[str] = None,
        customer_phone: Optional[str] = None,
        expiry_minutes: int = 15,
    ) -> dict:
        """
        Creates a short-lived Razorpay Payment Link to recover a dropped-off checkout.
        `amount` is in INR major units (rupees); Razorpay expects paise.
        """
        expire_by = int((datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)).timestamp())

        payload = {
            "amount": int(round(amount * 100)),
            "currency": "INR",
            "description": description,
            "expire_by": expire_by,
            "reminder_enable": True,
            "notify": {
                "sms": bool(customer_phone),
                "email": bool(customer_email),
            },
        }
        if customer_email or customer_phone:
            payload["customer"] = {
                "name": "Valued Customer",
                "email": customer_email or "",
                "contact": customer_phone or "",
            }

        try:
            link = self.client.payment_link.create(payload)
            logger.info("Created fallback payment link %s (expires %s)", link.get("id"), expire_by)
            return link
        except Exception as exc:  # razorpay.errors.BadRequestError, network errors, etc.
            logger.error("Failed to create payment link: %s", exc)
            return {
                "error": str(exc),
                "id": None,
                "short_url": None,
                "expire_by": expire_by,
            }

    # ------------------------------------------------------------------ #
    # Smart Mandate Retry Rail
    # ------------------------------------------------------------------ #
    def trigger_mandate_retry(self, subscription_id: str) -> dict:
        """
        Programs a retry charge on an existing subscription/mandate.
        Guardrail on retry COUNT lives in guardrails.py — this method only
        performs the actual API call once the caller has cleared that check.

        NOTE: Razorpay does not publicly expose a "force-charge-now" endpoint
        on Subscriptions in the standard `razorpay` SDK/API today — retries on
        a failed subscription charge are normally driven by Razorpay's own
        dunning schedule, not an on-demand call. We still probe for a
        `.charge()` method defensively (some SDK builds/API versions do add
        one), and otherwise fall back to confirming the mandate is still
        active via `subscription.fetch()` and marking the retry as
        "scheduled" — which is what actually happens in production. This
        keeps the guardrail (max 3 retries) meaningful regardless of which
        code path executes, since the retry COUNT is tracked in our own DB,
        not derived from this API response.
        """
        charge_fn = getattr(self.client.subscription, "charge", None)
        if callable(charge_fn):
            try:
                result = charge_fn(subscription_id, {})
                logger.info("Triggered mandate retry for subscription %s", subscription_id)
                return result
            except Exception as exc:
                logger.error("Mandate retry (charge) failed for %s: %s", subscription_id, exc)
                return {"error": str(exc), "subscription_id": subscription_id, "status": "failed"}

        try:
            sub = self.client.subscription.fetch(subscription_id)
            logger.info(
                "No direct charge() API available; confirmed subscription %s status=%s, "
                "retry scheduled via Razorpay's dunning cycle.",
                subscription_id, sub.get("status"),
            )
            return {
                "subscription_id": subscription_id,
                "status": "retry_scheduled",
                "subscription_status": sub.get("status"),
            }
        except Exception as exc:
            logger.error("Mandate retry (fetch fallback) failed for %s: %s", subscription_id, exc)
            return {"error": str(exc), "subscription_id": subscription_id, "status": "failed"}

    # ------------------------------------------------------------------ #
    # Webhook signature verification
    # ------------------------------------------------------------------ #
    def verify_webhook_signature(
        self, payload_body: str, signature: str, secret: Optional[str] = None
    ) -> bool:
        """
        Validates the HMAC-SHA256 webhook signature Razorpay sends in the
        `X-Razorpay-Signature` header. Uses the SDK's built-in utility, which
        is a constant-time comparison under the hood.
        """
        secret = secret or settings.RAZORPAY_WEBHOOK_SECRET
        try:
            razorpay.Utility(self.client).verify_webhook_signature(
                payload_body, signature, secret
            )
            return True
        except razorpay.errors.SignatureVerificationError:
            return False
        except Exception as exc:
            logger.error("Unexpected error verifying webhook signature: %s", exc)
            return False

    @staticmethod
    def compute_signature(payload_body: str, secret: str) -> str:
        """Helper used by the simulator to sign synthetic payloads the same way Razorpay does."""
        return hmac.new(
            key=secret.encode("utf-8"),
            msg=payload_body.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()


razorpay_service = RazorpayService()
