<div align="center">

# AI Ledger — AI-Native Accounting System

### Your books, managed by AI. From transaction to insight — automated, verified, and always under your control.

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Node" src="https://img.shields.io/badge/Node-18%2B-339933?logo=node.js&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black">
  <img alt="License" src="https://img.shields.io/badge/License-add%20before%20distribution-lightgrey">
  <img alt="Self-hosted" src="https://img.shields.io/badge/Self--hosted-No%20paid%20service-brightgreen">
</p>

**An AI-native accounting & business-management platform for Indian businesses.**
A real double-entry accounting engine with a layer of AI agents that read, classify,
match, detect and explain — but **never post, pay or approve**.

`v1.0.0` · Python 3.10+ · Node 18+ · SQLite or PostgreSQL · self-hosted · no paid dependency

**~346 automated checks** — 177 unit · 114 HTTP · 55 browser

</div>

---

## 📑 Table of Contents

- [The One Idea That Shapes Everything](#-the-one-idea-that-shapes-everything)
- [Features](#-features)
- [Quick Start (Local)](#-quick-start-local)
- [Deploy to Production](#-deploy-to-production)
- [Production Readiness](#-production-readiness)
- [What It Deliberately Does *Not* Do](#-what-it-deliberately-does-not-do)
- [AI Providers](#-ai-providers)
- [Verifying a Build](#-verifying-a-build)
- [Operations & Monitoring](#-operations--monitoring)
- [Upgrading a Deployment](#-upgrading-a-deployment)
- [Troubleshooting](#-troubleshooting)
- [Documentation](#-documentation)
- [Tech Stack](#-tech-stack)
- [Project Layout](#-project-layout)
- [Security & Disclaimer](#-security--disclaimer)

---

## 💡 The One Idea That Shapes Everything

> **The accounting engine is the source of truth. AI is an automation layer on top of it.**

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

There is **no code path from a language model to a database write.** That is not a
policy setting — it is how the system is built, and there are tests that fail if
it ever stops being true.

---

## ✨ Features

| Area | What is implemented |
|---|---|
| **📒 Accounting** | Double-entry engine, chart of accounts, 20 voucher types, fiscal periods, immutable posted entries, reversal-based corrections |
| **🏦 Banking** | Bank accounts, CSV/XLSX/JSON statement import (idempotent), 8-strategy reconciliation with evidence |
| **🧾 Sales** | Customers, invoices, receipts with allocation, credit notes, AR aging |
| **🛒 Purchases** | Vendors, bills, payments, debit notes, AP aging, duplicate detection |
| **📦 Inventory** | Items, warehouses, weighted-average valuation, transfers, adjustments, movement analysis |
| **🧮 Tax** | GST (CGST/SGST/IGST/UTGST/cess), GSTR-1 & GSTR-3B preparation, GSTR-2B reconciliation, TDS with dated thresholds, TCS |
| **📄 e-Documents** | e-Invoice and e-Way Bill payload building & validation via a provider abstraction |
| **📊 Reporting** | P&L, balance sheet, cash flow, trial balance, general ledger, aging, ratios, variance, budget |
| **🔒 Close** | 16 evidence-backed month-end checks, blocking-issue enforcement, period locking |
| **🤖 AI** | 17 specialist agents + a copilot, an orchestrator with 6 workflows, anomaly & fraud detection, cash forecasting |
| **🛡️ Controls** | RBAC across 8 roles, configurable approval thresholds, hash-chained audit trail |

---

## 🚀 Quick Start (Local)

**Requires only Python 3.10+ and Node 18+.** No API key, no paid service, no cloud account.
Everything works fully offline with the built-in deterministic AI provider.

<details open>
<summary><b>Windows (PowerShell)</b> — run each line separately, <code>&&</code> is not valid in PowerShell 5.1</summary>

```powershell
# Terminal 1 — backend
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

```powershell
# Terminal 2 — frontend
cd frontend
npm install
npm run dev
```
</details>

<details>
<summary><b>macOS / Linux (bash)</b></summary>

```bash
# Terminal 1 — backend
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --port 8000
```

```bash
# Terminal 2 — frontend
cd frontend
npm install
npm run dev
```
</details>

**Then:**
1. Open <http://localhost:5173>
2. Click **Load demo company**
3. Sign in as `demo@ailedger.local` / `demo1234`

> The demo generates **twelve months of real activity** — 112 customers, 107 vendors,
> 532 invoices, 336 bills, 2,077 journal entries, 676 bank lines, ~13,700 records —
> every one posted through the actual accounting engine. The books balance because
> they were genuinely kept. See [DEMO.md](docs/DEMO.md).
>
> **Remove the demo before entering real data:** `POST /api/v1/demo/reset`.

---

## 🌐 Deploy to Production

Full instructions in **[DEPLOYMENT.md](docs/DEPLOYMENT.md)**. The short path:

> **Never put a real value in `.env.example`** — it is a committed template. The file
> you edit is `backend/.env`, which is git-ignored.

```bash
# 1. Configure (macOS / Linux)
cp .env.example backend/.env
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))" >> backend/.env
```

```powershell
# 1. Configure (Windows PowerShell)
Copy-Item .env.example backend\.env
# Do NOT use '>>' here: PowerShell 5.1 writes UTF-16, which corrupts the file.
# Generate the key, then paste it into the SECRET_KEY= line in backend\.env:
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

```bash
# 2. Build & serve
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements.txt

cd ../frontend
npm ci
npm run build                    # → frontend/dist

cd ../backend
# --workers 4 assumes PostgreSQL; on SQLite use --workers 1
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4
```

Put nginx (or any reverse proxy) in front to terminate TLS and serve `frontend/dist`.
Reference `systemd`, `nginx` and `Dockerfile` configurations are in
[DEPLOYMENT.md](docs/DEPLOYMENT.md).

> ⚠️ **Concurrency facts to know before picking a worker count:**
> - **SQLite serialises writers** — more than one worker on SQLite will time out under write load.
> - The built-in **rate limiter is per-process** — N workers ≈ N× the configured limit.
>
> Both are covered (with an nginx `limit_req` config) under *Workers and concurrency* in
> [DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Misconfiguration is a startup failure, not a warning

With `ENVIRONMENT=production` the app **refuses to start** if `SECRET_KEY` is unset,
`DEBUG` is true, or `CORS_ORIGINS` is `*`. An unset `SECRET_KEY` in particular means a
per-process key that signs every user out on every restart — a failure easy to
misdiagnose for days. Catch it before the first request.

---

## ✅ Production Readiness

Honest status, so nothing is discovered at the wrong moment.

### Ready

- **Money is exact.** `Decimal` end to end — Python, database, and as strings over JSON. No float ever touches an amount.
- **The ledger is immutable.** Posted entries are never updated; corrections are reversals. Enforced in the engine, not by convention.
- **Invariants are checked, not assumed.** Five accounting invariants verifiable anytime from the UI or `GET /accounting/invariants`.
- **The audit trail is hash-chained** with no update or delete code path.
- **Security is enforced at startup and per request** — see [SECURITY.md](docs/SECURITY.md).
- **PostgreSQL supported** with identical application code.
- **Runs fully offline.** No mandatory external service.
- **Performance:** read endpoints under 60 ms at demo volume; the dashboard aggregate ~200 ms for what would otherwise be eight round trips.

### Do this before real data goes in

- **Generate an Alembic baseline.** The schema is created from models on startup; until you run `alembic revision --autogenerate` once, treat the database as disposable.
- **Verify the seeded tax rates** against the current Finance Act. They are dated, versioned configuration — not legal advice ([TAX.md](docs/TAX.md)).
- **Test a restore**, not just a backup. Then run the integrity endpoints against the restored copy.
- **Export the audit chain's head hash** somewhere outside the database — it proves nobody rewrote the entire chain.
- **Replace the demo login** with real accounts and roles.

Full checklist in [DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## 🚫 What It Deliberately Does *Not* Do

Being clear about this is part of the product.

- **It does not file GST returns.** It prepares GSTR-1 and GSTR-3B from your ledger and exports portal-shaped JSON. You file on the GST portal and record the ARN here.
- **e-Invoice and e-Way Bill run in demo mode** unless you configure real IRP credentials. Demo IDs are prefixed `DEMO`, stored with `provider_mode = MOCK`, and labelled as having no legal validity. With credentials configured, the live client **raises an error rather than fabricating an IRN** — a fallback that is deliberately absent.
- **It does not connect to your bank.** Statements are imported from files. The provider abstraction exists for a future integration; there is no pretend one.
- **It does not claim full statutory payroll compliance.** Payroll accounting (components, deductions, payslips, the journal) is implemented. PF/ESI/PT filing is not.

---

## 🤖 AI Providers

Every feature works with no language model configured. Providers are tried in order:

**Gemini → Grok → Ollama → Mock**

The **Mock** provider is deterministic, always available, and genuinely useful: it reads
the structured context an agent supplies and answers from *that data only*. It cannot
invent an account code or invoice number, and when the context does not support an
answer it says so. Configure a real provider for more fluent wording — **the numbers do
not change.**

```bash
# backend/.env — all optional
LLM_PROVIDER=auto
GEMINI_API_KEY=
GROK_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1
```

Keys are read server-side only — never sent to the frontend, never logged, never in an
error response. A test plants a realistic-looking key and asserts it appears in no
response body.

---

## 🧪 Verifying a Build

```bash
cd backend
.venv/bin/python -m pytest                # 177 unit tests
.venv/bin/python scripts/verify_api.py    # 114 HTTP checks — needs a running server

cd ../frontend
node e2e/ui.spec.mjs                      # 55 browser checks — needs both servers
```

> On Windows use `.venv\Scripts\python` and run each line separately (no `&&`).

The three layers check different things. Unit tests check behaviour in process.
`verify_api.py` drives a **running server** over real HTTP (catching serialisation and
routing faults — including whether money survives the JSON boundary) and doubles as a
post-deployment smoke test. `ui.spec.mjs` drives a real browser through all 26 screens
and fails on a console error, a horizontal scrollbar at 375 px, or a missing `<h1>`.

| Test file | What it guards |
|---|---|
| `test_accounting_engine.py` | The invariants — if these fail, the product is broken |
| `test_money_and_tax.py` | Decimal exactness, GST splits, TDS thresholds |
| `test_workflows.py` | Sales, purchases, inventory, banking end to end |
| `test_ai_safety.py` | Prompt injection, grounding, hallucination, AI permission limits |
| `test_security_and_api.py` | Auth, RBAC, file uploads, SQL injection, the HTTP contract |

---

## 📈 Operations & Monitoring

```
GET /health                          liveness; reports database and AI provider status
GET /api/v1/accounting/invariants    the five accounting invariants
GET /api/v1/workspace/audit/verify   recompute the audit hash chain
GET /api/v1/demo/verify              six integrity checks over the books
```

The last three are worth alerting on. A failing invariant means the books are wrong —
more urgent than any latency metric.

Every response carries `X-Request-Id` and `X-Response-Time-Ms`; the request id appears in
the server log and in every error body, so a user-reported problem traces back to an
exact request.

Rate limits are per user and per IP (`RATE_LIMIT_PER_MINUTE` default 300;
`AUTH_RATE_LIMIT_PER_MINUTE` default 10), enforced in-process. Two things silently
defeat them: **not forwarding `X-Forwarded-For`** behind a proxy, and **multiple
workers** (per-process buckets ≈ N× the limit). Enforce at the proxy or run one worker.

Back up the database and `STORAGE_DIR`. For SQLite use `.backup`, not `cp` — a live file
copy can capture a torn write.

---

## ⬆️ Upgrading a Deployment

```bash
# 1. Back up first — database and STORAGE_DIR
sqlite3 /data/ailedger.db ".backup '/backups/pre-upgrade.db'"     # or pg_dump -Fc

# 2. Pull, install, rebuild
git pull
cd backend && .venv/bin/pip install -r requirements.txt && cd ..
cd frontend && npm ci && npm run build && cd ..

# 3. Apply schema changes (only once you have an Alembic baseline)
cd backend && .venv/bin/alembic upgrade head && cd ..

# 4. Restart, then verify
sudo systemctl restart ailedger
cd backend && .venv/bin/python scripts/verify_api.py
```

Step 4 is the point: `verify_api.py` re-checks the accounting invariants, the audit
chain and the HTTP contract against the live server, so an upgrade that broke something
is caught in seconds rather than at month-end.

---

## 🧯 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Refusing to start with an unsafe configuration` | `SECRET_KEY` unset, `DEBUG=true`, or `CORS_ORIGINS=*` under `ENVIRONMENT=production` | The log names the exact problem; fix it in `backend/.env` |
| Everyone signed out after a restart | `SECRET_KEY` unset, so a new key is generated per process | Set `SECRET_KEY` to a fixed 48+ byte value |
| `database is locked` / write timeouts | Multiple workers on SQLite | Run one worker, or move to PostgreSQL |
| Rate limiting never triggers | Multiple workers, or `X-Forwarded-For` not passed | Enforce `limit_req` at the proxy; forward the client IP |
| 401 on every request | Clock skew, or a token issued under a previous `SECRET_KEY` | Check the system clock; sign in again |
| CORS errors in the browser | `CORS_ORIGINS` does not match the exact scheme and host | Match the origin exactly, including `https://` |
| Upload rejected with 422 | The file's leading bytes do not match an allowed type | Extensions are not trusted — the real type is checked |
| Uploads fail at ~1 MB | nginx `client_max_body_size` below `MAX_UPLOAD_BYTES` | Raise it above 25 MB |
| Document text not extracted | OCR/PDF extras not installed | `pip install pytesseract pdfplumber pymupdf`, or accept the honest "could not read" result |
| AI answers are terse | No provider configured; the deterministic provider is answering | Expected. Set `GEMINI_API_KEY` or run Ollama — the numbers do not change |
| Invariant check fails | The books are genuinely inconsistent | Stop and investigate before posting further; check `GET /workspace/audit` |

Every error response carries a `request_id` matching the server log — start there.

---

## 📚 Documentation

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

Configuration reference: [`.env.example`](.env.example) — every value optional; the app
runs with an empty environment. Interactive API docs are served at `/docs` from the same
schemas the server validates against.

---

## 🛠️ Tech Stack

Free and open-source throughout — no paid database, vector store, workflow engine,
analytics, auth or hosting.

- **Backend** — Python, FastAPI, Pydantic v2, SQLAlchemy 2, pandas, rapidfuzz, PyMuPDF/pdfplumber/Tesseract (optional OCR)
- **Frontend** — React 19, Vite, TypeScript, Tailwind CSS, Recharts, TanStack Query
- **Database** — SQLite locally, PostgreSQL in production (same code)
- **AI** — Gemini, Grok, Ollama, or the built-in deterministic provider

---

## 📁 Project Layout

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

## 🔐 Security & Disclaimer

Open an issue describing the impact and how to reproduce any vulnerability. **Do not
include real credentials or real company data in the report.** The threat model, and
what the audit chain does and does not prove, are in [SECURITY.md](docs/SECURITY.md).

**Disclaimer.** No licence file is included; add one before distributing. This software
does not provide tax, accounting or legal advice. Tax rates and thresholds ship as
editable, dated configuration and must be verified against current law before use in any
statutory filing. You remain responsible for the accuracy of your books and returns.