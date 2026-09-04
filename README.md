# RevOps.ai — AI Revenue Recovery Mesh

Built for **Razorpay Buildathon Track 03 (AI Revenue Recovery)**.

RevOps.ai is a unified mesh of three AI-assisted recovery rails that sit on top of
Razorpay's payments, subscriptions, and invoicing products:

| Rail | Trigger event | What it does |
|---|---|---|
| **Checkout Drop-off Saver** | `payment.failed` | Generates a short-lived Razorpay Payment Link (15 min expiry) to recover an abandoned checkout. Hard-capped at 1 link per transaction. |
| **Smart Mandate Retry** | `subscription.charged_failed` | Programs a retry charge on the subscription mandate. Hard-capped at 3 retries to avoid bank penalty fees. |
| **B2B Receivables Negotiator** | `invoice.overdue` | A Hinglish-speaking LLM agent negotiates overdue B2B invoices, extracts Promise-to-Pay commitments, and offers settlement discounts capped at 5%. Immediately halts on any dispute-related keyword. |

Every rail is wrapped in **deterministic, code-level guardrails** (`guardrails.py`) that
the LLM cannot bypass — the AI proposes, the code disposes.

---

## Project structure

```
revops-ai/
├── requirements.txt
├── .env.example
├── config.py              # centralized settings loader
├── database.py            # SQLAlchemy engine/session
├── models.py               # Transaction, RecoveryAttempt, PromiseToPayLedger, AuditLog
├── razorpay_service.py    # Razorpay SDK wrapper (payment links, mandate retries, webhook verify)
├── guardrails.py          # non-bypassable safety checks (retry caps, discount ceiling, dispute halt)
├── diagnostics.py         # webhook triage -> rail routing
├── agent.py               # Hinglish B2B AI negotiator (LLM-backed, with rule-based fallback)
├── main.py                 # FastAPI app: webhook endpoint + REST API
├── dashboard.py           # Streamlit merchant dashboard
├── simulate_batch.py      # synthetic HMAC-signed webhook batch runner
└── README.md
```

---

## 1. Setup

```bash
cd revops-ai
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:
- Add your Razorpay **test-mode** keys (`rzp_test_...`) from the [Razorpay Dashboard](https://dashboard.razorpay.com/app/keys).
- Add a webhook secret (any string you also configure in the Razorpay webhook settings, or reuse for local signing via the simulator).
- Add an `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and set `LLM_PROVIDER` accordingly.
  - **No LLM key?** `agent.py` automatically falls back to a deterministic rule-based
    Hinglish responder, so the whole pipeline still runs end-to-end for a demo.

The database (SQLite by default) and all tables are created automatically on first run —
no manual migration step needed.

---

## 2. Run the backend

```bash
uvicorn main:app --reload --port 8000
```

- Webhook endpoint: `POST http://localhost:8000/api/v1/webhooks/razorpay`
- Interactive API docs: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`

To point a real Razorpay test-mode account at this locally, expose port 8000 with a
tunnel (e.g. `ngrok http 8000`) and register the tunnel URL + `/api/v1/webhooks/razorpay`
as the webhook URL in your Razorpay Dashboard, subscribing to `payment.failed`,
`subscription.charged_failed`, and `invoice.overdue`.

---

## 3. Run the dashboard

In a second terminal (venv activated):

```bash
streamlit run dashboard.py
```

Opens at `http://localhost:8501`. Shows live metric cards, the AI Mesh vs. standard
fixed-interval retry comparison chart, per-rail breakdown, the Promise-to-Pay ledger,
and the guardrail-highlighted audit log. Click **Refresh data** to pull the latest
rows (auto-refreshes every 5s via cache TTL otherwise).

---

## 4. Run the synthetic batch simulator

With the backend running (`uvicorn main:app --reload`), in a third terminal:

```bash
python simulate_batch.py
```

This fires **25 HMAC-signed synthetic webhooks** at the backend, covering all three
rails, and specifically proves out the guardrails:

- Sends 8 `payment.failed` events (mix of gateway-downtime vs. checkout-abandonment).
- Sends 3 subscriptions × 4 consecutive `subscription.charged_failed` events each —
  the **4th failure per subscription is expected to be blocked** by the 3-retry cap.
- Sends 5 `invoice.overdue` events, then drives negotiation turns against the
  created transactions, including one message containing dispute language to prove
  the **B2B negotiator halts outreach immediately** on dispute keywords.
- Prints a final summary: total at-risk revenue, recovered amount, recovery rate,
  active promises-to-pay, and every guardrail event that fired.

Reload the Streamlit dashboard afterward to see the populated data.

---

## Recovery Rate metric

$$\text{Recovery Rate (\%)} = \left( \frac{\text{Total Currency Recovered}}{\text{Total At-Risk Revenue Ingested}} \right) \times 100$$

Computed live in both `GET /api/v1/metrics` and the dashboard.

---

## Guardrails at a glance (`guardrails.py`)

- **Checkout rail**: max 1 payment link per transaction session, 15-minute expiry.
- **Mandate retry rail**: max 3 retries per subscription/mandate.
- **B2B negotiator rail**:
  - Immediate halt on keywords: `dispute`, `fraud`, `wrong invoice`, `lawyer`, `incorrect amount`.
  - Every AI-proposed discount is clamped in code to a hard 5% ceiling — the LLM's
    number is never trusted directly.
  - Every guardrail trip is written to the immutable `AuditLog` table.

## Notes on the Razorpay testbed

- Payment links, subscriptions, and invoices used here assume standard Razorpay
  test-mode entities. Amounts are stored in the app as INR rupees (major units) and
  converted to paise only at the Razorpay API boundary, matching Razorpay's convention.
- `verify_webhook_signature` uses the official SDK's `Utility.verify_webhook_signature`,
  the same HMAC-SHA256 check Razorpay performs, so real Razorpay webhooks validate
  the same way the simulator's synthetic ones do.
