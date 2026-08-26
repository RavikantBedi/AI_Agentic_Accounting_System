"""Inventory: items, warehouses and an append-only stock ledger.

Stock is derived from immutable movements, exactly like the general ledger.
``StockBalance`` is a maintained cache, and ``services.inventory`` can rebuild
it from movements at any time — so the cache can always be proven correct.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Money, Quantity, Rate
from app.enums import ItemType, StockMoveType, ValuationMethod
from app.models.base import Base, CompanyScoped, SoftDelete, TimestampMixin, UUIDPk


class ItemCategory(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "item_categories"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_item_category"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("item_categories.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str] = mapped_column(Text, default="")


class UnitOfMeasure(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "units_of_measure"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_uom_code"),)

    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    decimal_places: Mapped[int] = mapped_column(Integer, default=2)


class Warehouse(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_warehouse_code"),)

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(100), default="")
    state_code: Mapped[str] = mapped_column(String(2), default="")
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    manager: Mapped[str] = mapped_column(String(150), default="")


class Item(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("company_id", "sku", name="uq_item_sku"),
        Index("ix_item_name", "company_id", "name"),
    )

    sku: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    item_type: Mapped[str] = mapped_column(String(10), default=ItemType.GOODS)
    category_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("item_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    uom: Mapped[str] = mapped_column(String(20), default="NOS")
    barcode: Mapped[str] = mapped_column(String(60), default="")
    brand: Mapped[str] = mapped_column(String(120), default="")

    hsn_sac: Mapped[str] = mapped_column(String(10), default="", index=True)
    gst_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("18.000000"))
    cess_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))

    purchase_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    selling_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    # Maintained by the valuation engine from stock movements.
    opening_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    opening_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    current_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    average_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    stock_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    reorder_level: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    reorder_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    max_stock_level: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7)

    valuation_method: Mapped[str] = mapped_column(String(20), default=ValuationMethod.WEIGHTED_AVERAGE)
    track_inventory: Mapped[bool] = mapped_column(Boolean, default=True)

    inventory_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    income_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expense_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    preferred_vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_sale_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    category: Mapped[ItemCategory | None] = relationship()

    @property
    def is_low_stock(self) -> bool:
        return bool(self.track_inventory) and self.current_qty <= self.reorder_level


class StockMove(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Append-only stock movement. Never updated after creation."""

    __tablename__ = "stock_moves"
    __table_args__ = (
        Index("ix_stock_move_item", "company_id", "item_id", "move_date"),
        Index("ix_stock_move_wh", "company_id", "warehouse_id", "move_date"),
        Index("ix_stock_move_source", "company_id", "source_type", "source_id"),
        Index("ix_stock_move_seq", "company_id", "item_id", "sequence"),
    )

    # The order in which this move was applied to the item, starting at 1.
    #
    # Weighted-average cost is order-dependent, so an append-only stock ledger
    # needs an unambiguous append order. ``created_at`` cannot serve: the clock
    # granularity on some platforms is coarser than the time it takes to write
    # several moves for one invoice, so timestamps tie and the replay order
    # becomes arbitrary. This is the order the moves were valued in, and it is
    # therefore the order the COGS journals were posted in.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=False)

    item_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    move_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    move_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Signed: positive increases stock, negative decreases it.
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    # Running state after this move (per item, company-wide).
    balance_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    balance_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    average_cost_after: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    source_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference: Mapped[str] = mapped_column(String(120), default="")
    narration: Mapped[str] = mapped_column(Text, default="")
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    item: Mapped[Item] = relationship()
    warehouse: Mapped[Warehouse] = relationship()


class StockBalance(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Per item+warehouse cache, rebuildable from ``StockMove``."""

    __tablename__ = "stock_balances"
    __table_args__ = (
        UniqueConstraint("company_id", "item_id", "warehouse_id", name="uq_stock_balance"),
    )

    item_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    last_move_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class StockTransfer(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "stock_transfers"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_stock_transfer_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    transfer_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    from_warehouse_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    to_warehouse_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    total_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["StockTransferLine"]] = relationship(cascade="all, delete-orphan")


class StockTransferLine(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "stock_transfer_lines"

    transfer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("stock_transfers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(32), ForeignKey("items.id"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))


class StockAdjustment(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Physical-count correction. Always posts a GL entry — stock is money."""

    __tablename__ = "stock_adjustments"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_stock_adjustment_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    adjustment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    warehouse_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(250), default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    total_value_impact: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["StockAdjustmentLine"]] = relationship(cascade="all, delete-orphan")


class StockAdjustmentLine(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "stock_adjustment_lines"

    adjustment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("stock_adjustments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(32), ForeignKey("items.id"), nullable=False)
    system_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    counted_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    difference_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    value_impact: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    reason: Mapped[str] = mapped_column(String(250), default="")
