"""Inventory endpoints: items, stock, warehouses, transfers and adjustments."""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.accounting.engine import AccountingEngine, EntryInput, LineInput
from app.accounting.periods import require_open_period
from app.deps import CurrentUserDep, DbDep, require_permission
from app.enums import ActorType, AuditAction, Permission, StockMoveType, VoucherType
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import Item, ItemCategory, StockAdjustment, StockAdjustmentLine, StockTransfer, StockTransferLine, Warehouse
from app.money import ZERO, D, money, quantity as to_qty
from app.schemas import (
    ItemCreate,
    ItemOut,
    MessageResponse,
    Page,
    StockAdjustmentRequest,
    StockTransferRequest,
    WarehouseOut,
)
from app.services import inventory as inventory_service
from app.services.numbering import next_number

router = APIRouter(prefix="/inventory", tags=["Inventory"])


@router.get("/items", response_model=Page[ItemOut])
def list_items(
    db: DbDep,
    current: CurrentUserDep,
    search: str | None = Query(default=None),
    low_stock_only: bool = Query(default=False),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ItemOut]:
    current.require(Permission.READ_INVENTORY.value)

    stmt = select(Item).where(Item.company_id == current.company_id)
    if not include_inactive:
        stmt = stmt.where(Item.is_active.is_(True))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Item.name).like(pattern)
            | func.lower(Item.sku).like(pattern)
            | func.lower(Item.hsn_sac).like(pattern)
        )
    if low_stock_only:
        stmt = stmt.where(Item.track_inventory.is_(True), Item.current_qty <= Item.reorder_level)

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    items = list(db.execute(stmt.order_by(Item.name).limit(limit).offset(offset)).scalars())

    results = []
    for item in items:
        out = ItemOut.model_validate(item)
        out.is_low_stock = item.is_low_stock
        out.category_name = item.category.name if item.category else None
        results.append(out)

    return Page(items=results, total=total, limit=limit, offset=offset)


@router.get("/items/{item_id}", response_model=ItemOut)
def get_item(item_id: str, db: DbDep, current: CurrentUserDep) -> ItemOut:
    current.require(Permission.READ_INVENTORY.value)
    item = db.get(Item, item_id)
    if item is None or item.company_id != current.company_id:
        raise NotFoundError("Item not found.")
    out = ItemOut.model_validate(item)
    out.is_low_stock = item.is_low_stock
    out.category_name = item.category.name if item.category else None
    return out


@router.post("/items", response_model=ItemOut, status_code=201)
def create_item(
    payload: ItemCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_INVENTORY.value))],
) -> ItemOut:
    existing = db.execute(
        select(Item).where(Item.company_id == current.company_id, Item.sku == payload.sku)
    ).scalar_one_or_none()
    if existing:
        raise ConflictError(f"SKU '{payload.sku}' already exists.")

    item = Item(
        company_id=current.company_id,
        sku=payload.sku,
        name=payload.name,
        description=payload.description,
        item_type=payload.item_type,
        category_id=payload.category_id,
        uom=payload.uom,
        hsn_sac=payload.hsn_sac,
        gst_rate=D(payload.gst_rate),
        purchase_price=money(payload.purchase_price),
        selling_price=money(payload.selling_price),
        reorder_level=to_qty(payload.reorder_level),
        reorder_quantity=to_qty(payload.reorder_quantity),
        track_inventory=payload.track_inventory,
    )
    db.add(item)
    db.flush()

    # Opening stock posts a real movement and a real journal entry.
    if payload.track_inventory and to_qty(payload.opening_qty) > 0:
        _post_opening_stock(db, current, item, payload.opening_qty, payload.opening_rate)

    out = ItemOut.model_validate(item)
    out.is_low_stock = item.is_low_stock
    return out


def _post_opening_stock(db, current, item: Item, quantity, rate) -> None:
    """Opening stock: Dr Inventory, Cr Opening Balance Equity."""
    opening_date = date.today()
    engine = AccountingEngine(db, current.company_id, actor_user_id=current.id)
    unit_rate = money(rate) if money(rate) > ZERO else money(item.purchase_price)
    value = money(to_qty(quantity) * unit_rate)
    if value <= ZERO:
        raise ValidationError("Opening stock needs a rate so it can be valued.")

    require_open_period(db, current.company_id, opening_date)
    entry = engine.create_and_post(
        EntryInput(
            entry_date=opening_date,
            voucher_type=VoucherType.OPENING,
            narration=f"Opening stock for {item.sku} — {item.name}",
            is_opening=True,
            lines=[
                LineInput(
                    account_id=engine.account_by_system_key("INVENTORY").id,
                    debit=value,
                    description=f"Opening stock — {item.name}",
                    item_id=item.id,
                ),
                LineInput(
                    account_id=engine.account_by_system_key("OPENING_BALANCE_EQUITY").id,
                    credit=value,
                    description=f"Opening stock — {item.name}",
                ),
            ],
        )
    )
    inventory_service.record_move(
        db,
        current.company_id,
        item_id=item.id,
        warehouse_id=None,
        move_date=opening_date,
        move_type=StockMoveType.OPENING,
        quantity=to_qty(quantity),
        rate=unit_rate,
        source_type="OpeningStock",
        source_id=item.id,
        narration="Opening stock",
        journal_entry_id=entry.id,
        user_id=current.id,
    )
    item.opening_qty = to_qty(quantity)
    item.opening_rate = unit_rate
    db.flush()


@router.get("/summary")
def stock_summary(db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_INVENTORY.value)
    return inventory_service.stock_summary(db, current.company_id)


@router.get("/movement-analysis")
def movement_analysis(
    db: DbDep, current: CurrentUserDep, as_of: date | None = Query(default=None)
) -> dict:
    """Fast-moving, slow-moving and dead stock, measured from real movements."""
    current.require(Permission.READ_INVENTORY.value)
    return inventory_service.movement_analysis(db, current.company_id, as_of=as_of)


@router.get("/items/{item_id}/ledger")
def item_ledger(
    item_id: str,
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    current.require(Permission.READ_INVENTORY.value)
    return inventory_service.item_ledger(
        db, current.company_id, item_id, start_date=start_date, end_date=end_date, limit=limit
    )


@router.get("/warehouses", response_model=list[WarehouseOut])
def list_warehouses(db: DbDep, current: CurrentUserDep) -> list[WarehouseOut]:
    current.require(Permission.READ_INVENTORY.value)
    warehouses = list(
        db.execute(
            select(Warehouse)
            .where(Warehouse.company_id == current.company_id, Warehouse.is_active.is_(True))
            .order_by(Warehouse.is_default.desc(), Warehouse.name)
        ).scalars()
    )
    return [WarehouseOut.model_validate(warehouse) for warehouse in warehouses]


@router.post("/transfers", status_code=201)
def create_transfer(
    payload: StockTransferRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_INVENTORY.value))],
) -> dict:
    """Move stock between warehouses. Value-neutral, so no GL entry is needed."""
    transfer = StockTransfer(
        company_id=current.company_id,
        number=next_number(db, current.company_id, "STOCK_TRANSFER", payload.transfer_date),
        transfer_date=payload.transfer_date,
        from_warehouse_id=payload.from_warehouse_id,
        to_warehouse_id=payload.to_warehouse_id,
        status="POSTED",
        created_by_user_id=current.id,
    )
    db.add(transfer)
    db.flush()

    out_move, in_move = inventory_service.transfer_stock(
        db,
        current.company_id,
        item_id=payload.item_id,
        from_warehouse_id=payload.from_warehouse_id,
        to_warehouse_id=payload.to_warehouse_id,
        quantity=payload.quantity,
        move_date=payload.transfer_date,
        reference=payload.reference or transfer.number,
        source_id=transfer.id,
        user_id=current.id,
    )

    value = money(abs(out_move.value))
    transfer.total_value = value
    db.add(
        StockTransferLine(
            company_id=current.company_id,
            transfer_id=transfer.id,
            item_id=payload.item_id,
            quantity=to_qty(payload.quantity),
            rate=out_move.average_cost,
            value=value,
        )
    )
    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.CREATE,
        entity_type="StockTransfer",
        entity_id=transfer.id,
        entity_label=transfer.number,
        summary=f"Transferred {payload.quantity} units between warehouses ({value})",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        amount=value,
    )

    return {
        "id": transfer.id,
        "number": transfer.number,
        "value": str(value),
        "from_balance": str(out_move.balance_qty),
        "to_balance": str(in_move.balance_qty),
    }


@router.post("/adjustments", status_code=201)
def create_adjustment(
    payload: StockAdjustmentRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_INVENTORY.value))],
) -> dict:
    """Correct stock to a physical count. Always posts a GL entry — stock is money."""
    require_open_period(db, current.company_id, payload.adjustment_date)
    engine = AccountingEngine(db, current.company_id, actor_user_id=current.id)

    adjustment = StockAdjustment(
        company_id=current.company_id,
        number=next_number(db, current.company_id, "STOCK_ADJUSTMENT", payload.adjustment_date),
        adjustment_date=payload.adjustment_date,
        warehouse_id=payload.warehouse_id,
        reason=payload.reason,
        status="POSTED",
        approved_by_user_id=current.id,
        created_by_user_id=current.id,
    )
    db.add(adjustment)
    db.flush()

    total_impact = ZERO
    lines_out = []

    for line in payload.lines:
        item = db.get(Item, line.item_id)
        if item is None or item.company_id != current.company_id:
            raise NotFoundError(f"Item {line.item_id} not found.")

        system_qty = to_qty(item.current_qty)
        counted = to_qty(line.counted_qty)
        difference = to_qty(counted - system_qty)
        if difference == 0:
            continue

        rate = money(item.average_cost) or money(item.purchase_price)
        impact = money(difference * rate)
        total_impact = money(total_impact + impact)

        inventory_service.record_move(
            db,
            current.company_id,
            item_id=item.id,
            warehouse_id=payload.warehouse_id,
            move_date=payload.adjustment_date,
            move_type=StockMoveType.ADJUSTMENT,
            quantity=difference,
            rate=rate,
            source_type="StockAdjustment",
            source_id=adjustment.id,
            reference=adjustment.number,
            narration=f"Stock adjustment: {line.reason or payload.reason}",
            user_id=current.id,
            allow_negative=True,
        )

        db.add(
            StockAdjustmentLine(
                company_id=current.company_id,
                adjustment_id=adjustment.id,
                item_id=item.id,
                system_qty=system_qty,
                counted_qty=counted,
                difference_qty=difference,
                rate=rate,
                value_impact=impact,
                reason=line.reason,
            )
        )
        lines_out.append(
            {
                "item_id": item.id,
                "sku": item.sku,
                "system_qty": str(system_qty),
                "counted_qty": str(counted),
                "difference": str(difference),
                "value_impact": str(impact),
            }
        )

    if total_impact == ZERO:
        adjustment.status = "NO_CHANGE"
        db.flush()
        return {
            "id": adjustment.id,
            "number": adjustment.number,
            "value_impact": "0.00",
            "lines": lines_out,
            "message": "The physical count matches the system; no adjustment was needed.",
        }

    inventory_account = engine.account_by_system_key("INVENTORY")
    adjustment_account = engine.account_by_system_key("STOCK_ADJUSTMENT")

    if total_impact > ZERO:
        # Stock found: increase inventory, credit the adjustment expense.
        entry_lines = [
            LineInput(account_id=inventory_account.id, debit=total_impact, description="Stock surplus"),
            LineInput(account_id=adjustment_account.id, credit=total_impact, description="Stock surplus"),
        ]
    else:
        entry_lines = [
            LineInput(account_id=adjustment_account.id, debit=-total_impact, description="Stock shortage"),
            LineInput(account_id=inventory_account.id, credit=-total_impact, description="Stock shortage"),
        ]

    entry = engine.create_and_post(
        EntryInput(
            entry_date=payload.adjustment_date,
            voucher_type=VoucherType.STOCK_JOURNAL,
            narration=f"Stock adjustment {adjustment.number}: {payload.reason}",
            reference=adjustment.number,
            lines=entry_lines,
            source_type="StockAdjustment",
            source_id=adjustment.id,
        )
    )

    adjustment.total_value_impact = total_impact
    adjustment.journal_entry_id = entry.id
    db.flush()

    return {
        "id": adjustment.id,
        "number": adjustment.number,
        "value_impact": str(total_impact),
        "journal_entry_id": entry.id,
        "lines": lines_out,
    }


@router.post("/rebuild-valuation")
def rebuild_valuation(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_INVENTORY.value))],
) -> dict:
    """Recompute every item's stock value from its movements.

    Proves the cached balances match the movement history.
    """
    result = inventory_service.rebuild_all_valuations(db, current.company_id)
    return {
        **result,
        "total_value": str(result["total_value"]),
        "message": (
            f"Verified {result['items_checked']} item(s) against their movement history; "
            f"{result['inconsistent']} needed correction."
        ),
    }


@router.get("/categories")
def list_categories(db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_INVENTORY.value)
    categories = list(
        db.execute(
            select(ItemCategory).where(
                ItemCategory.company_id == current.company_id, ItemCategory.is_active.is_(True)
            )
        ).scalars()
    )
    return {
        "categories": [
            {"id": category.id, "name": category.name, "parent_id": category.parent_id}
            for category in categories
        ]
    }
