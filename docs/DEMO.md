# The Demo Company

## NovaTech Solutions Pvt. Ltd.

A Mumbai-based IT hardware and services company with twelve months of trading
history, three warehouses and five bank accounts.

Load it from the first-run screen, or:

```bash
curl -X POST http://localhost:8000/api/v1/demo/load
```

Sign in as **`demo@ailedger.local`** / **`demo1234`**.

Generation takes about 30 seconds.

---

## What makes this different from seed data

**Every figure is the result of a transaction posted through the real
accounting engine.** Nothing is written directly to a balance column and no
number is chosen to look good.

The generator creates an invoice by calling the same `create_invoice` service
the API calls. That prices the lines, resolves the GST rate as of the invoice
date, posts a balanced journal, moves stock, computes COGS and writes an audit
event. If the engine would reject a document, the generator's document is
rejected too.

This is worth caring about because it makes the demo *falsifiable*. The trial
balance balances because the books were genuinely kept, not because the totals
were assigned. You can check:

```
Reports → Trial Balance     →  five accounting invariants, computed live
Audit Log → Verify chain    →  3,697 events, hash chain recomputed
Inventory → Rebuild valuation → 42 items re-derived from movement history
```

All of them are recomputed from the data when you press the button. During
development this caught four real bugs that reading the code had not, including
a reversal that left accounts receivable at −₹5,900 instead of zero.

---

## What you get

| | |
|---|---|
| Customers | 112 |
| Vendors | 107 |
| Items | 50 across 3 warehouses |
| Bank accounts | 5 |
| Employees | 12 |
| Chart of accounts | 116 accounts |
| Sales invoices | 532, with 1,192 lines |
| Receipts | 437 |
| Purchase bills | 336, with 496 lines |
| Payments | 267 |
| Journal entries | 2,077, with 6,358 lines |
| Bank transactions | 676 |
| Stock movements | 1,354 |
| TDS entries | 103 |
| Audit events | 3,697 |
| Accounting periods | 24 |
| **Total transactional records** | **~13,700** |

Trading covers a full financial year, with a growth trend and seasonal peaks in
March, October and November so the trend charts show a real shape rather than
noise.

Counterparties are onboarded **gradually** across the period rather than all at
once. That detail matters: the anomaly detectors use `created_at` to decide what
counts as a new vendor, and a book where every vendor was created this morning
would flood the exception centre on day one with a signal that means nothing.

---

## The deliberate problems

A clean dataset demonstrates nothing. These are seeded on purpose, and each one
is genuinely detectable rather than pre-labelled:

| Seeded issue | Where it surfaces |
|---|---|
| **Near-duplicate bill** — same vendor, same amount, reference differing by one character | Exceptions → `DUPLICATE_BILL` |
| **Duplicate payment** — same vendor and amount, four days apart | Exceptions → `DUPLICATE_PAYMENT` |
| **Payment after a bank-detail change** — released two days after the vendor's account was edited | Exceptions → `VENDOR_BANK_CHANGE` |
| **Marketing expense spike** — 2.4× the usual monthly spend | Reports → Variance; Reporting Agent |
| **Round-number manual provisions** | Exceptions → `ROUND_NUMBER` |
| **18 bank-only transactions** with no matching ledger entry | Banking → Reconciliation |
| **Overdue receivables and payables** across several aging buckets | Dashboard, AR/AP aging |
| **Low and dead stock** | Inventory → Movement analysis |
| **After-hours and weekend journals** | Exceptions |

The near-duplicate is worth explaining. An *exact* duplicate — same vendor, same
invoice number — is refused outright by a database constraint, because vendor
invoice numbers are unique by definition. The realistic and dangerous case is
the one seeded here: same vendor, same amount, a reference that differs by a
single character. That gets past a constraint and has to be *detected*.

---

## A ten-minute tour

**1. Dashboard.** The AI briefing at the top is generated from the actual
figures — unreconciled transactions, overdue balances, open anomalies, close
progress. Eight KPIs, a twelve-month revenue and expense trend, and a live
statement of whether the accounting invariants hold.

**2. Ask the copilot** (`/ai`, or Ctrl/Cmd+K):

- *What is our cash position?*
- *Show me overdue invoices*
- *Why is profit down this month?*
- *Are there any duplicate bills?*
- *Forecast cash for the next 30 days*

Every answer lists the typed query tools that produced it. Click one to see the
underlying data. The copilot cannot write SQL — see [AI_AGENTS.md](AI_AGENTS.md).

**3. Banking → Reconciliation.** Run the matcher. It matches around 97% of 676
transactions across eight strategies and shows the *evidence* for each match —
amount, date proximity, reference, counterparty. The leftovers are the
interesting part.

**4. Exceptions.** The seeded problems, found rather than announced, each with
evidence and a recommended action.

**5. Trial Balance.** Scroll to the footer: the five invariants, recomputed on
load.

**6. Audit Log → Verify chain.** Recomputes 3,697 hashes and reports the first
break, if any. There is no code path that edits or deletes an audit event.

**7. Month-end close.** Sixteen checks with evidence. Two are blocking, on
purpose — the close cannot complete while unreconciled items remain, which is
what a close checklist is for.

**8. Tax → e-Invoice.** Note the **Demo mode** badge. The payload is real and
validated; the IRN is not, and the system says so everywhere it appears. See
[TAX.md](TAX.md).

---

## Resetting

```bash
curl -X POST http://localhost:8000/api/v1/demo/load?force=true    # rebuild
curl -X POST http://localhost:8000/api/v1/demo/reset \
     -H "Authorization: Bearer $TOKEN"                            # remove
```

`reset` deletes in explicit dependency order. It has to: `journal_lines` holds
`RESTRICT` foreign keys to `accounts`, so a naive cascade from `companies` fails
— an account that has ever been posted to cannot be deleted while its postings
exist. That constraint is deliberate, and the reset works around it in the
correct order rather than by weakening it.

`reset` only ever removes companies flagged as demo data. A real company is
never touched.

---

## Verifying it yourself

```bash
# Six integrity checks over the generated books
curl -s localhost:8000/api/v1/demo/verify -H "Authorization: Bearer $TOKEN"
```

```
Accounting invariants ......... pass   (all five)
Trial balance balances ........ pass
Balance sheet balances ........ pass   (difference 0.00)
Cash flow reconciles .......... pass
Audit chain intact ............ pass   (3,697 events)
Stock valuation consistent .... pass   (42 items, 0 inconsistent)
```

Or from the repository:

```bash
cd backend
.venv/bin/python -m pytest                # 177 unit tests
.venv/bin/python scripts/verify_api.py    # 114 HTTP checks

cd ../frontend
node e2e/ui.spec.mjs                      # 55 browser checks
```

On Windows use `.venv\Scripts\python`, and run each line on its own — Windows
PowerShell 5.1 has no `&&`.

---

## Regenerating with different volumes

```bash
curl -X POST "localhost:8000/api/v1/demo/load?months=6&force=true"
```

`months` accepts 1–24. The name pools, product catalogue and seeded issues live
at the top of `backend/app/demo/generator.py` and are straightforward to edit.

One caution learned the hard way: the restocking rate and the sales rate have to
move together. Raising purchase volume without raising sales buries the company
in inventory and overdraws its bank accounts — the books stay perfectly
balanced, and the company is simply insolvent. The engine will not stop you,
because that is a real business outcome, not an accounting error.
