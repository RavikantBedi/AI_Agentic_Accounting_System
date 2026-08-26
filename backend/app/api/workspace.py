"""Exceptions, anomalies, audit trail, close, settings, documents and search."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy import func, or_, select

from app.deps import CurrentUserDep, DbDep, audit_context, require_permission
from app.enums import (
    ActorType,
    AuditAction,
    ExceptionStatus,
    Permission,
    Severity,
)
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Account,
    Anomaly,
    ApprovalRule,
    AuditEvent,
    Bill,
    Customer,
    ExceptionItem,
    Invoice,
    Item,
    JournalEntry,
    Notification,
    Policy,
    StoredDocument,
    Vendor,
)
from app.models.base import utcnow
from app.money import ZERO, money
from app.schemas import (
    AnomalyOut,
    ApprovalRuleOut,
    AuditEventOut,
    DocumentOut,
    ExceptionOut,
    ExceptionResolve,
    MessageResponse,
    Page,
    PolicyOut,
    PolicyUpdate,
)

router = APIRouter(tags=["Workspace"])


# ------------------------------------------------------------ exceptions --

@router.get("/exceptions", response_model=Page[ExceptionOut])
def list_exceptions(
    db: DbDep,
    current: CurrentUserDep,
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ExceptionOut]:
    current.require(Permission.READ_TRANSACTIONS.value)

    stmt = select(ExceptionItem).where(ExceptionItem.company_id == current.company_id)
    if status:
        stmt = stmt.where(ExceptionItem.status == status.upper())
    else:
        stmt = stmt.where(
            ExceptionItem.status.in_([ExceptionStatus.OPEN, ExceptionStatus.IN_REVIEW])
        )
    if category:
        stmt = stmt.where(ExceptionItem.category == category.upper())
    if severity:
        stmt = stmt.where(ExceptionItem.severity == severity.upper())

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    items = list(
        db.execute(stmt.order_by(ExceptionItem.created_at.desc()).limit(500)).scalars()
    )
    items.sort(key=lambda item: (order.get(item.severity, 9), -item.created_at.timestamp()))
    window = items[offset : offset + limit]

    return Page(
        items=[ExceptionOut.model_validate(item) for item in window],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/exceptions/summary")
def exception_summary(db: DbDep, current: CurrentUserDep) -> dict:
    """Counts by category and severity for the exception centre header."""
    current.require(Permission.READ_TRANSACTIONS.value)

    rows = db.execute(
        select(
            ExceptionItem.category,
            ExceptionItem.severity,
            func.count(ExceptionItem.id),
            func.sum(ExceptionItem.amount),
        )
        .where(
            ExceptionItem.company_id == current.company_id,
            ExceptionItem.status.in_([ExceptionStatus.OPEN, ExceptionStatus.IN_REVIEW]),
        )
        .group_by(ExceptionItem.category, ExceptionItem.severity)
    ).all()

    by_category: dict[str, dict] = {}
    by_severity: dict[str, int] = {}
    total = 0
    total_amount = ZERO

    for category, severity, count, amount in rows:
        entry = by_category.setdefault(
            category, {"category": category, "count": 0, "amount": ZERO, "by_severity": {}}
        )
        entry["count"] += int(count)
        entry["amount"] = money(entry["amount"] + (amount or 0))
        entry["by_severity"][severity] = int(count)
        by_severity[severity] = by_severity.get(severity, 0) + int(count)
        total += int(count)
        total_amount = money(total_amount + (amount or 0))

    return {
        "total_open": total,
        "total_amount": total_amount,
        "by_category": sorted(by_category.values(), key=lambda entry: entry["count"], reverse=True),
        "by_severity": by_severity,
    }


@router.post("/exceptions/{exception_id}/resolve", response_model=ExceptionOut)
def resolve_exception(
    exception_id: str,
    payload: ExceptionResolve,
    request: Request,
    db: DbDep,
    current: CurrentUserDep,
) -> ExceptionOut:
    current.require(Permission.READ_TRANSACTIONS.value)

    item = db.get(ExceptionItem, exception_id)
    if item is None or item.company_id != current.company_id:
        raise NotFoundError("Exception not found.")
    if item.status in (ExceptionStatus.RESOLVED, ExceptionStatus.DISMISSED):
        raise ConflictError(f"This exception is already {item.status.lower()}.")

    item.status = ExceptionStatus.DISMISSED if payload.dismiss else ExceptionStatus.RESOLVED
    item.resolved_by_user_id = current.id
    item.resolved_at = utcnow()
    item.resolution_note = payload.note
    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.UPDATE,
        entity_type="ExceptionItem",
        entity_id=item.id,
        entity_label=item.title,
        summary=f"Exception '{item.title}' marked {item.status.lower()}",
        reason=payload.note,
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        amount=item.amount,
        **audit_context(request),
    )
    return ExceptionOut.model_validate(item)


# ------------------------------------------------------------- anomalies --

@router.get("/anomalies", response_model=Page[AnomalyOut])
def list_anomalies(
    db: DbDep,
    current: CurrentUserDep,
    kind: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AnomalyOut]:
    current.require(Permission.READ_TRANSACTIONS.value)

    stmt = select(Anomaly).where(Anomaly.company_id == current.company_id)
    if status:
        stmt = stmt.where(Anomaly.status == status.upper())
    else:
        stmt = stmt.where(Anomaly.status == ExceptionStatus.OPEN)
    if kind:
        stmt = stmt.where(Anomaly.kind == kind.upper())
    if severity:
        stmt = stmt.where(Anomaly.severity == severity.upper())

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    anomalies = list(
        db.execute(stmt.order_by(Anomaly.created_at.desc()).limit(limit).offset(offset)).scalars()
    )
    return Page(
        items=[AnomalyOut.model_validate(anomaly) for anomaly in anomalies],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/anomalies/{anomaly_id}/review", response_model=AnomalyOut)
def review_anomaly(
    anomaly_id: str,
    db: DbDep,
    current: CurrentUserDep,
    is_true_positive: bool = Query(...),
    note: str = Query(default=""),
) -> AnomalyOut:
    """Confirm or dismiss an anomaly. The verdict measures detector quality."""
    current.require(Permission.READ_TRANSACTIONS.value)

    anomaly = db.get(Anomaly, anomaly_id)
    if anomaly is None or anomaly.company_id != current.company_id:
        raise NotFoundError("Anomaly not found.")

    anomaly.status = ExceptionStatus.RESOLVED if is_true_positive else ExceptionStatus.DISMISSED
    anomaly.is_true_positive = is_true_positive
    anomaly.reviewed_by_user_id = current.id
    anomaly.reviewed_at = utcnow()
    anomaly.resolution_note = note
    db.flush()
    return AnomalyOut.model_validate(anomaly)


@router.post("/anomalies/scan")
def scan_anomalies(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.READ_TRANSACTIONS.value))],
    lookback_days: int = Query(default=180, ge=1, le=1000),
) -> dict:
    from app.services.anomaly import detect_all

    result = detect_all(db, current.company_id, lookback_days=lookback_days)
    return {
        **result,
        "total_amount_flagged": str(result["total_amount_flagged"]),
        "by_severity": {str(key): value for key, value in result["by_severity"].items()},
    }


# ----------------------------------------------------------------- audit --

@router.get("/audit", response_model=Page[AuditEventOut])
def list_audit_events(
    db: DbDep,
    current: CurrentUserDep,
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    actor_type: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AuditEventOut]:
    current.require(Permission.READ_AUDIT.value)

    stmt = select(AuditEvent).where(AuditEvent.company_id == current.company_id)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action.upper())
    if actor_type:
        stmt = stmt.where(AuditEvent.actor_type == actor_type.upper())
    if start_date:
        stmt = stmt.where(AuditEvent.occurred_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        stmt = stmt.where(AuditEvent.occurred_at <= datetime.combine(end_date, datetime.max.time()))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(AuditEvent.summary).like(pattern)
            | func.lower(AuditEvent.entity_label).like(pattern)
            | func.lower(AuditEvent.actor_label).like(pattern)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    events = list(
        db.execute(stmt.order_by(AuditEvent.sequence.desc()).limit(limit).offset(offset)).scalars()
    )
    return Page(
        items=[AuditEventOut.model_validate(event) for event in events],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/audit/timeline/{entity_type}/{entity_id}", response_model=list[AuditEventOut])
def audit_timeline(
    entity_type: str, entity_id: str, db: DbDep, current: CurrentUserDep
) -> list[AuditEventOut]:
    """Every recorded change to one record, oldest first."""
    current.require(Permission.READ_AUDIT.value)
    from app.services.audit import entity_timeline

    events = entity_timeline(db, current.company_id, entity_type, entity_id)
    return [AuditEventOut.model_validate(event) for event in events]


@router.get("/audit/verify")
def verify_audit_chain(db: DbDep, current: CurrentUserDep) -> dict:
    """Recompute the audit hash chain and report any tampering."""
    current.require(Permission.READ_AUDIT.value)
    from app.services.audit import verify_chain

    return verify_chain(db, current.company_id)


# ----------------------------------------------------------------- close --

@router.get("/close/status")
def close_status(
    db: DbDep,
    current: CurrentUserDep,
    period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    from app.services.close import close_status as get_status

    return get_status(db, current.company_id, period=period)


@router.post("/close/run")
def run_close_checklist(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.READ_REPORTS.value))],
    period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    """Re-run every close check against live data."""
    from app.services.close import close_status as get_status
    from app.services.close import run_checklist

    run_checklist(db, current.company_id, period=period, user_id=current.id)
    return get_status(db, current.company_id, period=period)


@router.post("/close/tasks/{task_id}/complete")
def complete_close_task(
    task_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.READ_REPORTS.value))],
    note: str = Query(default=""),
) -> dict:
    from app.services.close import complete_task

    task = complete_task(db, current.company_id, task_id, user_id=current.id, notes=note)
    return {"id": task.id, "key": task.key, "status": task.status}


@router.post("/close/approve")
def approve_close(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.CLOSE_PERIOD.value))],
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    notes: str = Query(default=""),
    force: bool = Query(default=False),
) -> dict:
    """Verify the checklist, then close the accounting period."""
    from app.services.close import approve_and_close

    return approve_and_close(
        db, current.company_id, period=period, user_id=current.id, notes=notes, force=force
    )


# -------------------------------------------------------------- settings --

@router.get("/settings/policies", response_model=list[PolicyOut])
def list_policies(db: DbDep, current: CurrentUserDep) -> list[PolicyOut]:
    """Business policy is data. This is what the engine actually applies."""
    current.require(Permission.READ_REPORTS.value)
    from app.services.policy import DEFAULT_POLICIES, get_policy

    results = []
    for key, spec in DEFAULT_POLICIES.items():
        stored = db.execute(
            select(Policy)
            .where(Policy.company_id == current.company_id, Policy.key == key, Policy.is_active.is_(True))
            .order_by(Policy.version.desc())
        ).scalars().first()
        results.append(
            PolicyOut(
                key=key,
                version=stored.version if stored else 0,
                value=get_policy(db, current.company_id, key),
                description=spec["description"],
                is_active=True,
            )
        )
    return results


@router.put("/settings/policies/{key}", response_model=PolicyOut)
def update_policy(
    key: str,
    payload: PolicyUpdate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.CHANGE_ACCOUNTING_POLICY.value))],
) -> PolicyOut:
    """Write a new policy version. Old versions are retained and audited."""
    from app.services.policy import DEFAULT_POLICIES, set_policy

    if key not in DEFAULT_POLICIES:
        raise ValidationError(
            f"'{key}' is not a known policy.", details={"available": sorted(DEFAULT_POLICIES)}
        )

    policy = set_policy(
        db,
        current.company_id,
        key,
        payload.value,
        user_id=current.id,
        description=payload.description or DEFAULT_POLICIES[key]["description"],
    )
    return PolicyOut.model_validate(policy)


@router.get("/settings/approval-rules", response_model=list[ApprovalRuleOut])
def list_approval_rules(db: DbDep, current: CurrentUserDep) -> list[ApprovalRuleOut]:
    current.require(Permission.READ_REPORTS.value)
    rules = list(
        db.execute(
            select(ApprovalRule)
            .where(ApprovalRule.company_id == current.company_id)
            .order_by(ApprovalRule.entity_type, ApprovalRule.priority)
        ).scalars()
    )
    return [ApprovalRuleOut.model_validate(rule) for rule in rules]


@router.get("/settings/permissions")
def permission_matrix(current: CurrentUserDep) -> dict:
    """Who can do what — the full RBAC matrix, including the AI's limits."""
    from app.enums import Permission as P
    from app.services.policy import AI_ALLOWED_PERMISSIONS, FORBIDDEN_FOR_AI, ROLE_PERMISSIONS

    return {
        "roles": {role: sorted(perms) for role, perms in ROLE_PERMISSIONS.items()},
        "all_permissions": sorted(p.value for p in P),
        "ai_allowed": sorted(AI_ALLOWED_PERMISSIONS),
        "ai_forbidden": sorted(FORBIDDEN_FOR_AI),
        "your_role": current.role,
        "your_permissions": sorted(current.permissions),
        "note": (
            "AI agents can hold read and propose permissions only. Execute-class "
            "permissions (posting, approving, paying, closing) are structurally "
            "unavailable to them."
        ),
    }


# ------------------------------------------------------------- documents --

@router.get("/documents", response_model=Page[DocumentOut])
def list_documents(
    db: DbDep,
    current: CurrentUserDep,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[DocumentOut]:
    current.require(Permission.READ_BILLS.value)

    stmt = select(StoredDocument).where(StoredDocument.company_id == current.company_id)
    if status:
        stmt = stmt.where(StoredDocument.extraction_status == status.upper())
    if kind:
        stmt = stmt.where(StoredDocument.kind == kind.upper())

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    documents = list(
        db.execute(
            stmt.order_by(StoredDocument.created_at.desc()).limit(limit).offset(offset)
        ).scalars()
    )
    return Page(
        items=[DocumentOut.model_validate(document) for document in documents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/documents/upload", response_model=DocumentOut, status_code=201)
async def upload_document(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.PROPOSE_BILL.value))],
    file: UploadFile = File(...),
    kind: str = Query(default="OTHER"),
    process: bool = Query(default=True),
) -> DocumentOut:
    """Upload a document and extract its fields."""
    from app.services.documents import process_document, store_document

    content = await file.read()
    document = store_document(
        db,
        current.company_id,
        content=content,
        filename=file.filename or "document",
        kind=kind.upper(),
        user_id=current.id,
    )
    if process:
        process_document(db, current.company_id, document.id, user_id=current.id)
    return DocumentOut.model_validate(document)


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: str, db: DbDep, current: CurrentUserDep) -> DocumentOut:
    current.require(Permission.READ_BILLS.value)
    document = db.get(StoredDocument, document_id)
    if document is None or document.company_id != current.company_id:
        raise NotFoundError("Document not found.")
    return DocumentOut.model_validate(document)


@router.get("/documents/{document_id}/text")
def get_document_text(document_id: str, db: DbDep, current: CurrentUserDep) -> dict:
    """The extracted text, for the review pane beside the document preview."""
    current.require(Permission.READ_BILLS.value)
    document = db.get(StoredDocument, document_id)
    if document is None or document.company_id != current.company_id:
        raise NotFoundError("Document not found.")
    return {
        "id": document.id,
        "filename": document.filename,
        "extraction_method": document.extraction_method,
        "text": document.extracted_text[:50_000],
        "fields": document.extracted_fields,
        "field_confidence": document.field_confidence,
        "needs_review_reasons": document.needs_review_reasons,
    }


# ---------------------------------------------------------------- search --

@router.get("/search")
def global_search(
    db: DbDep,
    current: CurrentUserDep,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=8, ge=1, le=25),
) -> dict:
    """Command-palette search across every major entity."""
    current.require(Permission.READ_TRANSACTIONS.value)
    pattern = f"%{q.lower()}%"
    results: list[dict] = []

    def add(entity_type: str, rows: list, label_fn, url_fn, subtitle_fn=None) -> None:
        for row in rows:
            results.append(
                {
                    "type": entity_type,
                    "id": row.id,
                    "label": label_fn(row),
                    "subtitle": subtitle_fn(row) if subtitle_fn else "",
                    "url": url_fn(row),
                }
            )

    add(
        "invoice",
        list(
            db.execute(
                select(Invoice)
                .where(
                    Invoice.company_id == current.company_id,
                    func.lower(Invoice.number).like(pattern),
                )
                .limit(limit)
            ).scalars()
        ),
        lambda row: row.number,
        lambda row: f"/sales/invoices/{row.id}",
        lambda row: f"{row.customer.name if row.customer else ''} · {row.total}",
    )
    add(
        "bill",
        list(
            db.execute(
                select(Bill)
                .where(
                    Bill.company_id == current.company_id,
                    or_(
                        func.lower(Bill.number).like(pattern),
                        func.lower(Bill.vendor_invoice_number).like(pattern),
                    ),
                )
                .limit(limit)
            ).scalars()
        ),
        lambda row: f"{row.number} ({row.vendor_invoice_number})",
        lambda row: f"/purchases/bills/{row.id}",
        lambda row: f"{row.vendor.name if row.vendor else ''} · {row.total}",
    )
    add(
        "customer",
        list(
            db.execute(
                select(Customer)
                .where(
                    Customer.company_id == current.company_id,
                    or_(
                        func.lower(Customer.name).like(pattern),
                        func.lower(Customer.code).like(pattern),
                    ),
                )
                .limit(limit)
            ).scalars()
        ),
        lambda row: row.name,
        lambda row: f"/sales/customers/{row.id}",
        lambda row: row.code,
    )
    add(
        "vendor",
        list(
            db.execute(
                select(Vendor)
                .where(
                    Vendor.company_id == current.company_id,
                    or_(
                        func.lower(Vendor.name).like(pattern),
                        func.lower(Vendor.code).like(pattern),
                    ),
                )
                .limit(limit)
            ).scalars()
        ),
        lambda row: row.name,
        lambda row: f"/purchases/vendors/{row.id}",
        lambda row: row.code,
    )
    add(
        "item",
        list(
            db.execute(
                select(Item)
                .where(
                    Item.company_id == current.company_id,
                    or_(func.lower(Item.name).like(pattern), func.lower(Item.sku).like(pattern)),
                )
                .limit(limit)
            ).scalars()
        ),
        lambda row: f"{row.sku} — {row.name}",
        lambda row: f"/inventory/items/{row.id}",
        lambda row: f"{row.current_qty} {row.uom} in stock",
    )
    add(
        "account",
        list(
            db.execute(
                select(Account)
                .where(
                    Account.company_id == current.company_id,
                    or_(
                        func.lower(Account.name).like(pattern),
                        func.lower(Account.code).like(pattern),
                    ),
                )
                .limit(limit)
            ).scalars()
        ),
        lambda row: f"{row.code} — {row.name}",
        lambda row: f"/accounting/ledger/{row.id}",
        lambda row: row.type,
    )
    add(
        "journal",
        list(
            db.execute(
                select(JournalEntry)
                .where(
                    JournalEntry.company_id == current.company_id,
                    or_(
                        func.lower(JournalEntry.entry_number).like(pattern),
                        func.lower(JournalEntry.narration).like(pattern),
                    ),
                )
                .limit(limit)
            ).scalars()
        ),
        lambda row: row.entry_number,
        lambda row: f"/accounting/journals/{row.id}",
        lambda row: f"{row.entry_date} · {row.total_debit}",
    )

    return {"query": q, "results": results[: limit * 4], "count": len(results)}


# --------------------------------------------------------- notifications --

@router.get("/notifications")
def list_notifications(
    db: DbDep,
    current: CurrentUserDep,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict:
    stmt = select(Notification).where(Notification.company_id == current.company_id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))

    notifications = list(
        db.execute(stmt.order_by(Notification.created_at.desc()).limit(limit)).scalars()
    )
    unread = int(
        db.execute(
            select(func.count(Notification.id)).where(
                Notification.company_id == current.company_id,
                Notification.is_read.is_(False),
            )
        ).scalar_one()
    )

    return {
        "unread_count": unread,
        "notifications": [
            {
                "id": item.id,
                "kind": item.kind,
                "severity": item.severity,
                "title": item.title,
                "message": item.message,
                "amount": str(item.amount) if item.amount is not None else None,
                "action_url": item.action_url,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "is_read": item.is_read,
                "created_at": item.created_at,
            }
            for item in notifications
        ],
    }


@router.post("/notifications/{notification_id}/read", response_model=MessageResponse)
def mark_notification_read(
    notification_id: str, db: DbDep, current: CurrentUserDep
) -> MessageResponse:
    notification = db.get(Notification, notification_id)
    if notification is None or notification.company_id != current.company_id:
        raise NotFoundError("Notification not found.")
    notification.is_read = True
    notification.read_at = utcnow()
    db.flush()
    return MessageResponse(message="Marked as read.")


@router.post("/notifications/read-all", response_model=MessageResponse)
def mark_all_read(db: DbDep, current: CurrentUserDep) -> MessageResponse:
    notifications = list(
        db.execute(
            select(Notification).where(
                Notification.company_id == current.company_id,
                Notification.is_read.is_(False),
            )
        ).scalars()
    )
    for notification in notifications:
        notification.is_read = True
        notification.read_at = utcnow()
    db.flush()
    return MessageResponse(message=f"{len(notifications)} notification(s) marked as read.")
