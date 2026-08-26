# Architecture

## The shape of the system

```
                 ┌──────────────────────────────────────────┐
                 │  React SPA — 26 screens, design tokens    │
                 │  TanStack Query · Tailwind · Recharts     │
                 └───────────────────┬──────────────────────┘
                                     │ JSON over HTTP, Bearer token
                                     │ money as strings, never floats
                 ┌───────────────────▼──────────────────────┐
                 │  FastAPI — 12 routers under /api/v1       │
                 │  auth · rate limit · request id · errors  │
                 └───────────────────┬──────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
┌───────▼────────┐        ┌──────────▼─────────┐       ┌──────────▼────────┐
│  AI LAYER      │        │  SERVICE LAYER     │       │  ACCOUNTING CORE  │
│                │        │                    │       │                   │
│ 17 agents      │ reads  │ sales purchases    │ posts │ engine.py         │
│ orchestrator   │ via    │ banking inventory  │ via   │ ledger.py         │
│ typed tools    │ tools  │ tax gst reports    │       │ coa.py            │
│ security.py    ├───────►│ close forecast     ├──────►│ periods.py        │
│ grounding.py   │        │ anomaly policy     │       │                   │
│ providers/     │        │ audit reconcile    │       │ INVARIANTS        │
└────────────────┘        └──────────┬─────────┘       └──────────┬────────┘
   proposes only                     │                            │
                                     ▼                            ▼
                          ┌────────────────────────────────────────────┐
                          │  SQLAlchemy 2 — SQLite or PostgreSQL       │
                          │  Money/Quantity/Rate exact-decimal types   │
                          └────────────────────────────────────────────┘
                                     │
                          ┌──────────▼─────────┐
                          │  AUDIT — hash-chained, append-only │
                          └────────────────────┘
```

Dependencies point one way. The accounting core knows nothing about AI, HTTP or
the UI. Services depend on the core. The AI layer depends on services through a
narrow, typed interface. Nothing depends on the AI layer.

---

## The rule that shapes the design

**AI proposes. Deterministic code decides. Humans approve.**

The forbidden pipeline:

```
USER → LLM → DATABASE            ✗ does not exist in this codebase
```

The actual pipeline:

```
USER
 ↓  natural language, or a scheduled agent run
AI AGENT
 ↓  reads through typed query tools only — no SQL, no ORM access
STRUCTURED PROPOSAL
 ↓  a Pydantic model, not prose
VALIDATION
 ↓  schema, types, ranges, enum membership
GROUNDING
 ↓  every referenced id re-read from the database; unknown ids reject the proposal
ACCOUNTING RULE ENGINE
 ↓  debits = credits · period open · account exists, active, postable
POLICY ENGINE
 ↓  amount thresholds, required role, auto-apply eligibility
HUMAN APPROVAL
 ↓  mandatory for anything that touches the ledger or moves money
ACCOUNTING SERVICE
 ↓  the only writer
DATABASE
 ↓
VERIFICATION
 ↓  invariants re-checked after the write
AUDIT LOG
```

Each stage can only reject or narrow. No stage can widen what a later stage
will accept. That property is what makes the chain trustworthy: an agent cannot
talk its way past the engine, because the engine never reads the agent's words.

---

## Layers in detail

### Accounting core — `app/accounting/`

`engine.py` is the only module allowed to write journal entries. `ledger.py` is
the only module that computes balances, and it computes them by aggregating
posted lines rather than reading a cached column. `coa.py` seeds the Indian
chart of accounts and resolves accounts by stable system key. `periods.py`
manages fiscal periods and the open/closed lifecycle.

See [ACCOUNTING_ENGINE.md](ACCOUNTING_ENGINE.md).

### Service layer — `app/services/`

Business processes. Each service owns its domain and calls the engine to post:

| Service | Responsibility |
|---|---|
| `sales.py` | Invoices, receipts, allocation, credit notes, AR |
| `purchases.py` | Bills, payments, debit notes, AP, duplicate detection |
| `inventory.py` | Items, warehouses, weighted-average valuation, movements |
| `banking.py` | Accounts, statement import, description normalisation |
| `reconciliation.py` | Eight matching strategies with scored evidence |
| `tax.py` | Dated GST/TDS/TCS rate resolution |
| `gst.py` | GSTR-1, GSTR-3B, GSTR-2B reconciliation |
| `reports.py` | P&L, balance sheet, cash flow, aging, ratios |
| `forecast.py` | Cash projection from committed and recurring flows |
| `anomaly.py` | Twelve deterministic detectors |
| `close.py` | Sixteen month-end checks |
| `policy.py` | Approval rules and thresholds, stored as data |
| `audit.py` | Hash-chained append-only event log |
| `documents.py` | Upload handling, extraction, OCR when available |
| `providers.py` | e-Invoice / e-Way Bill provider abstraction |
| `numbering.py` | Row-locked voucher sequences |
| `ai_learning.py` | Measured accuracy from confirmed outcomes |
| `company.py` | Company, users, roles, settings |

### AI layer — `app/ai/`

```
ai/
  providers/    gemini · grok · ollama · mock, behind one interface
  agents/       17 specialists + copilot
  tools/        22 typed query tools — the only read path
  security.py   prompt-injection detection, fencing, redaction
  grounding.py  anti-fabrication verification
  proposals.py  structured proposal types and lifecycle
  orchestrator.py  six multi-agent workflows
```

See [AI_AGENTS.md](AI_AGENTS.md).

### API — `app/api/`

Twelve routers under `/api/v1`: `auth`, `company`, `accounting`, `banking`,
`sales`, `purchases`, `inventory`, `tax`, `reports`, `ai`, `workspace`, `demo`.

See [API.md](API.md).

### Frontend — `frontend/src/`

React with TanStack Query for server state; no client-side store duplicating
server data. `components/ui.tsx` is a single design system used by every screen
— buttons, tables, modals, badges, toasts. `lib/money.ts` does all arithmetic in
integer paise. `index.css` defines design tokens as RGB triplets so dark mode is
a token redefinition rather than a second stylesheet.

---

## The life of a request

`POST /api/v1/sales/invoices`

1. **Middleware** assigns a request id, applies the rate limit, and starts a
   database session bound to the request.
2. **Authentication** decodes the bearer token, loads the user, and resolves the
   active company. A user can only ever see one company's data; company scoping
   is applied in the query, not in the response filter.
3. **Authorisation** checks the required permission for the route against the
   user's role.
4. **Pydantic** parses the body. Money arrives as a string and becomes a
   `Decimal`. An invalid shape returns 422 before any business code runs.
5. **The service** resolves the customer, prices the lines, resolves the tax
   rate as of the invoice date, and computes totals in `Decimal`.
6. **The engine** validates the resulting journal, then posts it. Inventory
   moves in the same transaction.
7. **Audit** appends an event whose hash includes the previous event's hash.
8. **Commit.** One transaction: the invoice, its journal, the stock movement and
   the audit event either all exist or none do.
9. **Serialisation** turns `Decimal` back into strings.

If anything raises, the transaction rolls back and the error handler returns a
structured body: `code`, `message`, `details`, `request_id`.

---

## Data model

Around 90 tables. The load-bearing ones:

```
companies ──┬── users ── roles
            ├── accounts (self-referential tree, system_key)
            ├── accounting_periods
            ├── journal_entries ── journal_lines ──► accounts
            ├── customers ── invoices ── invoice_lines
            │              └─ receipts ── receipt_allocations
            ├── vendors ── bills ── bill_lines
            │            └─ payments ── payment_allocations
            ├── items ── stock_movements ── warehouses
            ├── bank_accounts ── bank_transactions ── reconciliation_matches
            ├── tax_rules · gst_returns · tds_entries · tcs_entries
            ├── einvoice_documents · ewaybill_documents  (provider_mode stamped)
            ├── ai_proposals · ai_decision_log · agent_runs · anomalies
            ├── policies · approval_rules · approvals
            └── audit_events   (hash-chained)
```

Every business table carries `company_id`. Every mutable table carries
`created_at`, `updated_at` and `created_by`.

`journal_lines` has `RESTRICT` foreign keys to `accounts`: an account that has
ever been posted to cannot be deleted, only deactivated. History does not
disappear because someone tidied the chart of accounts.

---

## Transactions and isolation

One request, one transaction. Bulk operations — demo generation, statement
import — wrap each item in a `SAVEPOINT` so a single bad row fails alone rather
than poisoning the session. This was not theoretical: before savepoints, one
rejected document left the session in `PendingRollbackError` and every
subsequent insert in the batch failed for an unrelated reason.

Voucher numbers are allocated under a row lock on the sequence, so two
concurrent invoices cannot receive the same number.

---

## Configuration

`app/config.py`, Pydantic Settings, environment-driven, with defaults that work.
The app starts with an empty environment: SQLite in `./data`, an ephemeral
signing key, the deterministic AI provider, mock regulatory providers.

Production hardening is enforced rather than documented: when
`ENVIRONMENT=production`, startup fails if `SECRET_KEY` is unset, refuses
wildcard CORS, and turns off `DEBUG`.

---

## Why these choices

**FastAPI + Pydantic v2** — the validation layer is a load-bearing part of the
safety architecture, not a convenience. Having it in the type system means the
"structured proposal" stage is real code rather than a convention.

**SQLAlchemy 2 with a custom decimal type** — the ORM's portability matters
because the same code must run on SQLite for a laptop and PostgreSQL for a
server. The custom type exists because the stock one silently loses money on
SQLite.

**SQLite by default** — the product must run locally with no services to
install. PostgreSQL is one environment variable away.

**React + TanStack Query, no Redux** — server state is server state. Caching and
invalidation belong in the query layer; duplicating it into a client store is
how stale financial figures get shown to users.

**Recharts** — small, declarative, works with the token system, no licence cost.

**No paid dependency anywhere** — every component is free and open-source, and
the whole system runs offline.
