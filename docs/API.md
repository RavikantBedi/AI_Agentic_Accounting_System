# API Reference

Base URL: `http://localhost:8000/api/v1`

Interactive docs, generated from the same Pydantic schemas the server validates
against, are at **<http://localhost:8000/docs>** while the server is running.
That page is authoritative for exact field names; this document explains the
conventions and lists the surface.

---

## Conventions

### Money is a string

```json
{ "subtotal": "100000.00", "tax_total": "18000.00", "total": "118000.00" }
```

Never a JSON number. A float cannot represent `118000.10` exactly, and an
accounting API that sends one has already lost the value before the client
parses it. Send strings too — `"1234.56"` and `1234.56` are both accepted on
input, but only the string form is guaranteed to survive.

### Dates

ISO-8601. Dates are `YYYY-MM-DD`; timestamps are UTC with a `Z`.

### Authentication

```http
Authorization: Bearer <access_token>
```

Obtained from `POST /auth/login`. The token carries the company, so no company
id is passed on other requests — and cannot be overridden by one.

### Pagination

```
?page=1&page_size=50        →  { "items": [...], "total": 531, "page": 1,
                                  "page_size": 50, "pages": 11 }
```

`page_size` is capped server-side.

### Errors

```json
{
  "code": "unbalanced_journal",
  "message": "Debits (118000.00) do not equal credits (100000.00).",
  "details": { "difference": "18000.00", "entry_number": "SV/2025-26/0001" },
  "request_id": "9f2c1a7e"
}
```

| Status | Meaning |
|---|---|
| 400 | Business rule violation — the message says which |
| 401 | Missing, invalid or expired token |
| 403 | Authenticated but not permitted for this role |
| 404 | Not found, or not yours (deliberately indistinguishable) |
| 409 | Conflict — duplicate document, already posted |
| 422 | Request body failed schema validation |
| 429 | Rate limited |
| 500 | Unexpected — `request_id` matches the server log |

Clients should branch on `code`, not on `message`.

---

## Endpoints

### Auth — `/auth`

```
POST   /register            create a company and its first user
POST   /login               → access_token, refresh_token, user, company
POST   /refresh             exchange a refresh token
POST   /logout
POST   /change-password
GET    /me                  current user, role and permissions
```

### Company — `/company`

```
GET    /                    companies for this user
GET    /current
POST   /                    create
PATCH  /current
GET    /current/users             POST   /current/users
PATCH  /current/users/{id}/role   DELETE /current/users/{id}
GET    /current/branches          POST   /current/branches
GET    /current/cost-centers      POST   /current/cost-centers
```

### Accounting — `/accounting`

```
GET    /accounts                     ?type=&search=&postable_only=
POST   /accounts
DELETE /accounts/{id}                deactivates if it has ever been posted to
POST   /accounts/seed-defaults       seed the Indian chart of accounts

GET    /journals                     ?from=&to=&status=&voucher_type=&account_id=
GET    /journals/{id}
POST   /journals                     create (DRAFT unless post=true)
POST   /journals/{id}/post
POST   /journals/{id}/reverse        body: { "reason": "..." }  — required

GET    /trial-balance                ?as_of=  → includes a `balanced` flag
GET    /ledger/{account_id}          ?from=&to=  → running balance
GET    /day-book                     ?date=
GET    /invariants                   the five accounting invariants, live

GET    /periods
GET    /periods/{id}/checks          what would block a close
POST   /periods/{id}/close           ?force=  (a forced close is audited as forced)
POST   /periods/{id}/reopen          body: { "reason": "..." }  — required
POST   /fiscal-years/ensure
```

### Sales — `/sales`

```
GET    /customers                    ?search=&active=
GET    /customers/{id}               includes outstanding balance and aging
POST   /customers

GET    /invoices                     ?status=&customer_id=&from=&to=&overdue=
GET    /invoices/next-number
GET    /invoices/{id}                includes lines, tax breakdown, journal entry
POST   /invoices
POST   /invoices/{id}/post
POST   /invoices/{id}/cancel         reverses the journal; never deletes

GET    /receipts                     POST /receipts    (with allocations)
POST   /credit-notes
POST   /refresh-overdue
```

### Purchases — `/purchases`

```
GET    /vendors                      GET /vendors/{id}      POST /vendors
PATCH  /vendors/{id}/bank-details    audited as a sensitive change

GET    /bills                        GET /bills/{id}
POST   /bills/check-duplicate        check before creating
POST   /bills                        409 on an exact duplicate reference
POST   /bills/{id}/post              POST /bills/{id}/cancel

GET    /payments                     POST /payments
POST   /debit-notes
POST   /refresh-overdue
```

**On duplicates:** the same vendor invoice number for the same vendor is
refused outright — `allow_duplicate` will not override it, because vendor
invoice numbers are unique by definition. `allow_duplicate=true` waives only the
*near*-duplicate warning (same vendor, same amount, close dates, different
reference), which is a legitimate pattern for recurring bills.

### Banking — `/banking`

```
GET    /accounts                     POST /accounts
GET    /transactions                 ?bank_account_id=&status=&from=&to=&search=
POST   /transactions/{id}/categorise
GET    /summary

POST   /accounts/{id}/import/preview  parse and report, write nothing
POST   /accounts/{id}/import          CSV / XLSX / JSON, idempotent

POST   /reconcile                     run the eight matching strategies
GET    /reconciliation/{bank_account_id}
GET    /transactions/{id}/candidates   ranked matches with scored evidence
POST   /matches/{id}/confirm           POST /matches/{id}/reject
POST   /transactions/{id}/unmatch      POST /matches/manual
```

Import is idempotent: re-importing the same statement adds nothing, because
lines are fingerprinted on date, amount, narration and balance.

### Inventory — `/inventory`

```
GET    /items                        GET /items/{id}       POST /items
GET    /summary                      GET /categories       GET /warehouses
GET    /movement-analysis            fast, slow, dead stock
GET    /items/{id}/ledger            every movement with running qty and value
POST   /transfers                    POST /adjustments
POST   /rebuild-valuation            recompute from movement history and report drift
```

`rebuild-valuation` is a verification tool, not a repair hack: it re-derives
every item's valuation from its movement history and reports any item whose
cached figure disagrees. On correct data it reports zero.

### Tax — `/tax`

```
GET    /gst/summary                  ?period=YYYY-MM
GET    /gst/returns
POST   /gst/returns/{type}/prepare   type = gstr1 | gstr3b
GET    /gst/returns/{id}/payload     portal-shaped JSON export
POST   /gst/returns/{id}/mark-filed  record a real ARN from the portal
POST   /gst/reconcile-2b             upload a 2B and reconcile
GET    /gst/reconciliation

GET    /tds/entries                  GET /tds/summary      GET /tds/sections
POST   /tds/entries/{id}/mark-deposited

GET    /providers                    which provider each e-document uses, and its mode
POST   /einvoice/validate            validate the payload without generating
POST   /einvoice/generate            GET /einvoice/{invoice_id}
POST   /ewaybill/generate            GET /ewaybill/{invoice_id}
```

Every e-document response carries `provider_mode`: `MOCK` or `LIVE`. In `MOCK`
the identifier is prefixed `DEMO` and has no legal validity. See
[TAX.md](TAX.md).

### Reports — `/reports`

```
GET    /profit-and-loss              ?from=&to=&compare_from=&compare_to=
GET    /balance-sheet                ?as_of=   → includes a `balanced` flag
GET    /cash-flow                    ?from=&to=&method=indirect
GET    /ar-aging                     GET /ap-aging          ?as_of=
GET    /ratios                       liquidity, leverage, profitability, efficiency
GET    /trend                        ?months=12
GET    /expense-summary              GET /sales-summary     GET /purchase-summary
GET    /budget-variance
GET    /cash-forecast                GET /cash-forecast/all
GET    /dashboard                    everything the overview screen needs, one call
GET    /export/{report}.csv
```

All of them accept the dimensional filters — `branch_id`, `cost_center_id`,
`department_id`, `project_id`.

### AI — `/ai`

```
POST   /ask                          { question } → answer, evidence, tools_used
GET    /suggested-prompts
GET    /conversations                GET /conversations/{id}

GET    /agents                       all 18, with measured accuracy where available
POST   /agents/{kind}/run
GET    /runs                         history with duration, tools, outcomes
GET    /workflows                    POST /workflows/{key}/run
GET    /briefing                     the morning briefing

GET    /proposals                    ?status=&kind=
GET    /proposals/{id}               with grounding and validation results
POST   /proposals/{id}/approve       requires approve_proposal permission
POST   /proposals/{id}/reject        body: { "reason": "..." }

GET    /metrics                      claimed confidence vs measured accuracy
GET    /providers                    which provider is active and why
GET    /tools                        the 22 typed tools and their permissions
```

`POST /ask` returns the tools that produced each figure. There is no endpoint
that accepts SQL, and no endpoint through which AI writes to the ledger —
`approve` is the human's action and requires a human's permission.

### Workspace — `/workspace`

```
GET    /exceptions                   GET /exceptions/summary
POST   /exceptions/{id}/resolve
GET    /anomalies                    POST /anomalies/{id}/review
POST   /anomalies/scan

GET    /audit                        ?entity_type=&entity_id=&user_id=&from=&to=
GET    /audit/timeline/{type}/{id}   everything that ever happened to one record
GET    /audit/verify                 recompute the hash chain; returns the head hash

GET    /close/status                 the 16 checks with evidence
POST   /close/run                    POST /close/tasks/{id}/complete
POST   /close/approve

GET    /settings/policies            PUT /settings/policies/{key}
GET    /settings/approval-rules      GET /settings/permissions

GET    /documents                    POST /documents/upload
GET    /documents/{id}               GET /documents/{id}/text

GET    /search                       ?q=   global search (the Ctrl+K palette)
GET    /notifications                POST /notifications/{id}/read
POST   /notifications/read-all
```

### Demo — `/demo`

```
GET    /status                       is a demo company loaded, and how large
POST   /load                         generate twelve months of activity
POST   /reset                        delete it, in dependency order
GET    /verify                       run every integrity check and report
```

See [DEMO.md](DEMO.md).

---

## A worked example

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@ailedger.local","password":"demo1234"}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# The invariants — the fastest way to know the books are sound
curl -s localhost:8000/api/v1/accounting/invariants -H "Authorization: Bearer $TOKEN"

# Ask a question
curl -s -X POST localhost:8000/api/v1/ai/ask \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"What is our cash position?"}'

# Verify the audit chain
curl -s localhost:8000/api/v1/workspace/audit/verify -H "Authorization: Bearer $TOKEN"
```

---

## Rate limits

`RATE_LIMIT_PER_MINUTE` (default 300) per user, and
`AUTH_RATE_LIMIT_PER_MINUTE` (default 10) on the auth endpoints. Exceeding
either returns 429 with a `Retry-After` header.
