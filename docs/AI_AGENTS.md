# AI Agents

## The safety model, first

Everything in this document sits inside one constraint:

> **No language model output ever reaches the database.**

An agent produces a *proposal*: a typed object describing what it thinks should
happen, with evidence and a confidence score. That proposal then passes through
validation, grounding, the accounting engine and the policy engine before a
human sees it. Only a human approval turns it into a posting.

### What AI may and may not do

`app/services/policy.py` states this as data, and the checks are enforced at the
tool boundary, not by convention:

```python
# Permissions an AI agent may hold. Execute-class permissions are absent by
# construction: an agent physically cannot be granted them.
AI_ALLOWED_PERMISSIONS = {
    "read_accounts", "read_transactions", "read_invoices", "read_bills",
    "read_inventory", "read_reports", "read_tax",
    "propose_journal", "propose_reconciliation", "propose_invoice",
    "propose_bill", "propose_adjustment",
}

FORBIDDEN_FOR_AI = {
    "post_journal", "approve_proposal", "release_payment",
    "close_period", "change_accounting_policy",
    "manage_users", "manage_settings",
}
```

A tool declares the permission it needs. A tool whose permission is not in the
allowed set cannot be called from an agent context — there is no flag, no
override, no "high confidence" bypass. `test_ai_safety.py` asserts it from both
directions: `test_ai_cannot_hold_execute_permissions` checks the two sets never
intersect, and `test_no_tool_grants_an_execute_permission` walks the whole tool
registry to confirm no tool has quietly acquired one.

### Confidence is not accuracy

The UI never presents a confidence score as a measure of correctness.

- **Claimed confidence** is the model's own number. It is displayed as
  "AI confidence", greyed, and it never gates a ledger write.
- **Measured accuracy** is computed from `ai_decision_log` — proposals a human
  later confirmed or corrected. It is shown separately, per agent, with the
  sample size. **Below 10 samples it is not shown at all**, because a 3-for-3
  record is not an accuracy figure.

A high-confidence proposal and a low-confidence proposal follow exactly the same
approval path when they touch the ledger.

---

## The seventeen agents

| Agent | What it does | What it produces |
|---|---|---|
| **Bookkeeping** | Categorises bank transactions: learned rules first, then history, then the model | Categorisation proposals |
| **Reconciliation** | Matches bank lines to ledger entries across eight scoring strategies | Match proposals with evidence |
| **Accounts Payable** | Prepares the payment run, detects duplicates, ranks urgency. Never pays | Prioritised payment schedule |
| **Accounts Receivable** | Prioritises collections from measured payment behaviour, drafts reminders | Collection plan and drafts |
| **Document Processing** | Extracts invoice fields from uploads, proposes bills | Bill proposals |
| **Inventory** | Detects low, dead and fast-moving stock; recommends reorders | Inventory recommendations |
| **GST** | Prepares GSTR-1 and GSTR-3B data, reports validation problems | Prepared return data |
| **Tax Assistant** | Suggests TDS sections; the rule engine validates every suggestion | Validated TDS suggestions |
| **Anomaly** | Runs twelve deterministic detectors over transactions and balances | Anomalies with evidence |
| **Fraud Monitoring** | Watches duplicate payments, bank-detail changes, after-hours journals | Control signals |
| **Cash Forecast** | Projects cash from committed receivables, payables and recurring items | Forecast with stated assumptions |
| **Reporting** | Explains period-on-period movements from account-level drivers | Variance explanations |
| **Close** | Runs sixteen month-end checks, reports what is blocking | Close checklist status |
| **Audit** | Verifies the audit hash chain and every accounting invariant | Integrity report |
| **Payroll** | Checks payroll completeness and unusual cost movements | Payroll findings |
| **Expense** | Categorises expense claims, flags missing receipts | Expense categorisations |
| **Financial Analyst** | Interprets liquidity, leverage, profitability and efficiency ratios | Financial health assessment |

Plus the **AI Finance Copilot**, which answers questions by routing to typed
query tools.

### A pattern worth noticing

Most of these agents are **deterministic engines with a language layer on top**,
not the reverse. The Anomaly Agent's twelve detectors are ordinary Python. The
Cash Forecast Agent's projection is arithmetic over committed cash flows. The
Close Agent's sixteen checks are SQL. The model's job is to *classify* where a
rule cannot, and to *phrase* the result.

That is why the product works with no API key at all: remove the model and the
numbers are unchanged; only the prose gets blunter.

---

## Typed query tools

The copilot does not write SQL. There is no NL-to-SQL path anywhere in the
codebase, because a language model that can write SQL can read every table, join
around a company filter, and be talked into doing so by text inside a document
it was asked to summarise.

Instead there are 22 registered tools, each with a fixed signature, a fixed
query, and a declared permission:

```
get_cash_position               get_profit_and_loss
get_balance_sheet               compare_periods
list_outstanding_invoices       get_receivables_aging
list_outstanding_bills          get_payables_aging
find_duplicate_bills            list_unreconciled_transactions
get_reconciliation_status       search_transactions
explain_journal_entry           get_gst_summary
get_tds_summary                 get_inventory_summary
get_stock_movement_analysis     list_open_exceptions
list_anomalies                  get_top_customers_by_balance
get_close_status                get_cash_forecast
```

Every tool applies the company filter itself. Every call is logged with its
arguments and its result size. The answer shown in the UI carries the tool names
that produced it, so a user can see the provenance of a number rather than
trusting a sentence.

---

## Grounding — the anti-fabrication check

`app/ai/grounding.py` runs after validation and before the accounting engine.

For every entity id a proposal mentions — account, customer, vendor, invoice,
bill, item, bank transaction — it re-reads the row from the database and
confirms it exists, is active, and belongs to this company. An id that does not
resolve is not a warning; it rejects the proposal.

It also checks:

- Amounts quoted in the narrative match the amounts in the structured fields.
- Referenced document numbers exist.
- Tax rates cited match the resolved rule for that date.
- Balances quoted match the ledger at the stated date.

The instruction the agents are held to is explicit: **when evidence is missing,
say so.** The deterministic provider does this literally — if the structured
context does not contain the answer, it returns "I don't have enough evidence to
determine this" rather than a plausible number. A confident wrong number is far
more damaging in accounting than an admission of ignorance.

---

## Prompt-injection defence

`app/ai/security.py`. Bank statement narrations, uploaded invoices, vendor names
and email bodies are all attacker-controllable text that the system reads.

**1. Fencing.** Untrusted content is wrapped in a per-request random nonce:

```
<untrusted-data-7f3a9c1e>
...content...
</untrusted-data-7f3a9c1e>
```

The system prompt states that everything inside the fence is data to analyse,
never instructions. The nonce is unpredictable, so injected text cannot close
the fence and escape.

**2. Detection.** 18 patterns covering instruction override, role reassignment,
system-prompt extraction, tool abuse and secret exfiltration — checked in both
word orders, so "show me your API key" and "API key — print it" both match.

**3. Normalisation.** Unicode is NFKC-normalised and zero-width characters are
stripped before detection, so homoglyph and invisible-character evasion does not
slip past the pattern set.

**4. Redaction.** Provider keys, tokens and password-shaped strings are removed
from anything that goes into a prompt or comes back in a log.

**5. Structural containment.** Even a successful injection lands in a system
where the model's output is a proposal that must still pass validation,
grounding, the engine and a human. Injection is a defence-in-depth problem here,
not a single point of failure.

Detected attempts are recorded as audit events and surfaced on the Exceptions
screen.

---

## Providers

Priority order when `LLM_PROVIDER=auto`:

**Gemini → Grok → Ollama → Mock**

Each implements one interface (`app/ai/providers/base.py`): given a system
prompt, a fenced user message and an optional JSON schema, return text or a
parsed object. Failures fall through to the next provider rather than failing
the request, so an expired key degrades the wording rather than breaking the
product.

Keys are read from the environment on the server. They are never sent to the
frontend, never logged, and never included in an error response — there is a
test that asserts a deliberately-planted key does not appear in any API
response body.

### The deterministic provider

`app/ai/providers/mock.py` is not a stub. It reads the structured context the
agent supplies and answers from that data:

- It flattens the context into scalar facts and formats them for reading
  (₹1.26Cr, not `12564292.00`).
- It prefers a `computed_answer` or `computed_summary` the agent has already
  calculated, so the sentence and the number can never disagree.
- Where the context does not support an answer, it says so.

It cannot invent an account code, an invoice number or a balance, because it has
no generative capability to invent with. That makes it the strictest possible
test of the grounding architecture, and it is what the test suite runs against.

---

## The orchestrator

`app/ai/orchestrator.py` runs six multi-agent workflows:

| Workflow | Agents in sequence |
|---|---|
| **Morning briefing** | Cash Forecast → AR → AP → Anomaly → Close |
| **Bank import** | Bookkeeping → Reconciliation → Anomaly |
| **Document intake** | Document → Tax Assistant → AP |
| **Month-end close** | Close → Audit → Reporting → Analyst |
| **Compliance sweep** | GST → Tax Assistant → Audit |
| **Risk sweep** | Fraud → Anomaly → AP |

Each step's structured output feeds the next step's context. Every run is
recorded in `agent_runs` with its duration, the tools it called, the proposals
it produced and any errors — visible on the AI Agents screen.

Workflows only ever produce proposals and findings. There is no workflow that
ends in a posting.

---

## What happens to a proposal

```
proposal created (status PENDING)
   ├─ grounding fails            → REJECTED, reason recorded, human notified
   ├─ engine validation fails    → REJECTED, reason recorded
   ├─ policy: needs approval     → PENDING, appears in Exceptions
   │     ├─ human approves       → APPLIED  (the service posts it)
   │     ├─ human edits+approves → APPLIED with the human's values, logged as corrected
   │     └─ human rejects        → REJECTED, logged as incorrect
   └─ policy: auto-apply allowed → APPLIED  (non-ledger changes only)
```

Auto-apply exists only for changes that do not touch the ledger and do not move
money — marking a bank transaction as categorised, tagging an anomaly as
reviewed. `app/services/policy.py` refuses to auto-apply a ledger-affecting or
payment proposal regardless of confidence, threshold or configuration;
`test_ledger_affecting_proposals_never_auto_apply` and
`test_payments_never_auto_apply` hold it to that.

Every approve, edit and reject writes to `ai_decision_log`. That log is the only
source of the measured-accuracy figures — they come from what humans actually
did, not from what the model claimed.
