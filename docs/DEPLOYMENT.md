# Deployment

The whole system runs locally with no external service, no API key and no paid
dependency. That is the baseline; everything below is about moving it to a
server without giving that property up.

---

## Requirements

| | Minimum | Notes |
|---|---|---|
| Python | 3.10 | 3.11+ is faster |
| Node | 18 | Build only; not needed at runtime if you serve the built assets |
| Disk | ~500 MB | Including the demo dataset |
| RAM | 512 MB | 2 GB is comfortable |
| Database | none | SQLite is built in; PostgreSQL 14+ optional |

No compiler is required. Password hashing uses PBKDF2 from the standard library
precisely so that a machine without build tools can still run this.

---

## Local

```bash
# Backend
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt      # macOS/Linux: .venv/bin/pip
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# Frontend, in a second terminal
cd frontend
npm install
npm run dev
```

<http://localhost:5173> → **Load demo company** → sign in.

`requirements.txt` is the runtime set; `requirements-dev.txt` adds pytest and
tooling.

### Optional extras

```bash
pip install pytesseract pdfplumber pymupdf   # OCR and PDF text extraction
ollama pull llama3.1                         # a local language model, free
```

Both are genuinely optional. Without OCR, document upload still works and the
extraction step reports honestly that it could not read the file rather than
guessing. Without any model, the deterministic provider handles everything.

---

## Configuration

Copy `.env.example` to `backend/.env` and set what you need. Every value has a
working default; see the file itself for the full list.

The settings that matter in production:

```bash
ENVIRONMENT=production
DEBUG=false
SECRET_KEY=<48+ random bytes>
DATABASE_URL=postgresql+psycopg://ailedger:...@localhost:5432/ailedger
CORS_ORIGINS=https://books.yourcompany.com
```

`app/config.py` **refuses to start** in production if `SECRET_KEY` is unset,
`DEBUG` is true, or `CORS_ORIGINS` is `*`. These are the three mistakes that
matter most and the cheapest possible time to catch them is startup, so they are
errors rather than warnings.

Generate a key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Without one, a key is generated per process — which means every restart signs
every user out.

---

## The schema

Tables are created from the SQLAlchemy models on startup (`create_all`). There
are no migration scripts in this repository, so **schema changes require
recreating the database**. For an evaluation build that is the honest trade;
before putting real data in, generate an Alembic baseline:

```bash
cd backend
alembic init migrations                     # once
alembic revision --autogenerate -m "baseline"
alembic upgrade head
```

Alembic is already a dependency. Until you do this, treat the database as
disposable.

---

## PostgreSQL

The application code is identical — the only reason PostgreSQL is worth the
setup is concurrency, since SQLite serialises writers. If you plan to run more
than one worker, this is the step that makes that safe.

```bash
sudo -u postgres createuser ailedger --pwprompt
sudo -u postgres createdb ailedger --owner=ailedger
pip install "psycopg[binary]"
```

```bash
DATABASE_URL=postgresql+psycopg://ailedger:password@localhost:5432/ailedger
```

Money is stored as native `NUMERIC(20,2)` on PostgreSQL and as a sign-prefixed
zero-padded string on SQLite. Both round-trip a `Decimal` exactly; the string
form exists because SQLAlchemy's `Numeric` goes through `float` on SQLite and
silently corrupts money. See [ACCOUNTING_ENGINE.md](ACCOUNTING_ENGINE.md).

There is no automatic converter between the two backends. To move data, export
and re-import through the API.

---

## Serving it

### Build the frontend

```bash
cd frontend
npm run build          # → frontend/dist
```

The client calls `/api/v1` as a relative path, so there is nothing to configure:
in development Vite proxies it to port 8000, and in production the reverse proxy
below does the same job. The frontend never needs to know the backend's address,
and it never holds a provider key.

### Run the backend

```bash
cd backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4
```

Bind to `127.0.0.1` and put a reverse proxy in front. Do not expose uvicorn
directly.

### Workers and concurrency

Two things change behaviour when you run more than one worker, and neither is
obvious from a stack trace later.

**SQLite serialises writers.** One writer holds the database at a time; the
others wait on `busy_timeout` (30 s here) and then fail. Reads are fine — WAL
mode is enabled — but a write-heavy workload spread across several workers will
start timing out. **On SQLite, run one worker.** If you need more, move to
PostgreSQL; that is the reason to.

**The rate limiter is per-process.** `RateLimiter` keeps its buckets in memory,
so with four workers a client gets roughly four times `RATE_LIMIT_PER_MINUTE`
before being limited, and the count resets whenever a worker restarts. It is
deliberately dependency-free and sized for a single-node deployment. Options, in
order of preference:

1. **Enforce the limit at the reverse proxy**, which sees every request:

   ```nginx
   limit_req_zone $binary_remote_addr zone=api:10m  rate=300r/m;
   limit_req_zone $binary_remote_addr zone=auth:10m rate=10r/m;

   location /api/ {
       limit_req zone=api burst=60 nodelay;
       proxy_pass http://127.0.0.1:8000;
   }
   location /api/v1/auth/ {
       limit_req zone=auth burst=5;
       proxy_pass http://127.0.0.1:8000;
   }
   ```

2. **Divide the setting by the worker count.** Crude, but it keeps the aggregate
   roughly right.
3. **Run single-worker instances** and scale by adding instances behind the
   proxy.

Do not rely on the in-process limiter alone for brute-force protection on a
multi-worker deployment.

### systemd

```ini
# /etc/systemd/system/ailedger.service
[Unit]
Description=AI Ledger
After=network.target postgresql.service

[Service]
Type=simple
User=ailedger
WorkingDirectory=/opt/ailedger/backend
EnvironmentFile=/opt/ailedger/backend/.env
# --workers 4 assumes PostgreSQL. On SQLite use --workers 1 (see above).
ExecStart=/opt/ailedger/backend/.venv/bin/python -m uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 4
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/ailedger/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ailedger
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name books.yourcompany.com;

    ssl_certificate     /etc/letsencrypt/live/books.yourcompany.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/books.yourcompany.com/privkey.pem;

    client_max_body_size 30M;          # must exceed MAX_UPLOAD_BYTES

    root /opt/ailedger/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;    # SPA routing
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;             # agent runs can be slow
    }
}

server {
    listen 80;
    server_name books.yourcompany.com;
    return 301 https://$host$request_uri;
}
```

`X-Forwarded-For` matters: without it every request appears to come from the
proxy and the per-IP rate limit becomes a global one.

---

## Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY frontend/dist ./static
RUN useradd -m ailedger && mkdir -p /data && chown -R ailedger /data /app
USER ailedger
ENV DATABASE_URL=sqlite:////data/ailedger.db STORAGE_DIR=/data/storage
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker run -d -p 8000:8000 -v ailedger-data:/data \
  -e SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e ENVIRONMENT=production -e DEBUG=false \
  --name ailedger ailedger:latest
```

Keep `/data` on a named volume. Everything that must survive a container
replacement lives there.

---

## Backups

Two things need backing up: the database and `STORAGE_DIR` (uploaded documents).

**SQLite** — use the backup API, not `cp`. Copying a live SQLite file can
capture a torn write:

```bash
sqlite3 /data/ailedger.db ".backup '/backups/ailedger-$(date +%F).db'"
```

**PostgreSQL**

```bash
pg_dump -Fc ailedger > /backups/ailedger-$(date +%F).dump
```

**Verify a restore before you need one.** After restoring, run the integrity
checks — they will tell you immediately whether the books came back whole:

```bash
curl -s localhost:8000/api/v1/demo/verify        -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/api/v1/workspace/audit/verify -H "Authorization: Bearer $TOKEN"
```

Record the audit chain's head hash somewhere outside the database. The chain
proves nobody edited a past event; an externally-recorded head hash also proves
nobody rewrote the whole chain.

---

## Health and monitoring

```
GET /health                        liveness — no auth; reports database and AI provider status
GET /api/v1/accounting/invariants  the five accounting invariants
GET /api/v1/workspace/audit/verify the audit hash chain
```

The second and third are the ones worth alerting on. A failing invariant means
the books are wrong, which is more urgent than any latency metric this system
could report.

Every response carries an `X-Request-Id` that appears in the server log and in
error bodies, so a user-reported problem can be traced to an exact request.

---

## Before going live

- [ ] `SECRET_KEY` set, 48+ random bytes, not in version control
- [ ] `ENVIRONMENT=production`, `DEBUG=false`
- [ ] `CORS_ORIGINS` set to your actual origin, not `*`
- [ ] HTTPS terminated, HTTP redirected
- [ ] `X-Forwarded-For` passed through the proxy
- [ ] Demo company removed (`POST /demo/reset`) before entering real data
- [ ] Alembic baseline generated
- [ ] Backups scheduled **and a restore tested**
- [ ] Audit chain head hash exported somewhere outside the database
- [ ] Seeded tax rates checked against current law — see [TAX.md](TAX.md)
- [ ] Real user accounts created with appropriate roles; demo login removed

---

## Verifying a deployment

```bash
cd backend
.venv/bin/python -m pytest                # 177 unit tests
.venv/bin/python scripts/verify_api.py    # 114 HTTP checks

cd ../frontend
node e2e/ui.spec.mjs                      # 55 browser checks
```

`verify_api.py` runs against a live server, so it is also a post-deployment
smoke test — point it at the deployed URL by editing `BASE` at the top.
