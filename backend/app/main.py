"""AI Ledger — FastAPI application entry point.

Wires up middleware (request ids, security headers, rate limiting), consistent
error handling, and every router. Errors always come back in the same shape so
the frontend can react to ``code`` rather than parsing prose.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import settings
from app.database import create_all, engine
from app.errors import AILedgerError, RateLimited
from app.security import rate_limiter

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("ailedger")

# Paths that skip the general rate limiter (health checks and docs).
EXEMPT_PATHS = {"/health", "/", "/docs", "/redoc", "/openapi.json"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = settings.validate_runtime()
    if problems:
        for problem in problems:
            logger.error("Configuration error: %s", problem)
        raise RuntimeError(
            "Refusing to start with an unsafe configuration: " + "; ".join(problems)
        )

    create_all()
    logger.info(
        "%s v%s started in %s mode (database: %s)",
        settings.APP_NAME,
        __version__,
        settings.ENVIRONMENT,
        "SQLite" if settings.is_sqlite else "PostgreSQL",
    )

    configured = [
        name
        for name, key in (("Gemini", settings.GEMINI_API_KEY), ("Grok", settings.GROK_API_KEY))
        if key
    ]
    logger.info(
        "AI providers configured: %s (Ollama probed at runtime; Mock always available)",
        ", ".join(configured) if configured else "none",
    )

    yield
    logger.info("%s shutting down", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "AI-native accounting platform. The accounting engine is the source of truth; "
        "AI agents propose, deterministic rules validate, and humans approve."
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Company-Id", "X-Request-Id"],
    expose_headers=["X-Request-Id"],
    max_age=600,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id, apply rate limits and set security headers."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    started = time.perf_counter()

    path = request.url.path
    if path not in EXEMPT_PATHS and not path.startswith("/static"):
        client = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        allowed, retry_after = rate_limiter.check(
            f"http:{client}", limit=settings.RATE_LIMIT_PER_MINUTE, window_seconds=60
        )
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "code": "rate_limited",
                    "message": f"Too many requests. Try again in {retry_after} seconds.",
                    "details": {"retry_after": retry_after},
                    "request_id": request_id,
                },
                headers={"Retry-After": str(retry_after), "X-Request-Id": request_id},
            )

    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - converted to a clean 500 below
        logger.exception("Unhandled error on %s %s [%s]", request.method, path, request_id)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "Something went wrong. The error has been logged.",
                "details": None,
                "request_id": request_id,
            },
            headers={"X-Request-Id": request_id},
        )

    response.headers["X-Request-Id"] = request_id
    response.headers["X-Response-Time-Ms"] = str(int((time.perf_counter() - started) * 1000))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ------------------------------------------------------- error handlers --

@app.exception_handler(AILedgerError)
async def handle_domain_error(request: Request, exc: AILedgerError) -> JSONResponse:
    """Domain errors carry a stable machine-readable code.

    ``details`` routinely carries Decimals and dates (duplicate-document
    evidence, period bounds), so it is encoded rather than handed straight to
    ``json.dumps``.
    """
    if exc.status_code >= 500:
        logger.error("%s: %s", exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(
            {**exc.to_dict(), "request_id": getattr(request.state, "request_id", "")},
            custom_encoder={Decimal: str},
        ),
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Turn Pydantic errors into readable, field-level messages."""
    problems = []
    for error in exc.errors():
        location = " -> ".join(str(part) for part in error["loc"] if part != "body")
        problems.append({"field": location or "request", "message": error["msg"]})

    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "The request could not be processed: "
            + "; ".join(f"{p['field']}: {p['message']}" for p in problems[:5]),
            "details": {"problems": problems},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    codes = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": codes.get(exc.status_code, "http_error"),
            "message": str(exc.detail),
            "details": None,
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


# ---------------------------------------------------------------- routes --

@app.get("/health", tags=["System"])
async def health() -> dict:
    """Liveness and dependency check."""
    database_status = "ok"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        database_status = f"error: {exc}"

    from app.ai.providers import active_provider_name

    try:
        provider = await active_provider_name()
    except Exception:  # noqa: BLE001
        provider = "mock"

    return {
        "status": "ok" if database_status == "ok" else "degraded",
        "version": __version__,
        "environment": settings.ENVIRONMENT,
        "database": database_status,
        "ai_provider": provider,
        "timestamp": datetime.now(timezone.utc),
    }


@app.get("/", tags=["System"])
async def root() -> dict:
    return {
        "name": settings.APP_NAME,
        "tagline": settings.APP_TAGLINE,
        "version": __version__,
        "docs": "/docs",
        "api": settings.API_PREFIX,
        "principle": (
            "The accounting engine is the source of truth. AI proposes, "
            "deterministic rules validate, humans approve, and everything is audited."
        ),
    }


from app.api import accounting, ai, auth, banking, company, demo, inventory, purchases, reports, sales, tax, workspace  # noqa: E402

for module in (
    auth,
    company,
    accounting,
    sales,
    purchases,
    banking,
    inventory,
    tax,
    reports,
    ai,
    workspace,
    demo,
):
    app.include_router(module.router, prefix=settings.API_PREFIX)


def run() -> None:  # pragma: no cover - convenience entry point
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)


if __name__ == "__main__":  # pragma: no cover
    run()
