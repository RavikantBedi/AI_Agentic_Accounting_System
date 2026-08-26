# AI Ledger

**Your books, managed by AI.**
*From transaction to financial insight — automated, verified, and always under your control.*

An AI-native accounting and business-management platform for Indian businesses.
A real double-entry accounting engine with a layer of AI agents that read,
classify, match, detect and explain — but never post, pay or approve.

`v1.0.0` · Python 3.10+ · Node 18+ · SQLite or PostgreSQL · self-hosted · no paid dependency

**346 automated checks passing** — 177 unit · 114 HTTP · 55 browser

---

## The one idea that shapes everything

**The accounting engine is the source of truth. AI is an automation layer on top of it.**

```
USER
 ↓
AI AGENT              proposes — reads data through typed queries only
 ↓
PYDANTIC VALIDATION   the proposal must be structurally valid
 ↓
GROUNDING             every id it references is re-read from the database
 ↓
ACCOUNTING ENGINE     debits = credits, period open, account postable
 ↓
POLICY ENGINE         who may approve this, and may it auto-apply at all
 ↓
HUMAN APPROVAL        for anything that touches the ledger or moves money
 ↓
DATABASE
 ↓
AUDIT LOG             append-only, hash-chained
```

There is no code path from a language model to a database write. That is not a
policy setting — it is how the system is built, and there are tests that fail if
it ever stops being true.

---

## Deploy

Full instructions in **[DEPLOYMENT.md](docs/DEPLOYMENT.md)**. The short path:

**Never put a real value in `.env.example`.** It is a committed template; the
file you edit is `backend/.env`, which is git-ignored.

```bash
# 1. Configure  (macOS / Linux)
cp .env.example backend/.env
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))" >> backend/.env
```

```powershell
# 1. Configure  (Windows PowerShell)
Copy-Item .env.example backend\.env
# Do NOT use '>>' here: Windows PowerShell 5.1 writes UTF-16, which corrupts
# the file. Generate the key, then paste it into the SECRET_KEY= line.
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then set these in `backend/.env`:

```bash
ENVIRONMENT=production
DEBUG=false
SECRET_KEY=<the value generated above>
DATABASE_URL=postgresql+psycopg://ailedger:...@localhost:5432/ailedger
CORS_ORIGINS=https://books.yourcompany.com
```

Build and serve:

```bash
# Install the backend
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# Build the frontend
cd ../frontend
npm ci
npm run build                    # → frontend/dist

# Serve  (--workers 4 assumes PostgreSQL; on SQLite use --workers 1)
cd ../backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4
```

Put nginx (or any reverse proxy) in front to terminate TLS and serve
`frontend/dist`. Reference `systemd`, `nginx` and `Dockerfile` configurations are
in [DEPLOYMENT.md](docs/DEPLOYMENT.md).

Two concurrency facts worth knowing before you pick a worker count: **SQLite
serialises writers**, so more than one worker on SQLite will start timing out
under write load; and the built-in **rate limiter is per-process**, so N workers
means roughly N× the configured limit. Both are covered, with an nginx
`limit_req` configuration, under *Workers and concurrency* in
[DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Misconfiguration is a startup failure, not a warning

With `ENVIRONMENT=production` the application **refuses to start** if
`SECRET_KEY` is unset, `DEBUG` is true, or `CORS_ORIGINS` is `*`. Those are the
three mistakes that cost the most, and the cheapest moment to catch them is
before the first request.

An unset `SECRET_KEY` in particular means a key generated per process, which
signs every user out on every restart — a failure that is easy to misdiagnose
for days.

---

## Evaluate locally first

Requires **Python 3.10+** and **Node 18+**. Nothing else. No API key, no paid
service, no cloud account.

**Windows (PowerShell)** — note that `&&` is not a valid separator in Windows
PowerShell 5.1; run each line on its own.

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

```powershell
# second terminal
cd frontend
npm install
npm run dev
```

**macOS / Linux**

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --port 8000
```

```bash
# second terminal
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>, click **Load demo company**, sign in as
`demo@ailedger.local` / `demo1234`.

The demo generates twelve months of real activity — 112 customers, 107 vendors,
532 invoices, 336 bills, 2,077 journal entries, 676 bank lines, about 13,700
transactional records — every one posted through the actual accounting engine.
It is not a fixture file of pretty numbers; the books balance because they were
genuinely kept. See [DEMO.md](docs/DEMO.md).

**Remove the demo company before entering real data:** `POST /api/v1/demo/reset`.

---

## What it does

| Area | What is implemented |
|---|---|
| **Accounting** | Double-entry engine, chart of accounts, 20 voucher types, fiscal periods, immutable posted entries, reversal-based corrections |
| **Banking** | Bank accounts, CSV/XLSX/JSON statement import (idempotent), 8-strategy reconciliation with evidence |
| **Sales** | Customers, invoices, receipts with allocation, credit notes, AR aging |
| **Purchases** | Vendors, bills, payments, debit notes, AP aging, duplicate detection |
| **Inventory** | Items, warehouses, weighted-average valuation, transfers, adjustments, movement analysis |
| **Tax** | GST (CGST/SGST/IGST/UTGST/cess), GSTR-1 and GSTR-3B preparation, GSTR-2B reconciliation, TDS with dated thresholds, TCS |
| **e-Documents** | e-Invoice and e-Way Bill payload building and validation, through a provider abstraction |
| **Reporting** | P&L, balance sheet, cash flow, trial balance, general ledger, aging, ratios, variance, budget |
| **Close** | 16 evidence-backed month-end checks, blocking-issue enforcement, period locking |
| **AI** | 17 specialist agents + a copilot, an orchestrator with 6 workflows, anomaly and fraud detection, cash forecasting |
| **Controls** | RBAC across 8 roles, configurable approval thresholds, hash-chained audit trail |

---

## Production readiness

Honest status, so nothing is discovered at the wrong moment.

### Ready

- **Money is exact.** `Decimal` end to end — in Python, in the database, and as
  strings over JSON. A float never touches an amount.
- **The ledger is immutable.** Posted entries are never updated; corrections are
  reversals. Enforced in the engine, not by convention.
- **Invariants are checked, not assumed.** Five accounting invariants verifiable
  at any time from the UI or `GET /accounting/invariants`.
- **The audit trail is hash-chained** and has no update or delete code path.
- **Security is enforced at startup and per request** — see [SECURITY.md](docs/SECURITY.md).
- **PostgreSQL supported** with identical application code.
- **Runs fully offline.** No mandatory external service of any kind.
- **Performance:** read endpoints under 60 ms at demo volume; the dashboard
  aggregate ~200 ms for what would otherwise be eight round trips.

### Do this before real data goes in

- **Generate an Alembic baseline.** There are no migration scripts in this
  repository — the schema is created from the models on startup, so a schema
  change today means recreating the database. Alembic is already a dependency;
  run `alembic revision --autogenerate` once, before your first real record.
  Until then, treat the database as disposable.
- **Verify the seeded tax rates** against the current Finance Act. They are
  dated, versioned configuration — not legal advice. See [TAX.md](docs/TAX.md).
- **Test a restore**, not just a backup. Then run the integrity endpoints against
  the restored copy; they will tell you immediately whether the books came back
  whole.
- **Export the audit chain's head hash** somewhere outside the database. The
  chain proves nobody edited a past event; an external head hash also proves
  nobody rewrote the entire chain.
- **Replace the demo login** with real accounts and appropriate roles.

Full checklist in [DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## What it deliberately does **not** do

Being clear about this is part of the product.

- **It does not file GST returns.** It prepares GSTR-1 and GSTR-3B from your
  ledger and exports portal-shaped JSON. You file on the GST portal and record
  the ARN here.
- **e-Invoice and e-Way Bill run in demo mode** unless you configure real IRP
  credentials. Demo identifiers are prefixed `DEMO`, stored with
  `provider_mode = MOCK`, and labelled as having no legal validity everywhere
  they appear. With credentials configured, the live client **raises an error
  rather than fabricating an IRN** — that fallback would be easy to write and is
  deliberately absent.
- **It does not connect to your bank.** Statements are imported from files. The
  provider abstraction exists for a future integration; there is no pretend one.
- **It does not claim full statutory payroll compliance.** Payroll accounting —
  components, deductions, payslips, the journal — is implemented. PF/ESI/PT
  filing is not.

---

## Operations

```
GET /health                          liveness; reports database and AI provider status
GET /api/v1/accounting/invariants    the five accounting invariants
GET /api/v1/workspace/audit/verify   recompute the audit hash chain
GET /api/v1/demo/verify              six integrity checks over the books
```

The last three are the ones worth alerting on. A failing invariant means the
books are wrong, which is more urgent than any latency metric this system could
report.

Every response carries `X-Request-Id` and `X-Response-Time-Ms`. The request id
appears in the server log and in every error body, so a user-reported problem
traces back to an exact request.

Rate limits are per user and per IP (`RATE_LIMIT_PER_MINUTE`, default 300;
`AUTH_RATE_LIMIT_PER_MINUTE`, default 10), enforced in-process. Two things will
silently defeat them:

- **Not forwarding the client IP.** Behind a proxy, pass `X-Forwarded-For` —
  without it every request appears to come from the proxy and the per-IP limit
  becomes a single global one.
- **Multiple workers.** The limiter keeps its buckets in memory per process, so
  four workers allow roughly four times the configured rate. Enforce the limit
  at the proxy instead, or run one worker per instance.

Back up the database and `STORAGE_DIR`. For SQLite use `.backup`, not `cp` — a
live file copy can capture a torn write.

---

## Upgrading a running deployment

```bash
# 1. Back up first — database and STORAGE_DIR
sqlite3 /data/ailedger.db ".backup '/backups/pre-upgrade.db'"     # or pg_dump -Fc

# 2. Pull, install, rebuild
git pull
cd backend
.venv/bin/pip install -r requirements.txt
cd ../frontend
npm ci
npm run build

# 3. Apply schema changes (only once you have an Alembic baseline)
cd ../backend
.venv/bin/alembic upgrade head

# 4. Restart, then verify
sudo systemctl restart ailedger
.venv/bin/python scripts/verify_api.py
```

Step 4 is the point of the exercise. `verify_api.py` re-checks the accounting
invariants, the audit chain and the HTTP contract against the live server, so an
upgrade that broke something is caught in seconds rather than at month-end.

Until an Alembic baseline exists, a release that changes the schema requires
recreating the database — which is why generating one before your first real
record matters.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Refusing to start with an unsafe configuration` | `SECRET_KEY` unset, `DEBUG=true`, or `CORS_ORIGINS=*` under `ENVIRONMENT=production` | The log line names the exact problem; fix it in `backend/.env` |
| Everyone signed out after a restart | `SECRET_KEY` unset, so a new key is generated per process | Set `SECRET_KEY` to a fixed 48+ byte value |
| `database is locked` / write timeouts | Multiple workers on SQLite | Run one worker, or move to PostgreSQL |
| Rate limiting never triggers | Multiple workers (per-process buckets), or `X-Forwarded-For` not passed | Enforce `limit_req` at the proxy; forward the client IP |
| 401 on every request | Clock skew, or a token issued under a previous `SECRET_KEY` | Check the system clock; sign in again |
| CORS errors in the browser | `CORS_ORIGINS` does not include the exact scheme and host | Match the origin exactly, including `https://` |
| Upload rejected with 422 | The file's leading bytes do not match an allowed type | Extensions are not trusted — the real type is checked |
| Uploads fail at ~1 MB | nginx `client_max_body_size` below `MAX_UPLOAD_BYTES` | Raise it above 25 MB |
| Document text not extracted | OCR/PDF extras not installed | `pip install pytesseract pdfplumber pymupdf`, or accept the honest "could not read" result |
| AI answers are terse | No provider configured; the deterministic provider is answering | Expected. Set `GEMINI_API_KEY` or run Ollama — the numbers do not change |
| Invariant check fails | The books are genuinely inconsistent | Stop and investigate before posting anything further; check `GET /workspace/audit` for what changed |

Every error response carries a `request_id` that matches the server log, so
start there.

---

## AI providers

Every feature works with no language model configured. Providers are tried in
order:

**Gemini → Grok → Ollama → Mock**

The Mock provider is deterministic, always available, and genuinely useful: it
reads the structured context an agent supplies and answers from *that data
only*. It cannot invent an account code or an invoice number, and when the
context does not support an answer it says so.

This works because the agents are built the right way round: deterministic
engines do the accounting, and the model is used for classification and
phrasing. Configure a real provider for more fluent wording — **the numbers do
not change.**

```bash
# backend/.env — all optional
LLM_PROVIDER=auto
GEMINI_API_KEY=
GROK_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1
```

Keys are read server-side only. They are never sent to the frontend, never
logged, and never included in an error response — there is a test that plants a
realistic-looking key and asserts it appears in no response body.

---

## Verifying a build

```bash
cd backend
.venv/bin/python -m pytest                # 177 unit tests
.venv/bin/python scripts/verify_api.py    # 114 HTTP checks — needs a running server

cd ../frontend
node e2e/ui.spec.mjs                      # 55 browser checks — needs both servers
```

On Windows use `.venv\Scripts\python` and run each line separately; Windows
PowerShell 5.1 has no `&&`.

The three layers check different things. Unit tests check behaviour in process.
`verify_api.py` drives a **running server** over real HTTP, so it catches
serialisation and routing faults that in-process tests cannot see — including
whether money survives the JSON boundary. It doubles as a post-deployment smoke
test: point `BASE` at the deployed URL. `ui.spec.mjs` drives a real browser
through all 26 screens and fails on a console error, a horizontal scrollbar at
375 px, or a missing `<h1>`.

The suite is organised around the properties that matter:

- `test_accounting_engine.py` — the invariants. If these fail, the product is broken.
- `test_money_and_tax.py` — decimal exactness, GST splits, TDS thresholds.
- `test_workflows.py` — sales, purchases, inventory, banking, end to end.
- `test_ai_safety.py` — prompt injection, grounding, hallucination, AI permission limits.
- `test_security_and_api.py` — auth, RBAC, file uploads, SQL injection, the HTTP contract.

A running instance can also be checked from inside the app: **Trial Balance**
shows the five invariants live, **Audit Log** recomputes the hash chain on
demand, and **Inventory** re-derives every item's valuation from its movement
history.

---

## Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, module layout, request lifecycle |
| [ACCOUNTING_ENGINE.md](docs/ACCOUNTING_ENGINE.md) | The double-entry core and its invariants |
| [AI_AGENTS.md](docs/AI_AGENTS.md) | All 17 agents, the orchestrator, and the safety model |
| [SECURITY.md](docs/SECURITY.md) | Threat model, RBAC, prompt-injection defence, audit integrity |
| [TAX.md](docs/TAX.md) | GST, TDS, TCS, e-invoicing — and their exact scope |
| [API.md](docs/API.md) | Endpoint reference and conventions |
| [DEMO.md](docs/DEMO.md) | What the demo company contains and what to try |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Local, systemd, Docker and PostgreSQL deployment |

Configuration reference: [`.env.example`](.env.example) — every value optional;
the application runs with an empty environment.

Interactive API docs are served at `/docs` from the same schemas the server
validates against.

---

## Tech stack

Free and open-source throughout. No mandatory paid service of any kind — no paid
database, vector store, workflow engine, analytics, auth or hosting.

**Backend** — Python, FastAPI, Pydantic v2, SQLAlchemy 2, pandas, rapidfuzz,
PyMuPDF/pdfplumber/Tesseract (optional OCR)
**Frontend** — React 19, Vite, TypeScript, Tailwind CSS, Recharts, TanStack Query
**Database** — SQLite locally, PostgreSQL in production (same code)
**AI** — Gemini, Grok, Ollama, or the built-in deterministic provider

---

## Project layout

```
backend/
  app/
    accounting/   engine, chart of accounts, periods, ledger
    services/     sales, purchases, inventory, banking, reconciliation,
                  tax, gst, reports, forecast, anomaly, close, policy, audit
    ai/
      providers/  gemini, grok, ollama, mock
      agents/     17 specialists + copilot
      tools/      typed query tools — the only way AI reads data
      security.py prompt-injection defence
      grounding.py anti-fabrication verification
      orchestrator.py
    models/       SQLAlchemy models
    api/          FastAPI routers
    demo/         synthetic company generator
  scripts/        verify_api.py — HTTP verification against a running server
  tests/
frontend/
  src/
    components/   design system, layout
    pages/        26 screens
    lib/          API client, money, hooks
  e2e/            browser suite
docs/
```

---

## Reporting a vulnerability

Open an issue describing the impact and how to reproduce it. Please do not
include real credentials or real company data in the report.

The threat model, and what the audit chain does and does not prove, are set out
in [SECURITY.md](docs/SECURITY.md).

---

## Licence and disclaimer

No licence file is included; add one before distributing.

This software does not provide tax, accounting or legal advice. Tax rates and
thresholds ship as editable, dated configuration and must be verified against
current law before use in any statutory filing. You remain responsible for the
accuracy of your books and returns.
