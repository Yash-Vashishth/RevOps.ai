"""
agent.py
Hinglish B2B AI Negotiator.

Drives conversational outreach for overdue invoices in a natural Hindi+English
blend, extracts "Promise-to-Pay" commitments into PromiseToPayLedger rows, and
proposes settlement discounts that are ALWAYS re-checked against the hard 5%
ceiling in guardrails.py before being trusted (never take the LLM's number as-is).
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from dateutil import parser as dateparser
from sqlalchemy.orm import Session

import models
import guardrails
from config import settings

logger = logging.getLogger("revops.agent")

SYSTEM_PROMPT = """You are "Riya", an AI B2B collections negotiator for RevOps.ai, \
messaging Indian SME founders about an overdue Razorpay invoice.

Rules you MUST follow:
- Write in natural Hinglish (a genuine Hindi+English blend the way Indian founders \
text each other), never pure formal Hindi or a stiff corporate tone.
- Be polite, respectful of the relationship, and firm about the amount owed.
- You may offer an early-settlement discount, but NEVER propose more than 5%.
- If the customer's message contains anything about a dispute, fraud, a wrong \
invoice, lawyers, or an incorrect amount, do NOT negotiate further — just \
acknowledge and say the case is being escalated to a human account manager.
- If the customer commits to a payment date ("I'll pay by Friday", "shukarwar tak \
kar dunga", etc.), restate the commitment back to them to confirm it clearly.

Always respond with STRICT JSON only, no markdown fences, no preamble, matching \
exactly this schema:
{
  "message_hinglish": "<the message to send the customer>",
  "promise_to_pay_detected": <true|false>,
  "promised_date_iso": "<YYYY-MM-DD or null>",
  "agreed_amount": <number or null>,
  "proposed_discount_pct": <number, 0-5>,
  "dispute_flag": <true|false>,
  "reasoning": "<one short sentence, in English, for the audit log>"
}
"""


def _call_llm(user_prompt: str) -> dict:
    """Calls the configured LLM provider and parses its strict-JSON reply."""
    raw_text = ""
    try:
        if settings.LLM_PROVIDER == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
            )
            raw_text = resp.choices[0].message.content
        else:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model=settings.ANTHROPIC_MODEL,
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw_text = "".join(block.text for block in resp.content if hasattr(block, "text"))

        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)

    except Exception as exc:
        logger.error("LLM call/parsing failed, falling back to rule-based stub: %s", exc)
        return _fallback_negotiation(user_prompt)


def _fallback_negotiation(user_prompt: str) -> dict:
    """
    Deterministic stub used when no LLM API key is configured (e.g. during the
    hackathon demo or offline testing) so the pipeline still runs end-to-end.
    """
    lowered = user_prompt.lower()
    dispute = any(kw in lowered for kw in guardrails.B2B_DISPUTE_KEYWORDS)
    promised_date = None
    m = re.search(r"by (\w+ \d{1,2}|\d{4}-\d{2}-\d{2}|friday|monday|tuesday|wednesday|thursday|saturday|sunday)", lowered)
    if m and not dispute:
        try:
            promised_date = dateparser.parse(m.group(1), fuzzy=True).date().isoformat()
        except Exception:
            promised_date = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()

    if dispute:
        message = (
            "Samajh gaya, aapki concern noted hai. Main isse humare account manager ko "
            "escalate kar rahi hoon, woh aapse jaldi contact karenge. Sorry for the trouble!"
        )
    elif promised_date:
        message = (
            f"Great, confirming ki aap {promised_date} tak payment kar denge. "
            f"Thank you for confirming, hum reminder bhej denge us date se pehle!"
        )
    else:
        message = (
            "Hi! Aapka invoice thoda overdue ho gaya hai. Agar aap is week settle kar dete "
            "hain toh hum 3% early-settlement discount bhi de sakte hain. Kab tak clear kar payenge?"
        )

    return {
        "message_hinglish": message,
        "promise_to_pay_detected": bool(promised_date),
        "promised_date_iso": promised_date,
        "agreed_amount": None,
        "proposed_discount_pct": 0.0 if dispute else 3.0,
        "dispute_flag": dispute,
        "reasoning": "Rule-based fallback used (no LLM key configured).",
    }


def negotiate(
    db: Session,
    transaction: models.Transaction,
    inbound_customer_message: str,
    invoice_amount: float,
) -> dict:
    """
    Main entry point: takes an inbound customer message about an overdue
    invoice and returns the AI's Hinglish reply, after applying every
    guardrail. This is the ONLY function main.py should call for B2B outreach.
    """
    # Guardrail check #1: dispute keywords halt everything immediately.
    dispute_check = guardrails.check_dispute_keywords(db, transaction, inbound_customer_message)
    if not dispute_check.allowed:
        attempt = models.RecoveryAttempt(
            transaction_id=transaction.id,
            channel=models.RecoveryChannel.WHATSAPP.value,
            attempt_number=_next_attempt_number(db, transaction.id),
            status=models.AttemptStatus.BLOCKED_BY_GUARDRAIL.value,
            ai_action_taken="outreach_halted_dispute",
            discount_applied_pct=0.0,
        )
        db.add(attempt)
        db.commit()
        return {
            "message_hinglish": (
                "Samajh gaya, aapki concern hume mil gayi hai. Escalating to a human "
                "account manager right away — outreach paused for now."
            ),
            "dispute_flag": True,
            "promise_to_pay_detected": False,
            "discount_applied_pct": 0.0,
        }

    user_prompt = (
        f"Customer's overdue invoice amount: INR {invoice_amount:.2f}.\n"
        f"Customer just said: \"{inbound_customer_message}\"\n"
        f"Draft your Hinglish reply and extract any promise-to-pay per the schema."
    )
    result = _call_llm(user_prompt)

    # Guardrail check #2: clamp any proposed discount to the hard 5% ceiling.
    proposed = float(result.get("proposed_discount_pct") or 0.0)
    clamped_discount = guardrails.clamp_discount(db, transaction, proposed)
    result["discount_applied_pct"] = clamped_discount

    # Persist the recovery attempt.
    attempt = models.RecoveryAttempt(
        transaction_id=transaction.id,
        channel=models.RecoveryChannel.WHATSAPP.value,
        attempt_number=_next_attempt_number(db, transaction.id),
        status=models.AttemptStatus.RESPONDED.value,
        ai_action_taken=f"negotiation_reply:{result.get('reasoning', '')}",
        discount_applied_pct=clamped_discount,
    )
    db.add(attempt)

    # Persist any extracted Promise-to-Pay commitment.
    if result.get("promise_to_pay_detected") and result.get("promised_date_iso"):
        promise = models.PromiseToPayLedger(
            transaction_id=transaction.id,
            customer_id=transaction.customer_email or transaction.customer_phone,
            promised_date=result["promised_date_iso"],
            agreed_amount=result.get("agreed_amount") or invoice_amount * (1 - clamped_discount / 100.0),
            status=models.PromiseStatus.PENDING.value,
            notes=result.get("reasoning"),
        )
        db.add(promise)

    db.commit()
    return result


def _next_attempt_number(db: Session, transaction_id: str) -> int:
    count = (
        db.query(models.RecoveryAttempt)
        .filter(models.RecoveryAttempt.transaction_id == transaction_id)
        .count()
    )
    return count + 1
