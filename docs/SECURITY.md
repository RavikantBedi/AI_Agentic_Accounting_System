# Security

An accounting system holds the record that everything else is reconciled
against. The threats that matter are not only "can someone steal the data" but
"can someone change the record without leaving a trace", and — because this
product has a language model in it — "can text inside a document make the system
act".

---

## Threat model

| Threat | Defence |
|---|---|
| Credential theft | PBKDF2-HMAC-SHA256, 240k iterations, per-user salt; generic login errors; auth rate limit |
| Session theft | Short-lived access tokens, separate refresh tokens, signature verified on every request |
| Privilege escalation | Permission checked per route against the user's role; roles are data, not code branches |
| Cross-company data access | `company_id` applied in the query, never as a response filter |
| SQL injection | Parameterised statements only; no string-built SQL anywhere; no NL-to-SQL path |
| Prompt injection | Nonce fencing, 18 detection patterns, unicode normalisation, structural containment |
| AI data exfiltration | Typed tools with row limits; secrets redacted from prompts and logs |
| AI fabrication | Grounding re-reads every referenced id from the database |
| Unauthorised ledger change | Posted entries immutable; corrections only by reversal |
| Silent tampering | Hash-chained append-only audit trail, verifiable on demand |
| Malicious upload | Type identified by magic bytes against an allow-list (extension untrusted), size cap, randomised stored filename |
| Denial of service | Per-IP and per-user rate limits, pagination caps, query row limits |
| Secret leakage | Keys server-side only, never serialised, redacted from logs and errors |

---

## Authentication

Passwords are hashed with **PBKDF2-HMAC-SHA256**, 240,000 iterations, a
16-byte per-user random salt, stored as `pbkdf2_sha256$iterations$salt$hash`.
Verification is constant-time (`hmac.compare_digest`).

PBKDF2 rather than argon2 or bcrypt for one deliberate reason: it is in the
Python standard library. Both alternatives need a native build step, which is
the most common reason a "just run it locally" project fails to start on a
machine without a compiler. The iteration count is set high enough to be a real
cost, and the algorithm and its parameters are stored per-hash so they can be
raised later without invalidating existing passwords.

Tokens are HMAC-SHA256-signed, carry `sub`, `company_id`, `role`, `exp` and a
type discriminator, and are verified — signature *and* expiry — on every
request. A refresh token cannot be used as an access token.

`SECRET_KEY` is generated per process if unset, which means restarting the
server signs everyone out. In `ENVIRONMENT=production` the app refuses to start
without one, rather than silently generating a key that changes on every deploy.

Login is rate-limited separately and more tightly than the rest of the API
(`AUTH_RATE_LIMIT_PER_MINUTE`, default 10/minute), and returns the same message
for an unknown email and a wrong password.

---

## Authorisation

Eight roles: `OWNER`, `ADMIN`, `ACCOUNTANT`, `FINANCE_MANAGER`,
`SALES_MANAGER`, `PURCHASE_MANAGER`, `AUDITOR`, `VIEWER`.

Permissions are granular and stored as data in `app/services/policy.py` —
`post_journal`, `approve_proposal`, `release_payment`, `close_period`,
`manage_users`, `change_accounting_policy`, and the read/propose set.

Two properties are worth stating explicitly:

- **`AUDITOR` has read access to everything and write access to nothing** —
  including the audit log, which no role can modify.
- **Approval separation.** The approval rules can require that the approver is
  not the creator. For a payment above the configured threshold this is the
  default, so a single compromised account cannot both raise and release a
  payment.

Company scoping is applied in the query. A request for another company's invoice
returns 404 — it does not return 403, because "that record exists but is not
yours" is itself information.

---

## The audit trail

`app/services/audit.py`. Append-only, and hash-chained:

```
hash(n) = sha256( hash(n-1) + canonical_json(event(n)) )
```

`canonical_json` sorts keys and serialises `Decimal` as a string, so the hash of
an event is stable across processes and Python versions.

Every event records who, what, when, from which IP, with which request id, and
the before/after state of what changed. Recorded events include: authentication,
every ledger posting and reversal, every approval and rejection, period
open/close, policy and tax-rule changes, user and role changes, exports, AI
proposals and their outcomes, and detected prompt-injection attempts.

**There is no update or delete path for audit events.** Not a permission that
nobody holds — no code that does it.

`verify_chain()` recomputes the whole chain and reports the first index where it
breaks. It is exposed at `GET /workspace/audit/verify`, runs as one of the
month-end close checks, and there is a button for it on the Audit Log screen.

What this does and does not give you: it proves that nobody has *edited or
removed* a past event, because doing so breaks every subsequent hash. It does
not by itself stop someone with database write access from rewriting the entire
chain from a chosen point. For that you would export the head hash to somewhere
outside the database — the head hash is exposed in the verify response precisely
so it can be recorded externally.

---

## AI-specific security

Covered in depth in [AI_AGENTS.md](AI_AGENTS.md). The security-relevant summary:

**Structural containment.** The strongest defence is not the pattern matcher —
it is that a language model's output has nowhere to go. It becomes a typed
proposal, which is validated, grounded against the database, checked by the
accounting engine, gated by the policy engine, and approved by a human. A
successful prompt injection wins the right to *suggest something to an
accountant*, on a screen that shows the evidence.

**No NL-to-SQL.** 22 typed tools with fixed queries, declared permissions, and
their own company filter. A tool argument is a value, never a fragment of a
query — `test_sql_injection_through_a_tool_argument_is_inert` searches for
`'; DROP TABLE journal_entries; --` and gets zero results, not an error and not
a dropped table.

**Row limits.** Every list-returning tool caps its result set, so a prompt
cannot turn a query tool into a bulk export.

**Secret redaction.** Provider keys, bearer tokens and password-shaped strings
are stripped from anything entering a prompt or a log.
`test_security_and_api.py` plants a realistic-looking key in the settings and
asserts it appears in no API response body.

---

## File uploads

Attachments and statement imports are validated before anything is written.
`app/services/documents.py` identifies the file **from its leading bytes** and
checks that type against an allow-list. The extension and the browser-declared
content type are not trusted at all, so renaming a script to `.pdf` gets it
rejected rather than stored. Size is capped by `MAX_UPLOAD_BYTES` (25 MB by
default) and checked before the body is read into memory.

Stored filenames are randomised; the original name is kept as metadata and never
used as a path.

OCR and PDF parsing are optional dependencies. When they are absent the upload
still succeeds and the extraction step reports honestly that text could not be
read — it does not guess at field values.

---

## Transport and headers

Every response carries:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Strict-Transport-Security: max-age=31536000; includeSubDomains   (HTTPS only)
```

CORS is an explicit origin list. In production a wildcard origin is refused at
startup rather than accepted with a warning.

Errors return a structured body — `code`, `message`, `details`, `request_id` —
and never a stack trace, a SQL statement, or a file path. The request id is the
link between what the user saw and what the server logged.

---

## Secrets

- Never committed. `.gitignore` excludes `.env`, `data/` and `*.db`;
  `.env.example` contains only empty placeholders.
- Never sent to the frontend. The frontend has no provider key and makes no
  direct call to any AI provider — every model call goes through the backend.
- Never logged. Redaction runs on log records, prompts and error payloads.
- Never in an error message.

---

## Reporting a vulnerability

This is an evaluation build. If you find something, open an issue describing the
impact and the reproduction — please do not include real credentials or real
company data in the report.
