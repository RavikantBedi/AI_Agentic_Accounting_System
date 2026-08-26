"""Inventory service: movements, valuation and stock analytics.

Stock is an append-only ledger of ``StockMove`` rows, valued with weighted
average cost (FIFO is selectable per item and falls back to weighted average
for valuation reporting). ``Item.current_qty`` / ``stock_value`` are caches;
``rebuild_item_valuation`` recomputes them from the movements, which is what
makes the cache provable rather than merely assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import StockMoveType
from app.errors import InsufficientStockError, NotFoundError, ValidationError
from app.models import Item, StockBalance, StockMove, Warehouse
from app.money import ZERO, D, money, quantity as to_qty
from app.services.policy import get_policy

# Movement types that increase stock.
INWARD_TYPES = {
    StockMoveType.OPENING,
    StockMoveType.PURCHASE,
    StockMoveType.SALE_RETURN,
    StockMoveType.TRANSFER_IN,
    StockMoveType.MANUFACTURE,
}


@dataclass
class MoveResult:
    move: StockMove
    value: Decimal
    average_cost: Decimal
    balance_qty: Decimal


def default_warehouse(db: Session, company_id: str) -> Warehouse:
    warehouse = db.execute(
        select(Warehouse)
        .where(Warehouse.company_id == company_id, Warehouse.is_active.is_(True))
        .order_by(Warehouse.is_default.desc(), Warehouse.code.asc())
    ).scalars().first()
    if warehouse is None:
        raise NotFoundError("No warehouse is configured for this company.")
    return warehouse


def record_move(
    db: Session,
    company_id: str,
    *,
    item_id: str,
    warehouse_id: str | None,
    move_date: date,
    move_type: str,
    quantity: Decimal,
    rate: Decimal | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    reference: str = "",
    narration: str = "",
    journal_entry_id: str | None = None,
    user_id: str | None = None,
    allow_negative: bool | None = None,
) -> MoveResult:
    """Record one stock movement and update valuation.

    ``quantity`` is *signed*: positive increases stock, negative decreases it.
    Outward moves are valued at the current weighted-average cost, which is what
    makes COGS consistent with the inventory balance on the balance sheet.
    """
    item = db.get(Item, item_id)
    if item is None or item.company_id != company_id:
        raise NotFoundError("Item not found.")

    if not item.track_inventory:
        raise ValidationError(
            f"'{item.name}' is a non-stock item; it has no inventory to move."
        )

    qty = to_qty(quantity)
    if qty == 0:
        raise ValidationError("Stock movement quantity cannot be zero.")

    warehouse_id = warehouse_id or default_warehouse(db, company_id).id
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.company_id != company_id:
        raise NotFoundError("Warehouse not found.")

    if allow_negative is None:
        allow_negative = bool(
            get_policy(db, company_id, "inventory.alerts").get("negative_stock_allowed", False)
        )

    current_qty = to_qty(item.current_qty)
    current_value = money(item.stock_value)
    current_avg = money(item.average_cost)

    if qty > 0:
        # Inward: weighted average moves toward the new purchase rate.
        unit_rate = money(rate if rate is not None else current_avg or item.purchase_price)
        move_value = money(unit_rate * qty)
        new_qty = to_qty(current_qty + qty)
        new_value = money(current_value + move_value)
        new_avg = money(new_value / new_qty) if new_qty > 0 else ZERO
    else:
        outward_qty = -qty
        if current_qty < outward_qty and not allow_negative:
            raise InsufficientStockError(
                f"Only {current_qty} {item.uom} of '{item.name}' in stock; "
                f"{outward_qty} requested.",
                details={
                    "item_id": item.id,
                    "sku": item.sku,
                    "available": str(current_qty),
                    "requested": str(outward_qty),
                },
            )
        # Outward is valued at the running average, not at the sale price.
        #
        # The value released is the *proportional share of the carrying value*
        # rather than quantity x rounded-average. Multiplying by a 2dp average
        # leaves a paisa of residue on every issue, and across hundreds of
        # movements that residue accumulates into a real discrepancy between the
        # stock ledger and the inventory control account. Taking the share
        # keeps the two exactly equal, and empties the value precisely when the
        # last unit leaves.
        new_qty = to_qty(current_qty + qty)

        if rate is not None and money(rate) > ZERO:
            unit_rate = money(rate)
            move_value = money(-(unit_rate * outward_qty))
        elif current_qty > 0:
            if new_qty <= 0:
                # Issuing everything (or more): release the entire carrying value.
                move_value = money(-current_value)
            else:
                move_value = money(-(current_value * outward_qty / current_qty))
            unit_rate = (
                money(-move_value / outward_qty) if outward_qty > 0 else current_avg
            )
        else:
            # Issuing from empty stock (negative stock is allowed by policy).
            unit_rate = current_avg or money(item.purchase_price)
            move_value = money(-(unit_rate * outward_qty))

        new_value = money(current_value + move_value)
        if new_qty == 0:
            new_value = ZERO
            new_avg = current_avg
        elif new_qty < 0:
            new_avg = current_avg
        else:
            new_avg = money(new_value / new_qty)

    # Next position in this item's append-only ledger. Weighted-average cost
    # depends on the order moves are applied, so that order is recorded
    # explicitly rather than inferred from a timestamp that can tie.
    next_sequence = int(
        db.execute(
            select(func.coalesce(func.max(StockMove.sequence), 0)).where(
                StockMove.company_id == company_id, StockMove.item_id == item.id
            )
        ).scalar_one()
    ) + 1

    move = StockMove(
        company_id=company_id,
        item_id=item.id,
        warehouse_id=warehouse_id,
        sequence=next_sequence,
        move_date=move_date,
        move_type=move_type,
        quantity=qty,
        rate=unit_rate,
        value=move_value,
        balance_qty=new_qty,
        balance_value=new_value,
        average_cost_after=new_avg,
        source_type=source_type,
        source_id=source_id,
        reference=reference,
        narration=narration,
        journal_entry_id=journal_entry_id,
        created_by_user_id=user_id,
    )
    db.add(move)

    item.current_qty = new_qty
    item.stock_value = new_value
    item.average_cost = new_avg
    if qty > 0 and move_type == StockMoveType.PURCHASE:
        item.last_purchase_date = move_date
        item.purchase_price = unit_rate
    if qty < 0 and move_type == StockMoveType.SALE:
        item.last_sale_date = move_date

    _update_balance(db, company_id, item.id, warehouse_id, qty, move_value, move_date)
    db.flush()

    return MoveResult(move=move, value=move_value, average_cost=new_avg, balance_qty=new_qty)


def _update_balance(
    db: Session,
    company_id: str,
    item_id: str,
    warehouse_id: str,
    qty: Decimal,
    value: Decimal,
    move_date: date,
) -> StockBalance:
    balance = db.execute(
        select(StockBalance).where(
            StockBalance.company_id == company_id,
            StockBalance.item_id == item_id,
            StockBalance.warehouse_id == warehouse_id,
        )
    ).scalar_one_or_none()

    if balance is None:
        balance = StockBalance(
            company_id=company_id,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=ZERO,
            value=ZERO,
        )
        db.add(balance)
        db.flush()

    balance.quantity = to_qty(balance.quantity + qty)
    balance.value = money(balance.value + value)
    balance.last_move_date = move_date
    return balance


def transfer_stock(
    db: Session,
    company_id: str,
    *,
    item_id: str,
    from_warehouse_id: str,
    to_warehouse_id: str,
    quantity: Decimal,
    move_date: date,
    reference: str = "",
    source_id: str | None = None,
    user_id: str | None = None,
) -> tuple[MoveResult, MoveResult]:
    """Move stock between warehouses.

    A transfer is value-neutral overall: it is recorded as two moves at the
    same unit cost, so total inventory value (and the GL) is unchanged.
    """
    if from_warehouse_id == to_warehouse_id:
        raise ValidationError("Source and destination warehouses must differ.")

    item = db.get(Item, item_id)
    if item is None or item.company_id != company_id:
        raise NotFoundError("Item not found.")

    qty = to_qty(quantity)
    if qty <= 0:
        raise ValidationError("Transfer quantity must be positive.")

    available = warehouse_quantity(db, company_id, item_id, from_warehouse_id)
    if available < qty:
        raise InsufficientStockError(
            f"Only {available} {item.uom} of '{item.name}' at the source warehouse; "
            f"{qty} requested.",
            details={"available": str(available), "requested": str(qty)},
        )

    unit_rate = money(item.average_cost)
    out_move = record_move(
        db,
        company_id,
        item_id=item_id,
        warehouse_id=from_warehouse_id,
        move_date=move_date,
        move_type=StockMoveType.TRANSFER_OUT,
        quantity=-qty,
        rate=unit_rate,
        source_type="StockTransfer",
        source_id=source_id,
        reference=reference,
        narration="Warehouse transfer out",
        user_id=user_id,
    )
    in_move = record_move(
        db,
        company_id,
        item_id=item_id,
        warehouse_id=to_warehouse_id,
        move_date=move_date,
        move_type=StockMoveType.TRANSFER_IN,
        quantity=qty,
        rate=unit_rate,
        source_type="StockTransfer",
        source_id=source_id,
        reference=reference,
        narration="Warehouse transfer in",
        user_id=user_id,
    )
    return out_move, in_move


def warehouse_quantity(db: Session, company_id: str, item_id: str, warehouse_id: str) -> Decimal:
    balance = db.execute(
        select(StockBalance).where(
            StockBalance.company_id == company_id,
            StockBalance.item_id == item_id,
            StockBalance.warehouse_id == warehouse_id,
        )
    ).scalar_one_or_none()
    return to_qty(balance.quantity) if balance else ZERO


def rebuild_item_valuation(db: Session, company_id: str, item_id: str) -> dict:
    """Recompute an item's quantity/value from its movement history.

    This is the proof that the cached balance is correct — the close checklist
    and the test suite both use it.
    """
    item = db.get(Item, item_id)
    if item is None or item.company_id != company_id:
        raise NotFoundError("Item not found.")

    # Replayed in *application* order, not date order.
    #
    # This is deliberate and it matters. Weighted-average cost is order
    # dependent, and the value released by each outward move was posted to the
    # COGS and inventory-control accounts at the moment it was applied. Those
    # journal entries are immutable. Replaying a backdated purchase into its
    # chronological position would produce a different — arguably more
    # theoretically correct — valuation, but one the general ledger does not
    # reflect, and the check would then flag every backdated document as drift
    # while the books were in fact consistent.
    #
    # What this function proves is the property that is actually load-bearing:
    # the cached quantity and value on the item equal the sum of the recorded
    # movements, which equal what was posted to the ledger.
    moves = list(
        db.execute(
            select(StockMove)
            .where(StockMove.company_id == company_id, StockMove.item_id == item_id)
            .order_by(StockMove.sequence.asc(), StockMove.created_at.asc())
        ).scalars()
    )

    qty = ZERO
    value = ZERO
    average = ZERO
    for move in moves:
        if move.quantity > 0:
            qty = to_qty(qty + move.quantity)
            value = money(value + move.value)
            average = money(value / qty) if qty > 0 else ZERO
        else:
            qty = to_qty(qty + move.quantity)
            value = money(value + move.value)
            if qty <= 0:
                value = ZERO if qty == 0 else value
            else:
                average = money(value / qty)

    cached_qty = to_qty(item.current_qty)
    cached_value = money(item.stock_value)
    drift_qty = to_qty(cached_qty - qty)
    drift_value = money(cached_value - value)

    item.current_qty = qty
    item.stock_value = value
    item.average_cost = average
    db.flush()

    return {
        "item_id": item_id,
        "sku": item.sku,
        "moves": len(moves),
        "quantity": qty,
        "value": value,
        "average_cost": average,
        "drift_quantity": drift_qty,
        "drift_value": drift_value,
        "was_consistent": drift_qty == ZERO and drift_value == ZERO,
    }


def rebuild_all_valuations(db: Session, company_id: str) -> dict:
    items = list(
        db.execute(
            select(Item).where(Item.company_id == company_id, Item.track_inventory.is_(True))
        ).scalars()
    )
    results = [rebuild_item_valuation(db, company_id, item.id) for item in items]
    inconsistent = [r for r in results if not r["was_consistent"]]
    return {
        "items_checked": len(results),
        "inconsistent": len(inconsistent),
        "total_value": money(sum((r["value"] for r in results), ZERO)),
        "details": inconsistent[:50],
    }


def stock_summary(db: Session, company_id: str) -> dict:
    """Headline inventory numbers for the dashboard and the stock report."""
    items = list(
        db.execute(
            select(Item).where(Item.company_id == company_id, Item.is_active.is_(True))
        ).scalars()
    )
    tracked = [item for item in items if item.track_inventory]

    total_value = money(sum((money(item.stock_value) for item in tracked), ZERO))
    low_stock = [item for item in tracked if item.is_low_stock and to_qty(item.current_qty) > 0]
    out_of_stock = [item for item in tracked if to_qty(item.current_qty) <= 0]

    return {
        "total_items": len(items),
        "tracked_items": len(tracked),
        "total_stock_value": total_value,
        "low_stock_count": len(low_stock),
        "out_of_stock_count": len(out_of_stock),
        "low_stock_items": [
            {
                "id": item.id,
                "sku": item.sku,
                "name": item.name,
                "current_qty": to_qty(item.current_qty),
                "reorder_level": to_qty(item.reorder_level),
                "reorder_quantity": to_qty(item.reorder_quantity),
                "uom": item.uom,
                "stock_value": money(item.stock_value),
            }
            for item in sorted(low_stock, key=lambda i: to_qty(i.current_qty))[:25]
        ],
        "out_of_stock_items": [
            {"id": item.id, "sku": item.sku, "name": item.name, "uom": item.uom}
            for item in out_of_stock[:25]
        ],
    }


def movement_analysis(db: Session, company_id: str, *, as_of: date | None = None) -> dict:
    """Classify items as fast-moving, slow-moving or dead stock.

    Classification is by *measured* outward movement over the configured
    windows — never by guesswork.
    """
    as_of = as_of or date.today()
    policy = get_policy(db, company_id, "inventory.alerts")
    dead_days = int(policy.get("dead_stock_days", 90))
    slow_days = int(policy.get("slow_moving_days", 45))
    fast_percent = D(policy.get("fast_moving_top_percent", 20))

    window_start = as_of - timedelta(days=dead_days)

    rows = db.execute(
        select(
            StockMove.item_id,
            func.sum(-StockMove.quantity).label("outward"),
            func.max(StockMove.move_date).label("last_move"),
        )
        .where(
            StockMove.company_id == company_id,
            StockMove.move_type.in_([StockMoveType.SALE, StockMoveType.TRANSFER_OUT]),
            StockMove.move_date >= window_start,
            StockMove.move_date <= as_of,
        )
        .group_by(StockMove.item_id)
    ).all()

    outward_by_item = {row[0]: to_qty(row[1] or 0) for row in rows}

    items = list(
        db.execute(
            select(Item).where(
                Item.company_id == company_id,
                Item.is_active.is_(True),
                Item.track_inventory.is_(True),
            )
        ).scalars()
    )

    ranked = sorted(
        ((item, outward_by_item.get(item.id, ZERO)) for item in items),
        key=lambda pair: pair[1],
        reverse=True,
    )
    fast_cutoff = max(1, int(len(ranked) * float(fast_percent) / 100)) if ranked else 0

    fast, slow, dead = [], [], []
    for index, (item, outward) in enumerate(ranked):
        last_sale = item.last_sale_date
        days_since = (as_of - last_sale).days if last_sale else None
        entry = {
            "id": item.id,
            "sku": item.sku,
            "name": item.name,
            "current_qty": to_qty(item.current_qty),
            "stock_value": money(item.stock_value),
            "outward_qty": outward,
            "last_sale_date": last_sale,
            "days_since_sale": days_since,
        }
        if outward > 0 and index < fast_cutoff:
            fast.append(entry)
        elif to_qty(item.current_qty) > 0 and (days_since is None or days_since >= dead_days):
            entry["reason"] = (
                f"No sale in the last {dead_days} days"
                if days_since is None
                else f"Last sold {days_since} days ago"
            )
            dead.append(entry)
        elif to_qty(item.current_qty) > 0 and days_since is not None and days_since >= slow_days:
            entry["reason"] = f"Last sold {days_since} days ago"
            slow.append(entry)

    return {
        "as_of": as_of,
        "window_days": dead_days,
        "fast_moving": fast[:20],
        "slow_moving": sorted(slow, key=lambda e: e["days_since_sale"] or 0, reverse=True)[:20],
        "dead_stock": sorted(dead, key=lambda e: e["stock_value"], reverse=True)[:20],
        "dead_stock_value": money(sum((e["stock_value"] for e in dead), ZERO)),
        "slow_moving_value": money(sum((e["stock_value"] for e in slow), ZERO)),
    }


def item_ledger(
    db: Session,
    company_id: str,
    item_id: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 200,
) -> dict:
    """Movement history for one item with running balances."""
    item = db.get(Item, item_id)
    if item is None or item.company_id != company_id:
        raise NotFoundError("Item not found.")

    stmt = select(StockMove).where(
        StockMove.company_id == company_id, StockMove.item_id == item_id
    )
    if start_date:
        stmt = stmt.where(StockMove.move_date >= start_date)
    if end_date:
        stmt = stmt.where(StockMove.move_date <= end_date)

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    moves = list(
        db.execute(
            stmt.order_by(StockMove.sequence.desc()).limit(limit)
        ).scalars()
    )

    return {
        "item": {
            "id": item.id,
            "sku": item.sku,
            "name": item.name,
            "uom": item.uom,
            "current_qty": to_qty(item.current_qty),
            "average_cost": money(item.average_cost),
            "stock_value": money(item.stock_value),
        },
        "moves": [
            {
                "id": move.id,
                "date": move.move_date,
                "type": move.move_type,
                "quantity": to_qty(move.quantity),
                "rate": money(move.rate),
                "value": money(move.value),
                "balance_qty": to_qty(move.balance_qty),
                "balance_value": money(move.balance_value),
                "reference": move.reference,
                "narration": move.narration,
                "source_type": move.source_type,
                "source_id": move.source_id,
            }
            for move in moves
        ],
        "total": total,
    }
