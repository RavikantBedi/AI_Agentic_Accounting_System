# The Accounting Engine

The engine is the part of this system that must never be wrong. Everything else
— the AI, the reports, the UI — is downstream of it.

`app/accounting/engine.py` is the **only** code in the codebase permitted to
write to `journal_entries` and `journal_lines`. The API, the importers and every
AI agent go through it.

---

## Money is never a float

A JavaScript number and a Python float are both IEEE-754 binary doubles.
`0.1 + 0.2 != 0.3` in both. For an accounting system that is disqualifying.

Three separate places had to be fixed for this to actually hold:

**1. In Python** — `app/money.py`. Every amount is `decimal.Decimal`.
Conversion from float goes through `repr()`, so `0.1` becomes `Decimal('0.1')`
rather than the binary expansion. Rounding is `ROUND_HALF_UP` (accounting
convention), not banker's rounding.

**2. In the database** — `app/database.py`. SQLAlchemy's stock `Numeric` type
round-trips through `float` on SQLite, silently corrupting money. The `Money`,
`Quantity` and `Rate` column types store the value as a sign-prefixed,
zero-padded string on SQLite (so `ORDER BY` still sorts numerically) and as real
`NUMERIC` on PostgreSQL. A `Decimal` comes back bit-for-bit identical.

**3. Over the API** — `app/schemas/common.py`. Money serialises to a JSON
*string*. A float in JSON would corrupt the value in transit no matter how
careful the two ends were. The frontend parses to integer paise (`lib/money.ts`)
for any arithmetic it does.

```python
>>> money("0.1") + money("0.2") == money("0.3")
True
>>> split_evenly("100.00", 3)
[Decimal('33.33'), Decimal('33.33'), Decimal('33.34')]     # sums to exactly 100.00
```

`split_evenly` and `allocate_proportionally` push all rounding drift onto the
final instalment, so an amortisation schedule can never leak a paisa.

---

## The invariants

Enforced on every posting, without exception:

| # | Invariant | Where |
|---|---|---|
| 1 | Total debits equal total credits, to the paisa | `validate()` and again in `post()` |
| 2 | A line carries a debit **or** a credit — never both, never negative, never zero | `validate()` |
| 3 | Accounts exist, are active, belong to this company, and are postable | `validate()` and `post()` |
| 4 | The target period exists and is `OPEN` | `require_open_period()` |
| 5 | A `POSTED` entry is immutable | `guard_mutation()`, `post()`, `void_draft()` |
| 6 | An entry has at least two lines and a non-zero total | `validate()` |

`verify_invariants()` in `app/accounting/ledger.py` re-checks all of this across
the whole ledger at any time. It is exposed at `GET /accounting/invariants`,
shown live on the Trial Balance screen and on the dashboard, and is one of the
16 month-end close checks.

The three structural checks it adds on top:

- Every posted entry balances internally.
- Ledger-wide debits equal ledger-wide credits.
- **Assets = Liabilities + Equity + (Revenue − Expenses)**

---

## `validate()` is a pure dry-run

```python
report = engine.validate(entry_input)
if report.ok:
    ...  # posting is guaranteed to succeed
```

It touches nothing. It returns *every* problem at once rather than the first
one, so a user (or an agent) fixing an entry sees the full picture. This is what
lets the AI layer prove a proposal would post cleanly *before* a human is asked
to approve it — approving a validated proposal cannot then fail on an
unbalanced entry.

Errors are typed precisely: an out-of-balance entry raises
`UnbalancedJournalError`, a closed period raises `ClosedPeriodError`, a group
account raises `AccountingError`. The frontend reacts to the `code`, not to
prose.

---

## Posted entries are immutable

There is no code path that updates the amount on a posted journal line. A
correction is a **reversal**: an equal and opposite entry, linked to the
original in both directions, with a mandatory reason that becomes part of the
audit trail.

```python
reversal = engine.reverse(entry, reason="Booked to the wrong cost centre")
# original.status              -> REVERSED
# original.reversed_by_entry_id -> reversal.id
# reversal.reverses_entry_id    -> original.id
```

**A subtlety worth stating**, because getting it wrong is easy and the bug is
silent: when an entry is reversed, its *header* is marked `REVERSED` but its
*lines stay `POSTED`*. A reversal works by adding an equal and opposite entry —
both must remain in the ledger so they cancel to zero. Un-posting the original
would remove one side and leave the reversal standing alone, swinging the
balance to the opposite sign instead of back to zero. There is a test for this
(`test_cancelling_an_invoice_reverses_it_rather_than_deleting`), and it caught
exactly that bug during development.

Drafts are different: they are not in the ledger at all, so they can be edited
or voided freely.

---

## The chart of accounts

`app/accounting/coa.py` ships a complete Indian chart of accounts — assets,
liabilities, equity, revenue, COGS and operating expenses, with GST input/output
accounts, TDS/TCS payable, and payroll liabilities.

Accounts are hierarchical. **Group accounts are not postable** — only leaves
are. Group balances are rolled up at read time, never stored.

Automated postings look accounts up by **system key**, not by code:

```python
engine.account_by_system_key("ACCOUNTS_RECEIVABLE")   # not "1131"
```

That means a company can renumber or rename its accounts freely without
breaking invoicing, GST posting or reconciliation.

---

## Periods

One `AccountingPeriod` per calendar month, generated from the company's fiscal
year (April–March by default for India).

Closing a period runs `period_checks()` first, which refuses to close over
unposted entries or an out-of-balance period, and refuses to close a month while
an earlier one is still open. `force=True` overrides — and records *that it was
forced* in the audit trail.

Re-opening requires a reason and must happen newest-first.

---

## What a posting actually looks like

A sales invoice for ₹1,00,000 + 18% GST to an in-state customer, for a stocked
item costing ₹60,000:

```
SV/2025-26/0001  Sales invoice INV/2025-26/0001 to Globex Ltd
  Dr  1131 Trade Receivables        1,18,000.00
      Cr  4110 Product Sales                     1,00,000.00
      Cr  2121 Output CGST                           9,000.00
      Cr  2122 Output SGST                           9,000.00

STK/2025-26/0001  COGS for invoice INV/2025-26/0001
  Dr  5100 Cost of Goods Sold          60,000.00
      Cr  1141 Stock in Hand                       60,000.00
```

Both entries balance. The stock ledger moves at the same moment, and the
inventory control account always equals the sum of the stock ledger — provable
at any time via `POST /inventory/rebuild-valuation`.

**On inventory valuation**: outward movements are valued at the *proportional
share of carrying value*, not quantity × rounded-average-cost. Multiplying by a
2dp average leaves a paisa of residue on every issue, and across hundreds of
movements that becomes a real discrepancy between the stock ledger and the
control account. Taking the share keeps them exactly equal and empties the value
precisely when the last unit leaves.

---

## Voucher types

`SALES`, `PURCHASE`, `PAYMENT`, `RECEIPT`, `CONTRA`, `JOURNAL`, `DEBIT_NOTE`,
`CREDIT_NOTE`, `EXPENSE`, `TRANSFER`, `STOCK_JOURNAL`, `DELIVERY_NOTE`,
`RECEIPT_NOTE`, `PAYROLL`, `DEPRECIATION`, `ACCRUAL`, `PREPAID`, `OPENING`,
`CLOSING`.

Each has its own number sequence, allocated with a row lock so concurrent
requests can never receive the same number (`app/services/numbering.py`).

---

## Reading the ledger

`app/accounting/ledger.py` is the only source of balances. It aggregates
`POSTED` journal lines. **No report reads a cached balance column on an
account**, because a cache that can drift is a liability in an accounting
system.

- `account_balances()` — per-account debit/credit totals over a filter window
- `trial_balance()` — with a `balanced` flag
- `general_ledger()` — running balance with an accurate opening figure
- `day_book()` — all vouchers in a date range
- `verify_invariants()` — the structural checks above

All of them accept the same `LedgerFilters` (dates, branch, cost centre,
department, project), so dimensional reporting behaves consistently everywhere.
