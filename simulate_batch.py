"""
simulate_batch.py
Generates a batch of 20+ realistic, HMAC-signed synthetic Razorpay webhooks and
fires them at the running FastAPI backend (POST /api/v1/webhooks/razorpay),
end-to-end exercising all three recovery rails and their guardrails:

  1. Checkout Drop-off Saver Rail   (payment.failed)
  2. Smart Mandate Retry Rail       (subscription.charged_failed x4 -> proves 4th is blocked)
  3. B2B Receivables Negotiator     (invoice.overdue -> negotiate -> dispute message halts outreach)

Run the backend first:
    uvicorn main:app --reload

Then in a second terminal:
    python simulate_batch.py
"""
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from config import settings
from razorpay_service import RazorpayService

BASE_URL = "http://localhost:8000"
WEBHOOK_URL = f"{BASE_URL}/api/v1/webhooks/razorpay"
WEBHOOK_SECRET = settings.RAZORPAY_WEBHOOK_SECRET

INDIAN_NAMES = ["Aarav", "Vivaan", "Diya", "Ananya", "Rohan", "Kavya", "Ishaan", "Meera", "Arjun", "Priya"]
COMPANIES = ["Nimbus Traders", "Zenith Textiles", "Kavya Foods Pvt Ltd", "Orbit Logistics", "BluePeak Retail"]


def _rid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def _sign(body_str: str) -> str:
    return RazorpayService.compute_signature(body_str, WEBHOOK_SECRET)


def send_webhook(event: str, payload_entity: dict, top_level_extra: dict | None = None) -> dict:
    body = {
        "event": event,
        "payload": payload_entity,
        "created_at": int(time.time()),
    }
    if top_level_extra:
        body.update(top_level_extra)

    body_str = json.dumps(body)
    signature = _sign(body_str)

    resp = requests.post(
        WEBHOOK_URL,
        data=body_str,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
        },
        timeout=15,
    )
    try:
        return {"status_code": resp.status_code, "body": resp.json()}
    except Exception:
        return {"status_code": resp.status_code, "body": resp.text}


# ------------------------------------------------------------------ #
# Payload builders
# ------------------------------------------------------------------ #
def build_payment_failed(amount_rupees: float, transient: bool) -> dict:
    payment_id = _rid("pay")
    order_id = _rid("order")
    name = random.choice(INDIAN_NAMES)
    reason = random.choice(["gateway_timeout", "network_error"]) if transient else "checkout_abandoned"
    description = "Gateway timeout during authorization" if transient else "Customer closed checkout window"

    return {
        "payment": {
            "entity": {
                "id": payment_id,
                "order_id": order_id,
                "amount": int(amount_rupees * 100),
                "currency": "INR",
                "status": "failed",
                "error_reason": reason,
                "error_description": description,
                "email": f"{name.lower()}@example.com",
                "contact": f"+9198{random.randint(10000000, 99999999)}",
            }
        }
    }


def build_subscription_charge_failed(subscription_id: str, amount_rupees: float, low_balance: bool) -> dict:
    payment_id = _rid("pay")
    name = random.choice(INDIAN_NAMES)
    error_code = "BAD_REQUEST_ERROR" if low_balance else "GATEWAY_ERROR"
    error_description = "Insufficient funds in customer account" if low_balance else "Issuer gateway temporarily down"

    return {
        "subscription": {"entity": {"id": subscription_id, "status": "active"}},
        "payment": {
            "entity": {
                "id": payment_id,
                "amount": int(amount_rupees * 100),
                "currency": "INR",
                "error_code": error_code,
                "error_description": error_description,
                "email": f"{name.lower()}@example.com",
                "contact": f"+9197{random.randint(10000000, 99999999)}",
            }
        },
    }


def build_invoice_overdue(amount_rupees: float, days_overdue: int, ltv: float, company: str) -> dict:
    order_id = _rid("order")
    return {
        "invoice": {
            "entity": {
                "order_id": order_id,
                "amount_due": int(amount_rupees * 100),
                "days_overdue": days_overdue,
                "customer_ltv": ltv,
                "customer_details": {
                    "email": f"accounts@{company.lower().replace(' ', '')}.com",
                    "contact": f"+9196{random.randint(10000000, 99999999)}",
                },
            }
        }
    }


# ------------------------------------------------------------------ #
# Scenarios
# ------------------------------------------------------------------ #
def run_checkout_dropoff_scenario(n: int = 8) -> list:
    print(f"\n--- [Rail 1] Checkout Drop-off Saver: sending {n} payment.failed events ---")
    results = []
    for i in range(n):
        amount = round(random.uniform(499, 4999), 2)
        transient = i % 3 == 0
        payload = build_payment_failed(amount, transient)
        res = send_webhook("payment.failed", payload)
        print(f"  [{i+1}/{n}] amount=₹{amount} transient={transient} -> HTTP {res['status_code']}")
        results.append(res)
        time.sleep(0.05)
    return results


def run_mandate_retry_scenario(n_subscriptions: int = 3) -> list:
    print(f"\n--- [Rail 2] Smart Mandate Retry: {n_subscriptions} subscriptions x 4 failures each ---")
    print("    (guardrail caps retries at 3 — the 4th failure per subscription must be BLOCKED)")
    results = []
    for s in range(n_subscriptions):
        subscription_id = _rid("sub")
        amount = round(random.uniform(999, 2999), 2)
        for attempt in range(1, 5):  # 4 successive failures
            low_balance = attempt % 2 == 0
            payload = build_subscription_charge_failed(subscription_id, amount, low_balance)
            res = send_webhook("subscription.charged_failed", payload)
            blocked = "BLOCKED" if attempt == 4 else "processed"
            print(f"  sub={subscription_id} attempt={attempt} expected={blocked} -> HTTP {res['status_code']} "
                  f"body_status={res['body'].get('result', {}).get('status') if isinstance(res['body'], dict) else res['body']}")
            results.append(res)
            time.sleep(0.05)
    return results


def run_b2b_negotiator_scenario(n: int = 5) -> list:
    print(f"\n--- [Rail 3] B2B Receivables Negotiator: {n} overdue invoices ---")
    results = []
    txn_ids = []

    for i in range(n):
        amount = round(random.uniform(15000, 250000), 2)
        days_overdue = random.choice([5, 12, 35])
        ltv = random.choice([50000, 300000, 750000])
        company = random.choice(COMPANIES)
        payload = build_invoice_overdue(amount, days_overdue, ltv, company)
        res = send_webhook("invoice.overdue", payload)
        print(f"  [{i+1}/{n}] {company} amount=₹{amount} days_overdue={days_overdue} -> HTTP {res['status_code']}")
        results.append(res)
        time.sleep(0.05)

    # Pull the transactions we just created so we can drive negotiation turns.
    txns_resp = requests.get(f"{BASE_URL}/api/v1/transactions", timeout=15).json()
    overdue_txns = [t for t in txns_resp if t["rail_type"] == "b2b_receivables_negotiator"][:n]
    txn_ids = [t["id"] for t in overdue_txns]

    print("\n  Driving negotiation turns (normal promise-to-pay case):")
    for txn_id in txn_ids[:-1] if len(txn_ids) > 1 else txn_ids:
        msg = random.choice([
            "Haan bhai, thoda cash flow tight hai, main Friday tak clear kar dunga.",
            "Sorry for the delay, I'll pay by next Monday.",
            "Is week busy tha, shukarwar tak kar dunga pakka.",
        ])
        resp = requests.post(f"{BASE_URL}/api/v1/negotiate/{txn_id}", json={"message": msg}, timeout=20)
        print(f"    txn={txn_id} customer_msg='{msg}' -> {resp.status_code} {resp.json().get('message_hinglish', '')[:80]}")
        results.append({"status_code": resp.status_code, "body": resp.json()})

    print("\n  Testing dispute-keyword guardrail (must HALT outreach immediately):")
    if txn_ids:
        dispute_txn = txn_ids[-1]
        dispute_msg = "This is a dispute — the invoice amount is wrong and we're consulting our lawyer."
        resp = requests.post(f"{BASE_URL}/api/v1/negotiate/{dispute_txn}", json={"message": dispute_msg}, timeout=20)
        body = resp.json()
        halted = body.get("dispute_flag") is True
        print(f"    txn={dispute_txn} customer_msg='{dispute_msg}'")
        print(f"    -> HTTP {resp.status_code} | dispute_flag={body.get('dispute_flag')} | "
              f"{'HALTED (guardrail worked)' if halted else 'WARNING: not halted!'}")
        results.append({"status_code": resp.status_code, "body": body})

    return results


def print_summary():
    print("\n" + "=" * 60)
    print("SIMULATION SUMMARY")
    print("=" * 60)
    metrics = requests.get(f"{BASE_URL}/api/v1/metrics", timeout=15).json()
    for k, v in metrics.items():
        print(f"  {k:28s}: {v}")

    audit = requests.get(f"{BASE_URL}/api/v1/audit-log", timeout=15).json()
    guardrail_events = [a for a in audit if a["guardrail_triggered"]]
    print(f"\n  Guardrail events triggered   : {len(guardrail_events)}")
    for g in guardrail_events[:10]:
        print(f"    - [{g['event_type']}] {g['log_message']}")
    print("=" * 60)


if __name__ == "__main__":
    print("RevOps.ai — Synthetic Batch Test Runner")
    print(f"Target: {WEBHOOK_URL}")
    try:
        requests.get(f"{BASE_URL}/health", timeout=5)
    except requests.exceptions.ConnectionError:
        print("\n[ERROR] Backend not reachable. Start it first with:\n    uvicorn main:app --reload\n")
        raise SystemExit(1)

    random.seed()
    run_checkout_dropoff_scenario(n=8)
    run_mandate_retry_scenario(n_subscriptions=3)  # 3 x 4 = 12 events
    run_b2b_negotiator_scenario(n=5)
    # Total events fired: 8 + 12 + 5 = 25 (> 20 required)

    print_summary()
