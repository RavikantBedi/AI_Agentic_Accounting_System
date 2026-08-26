"""Payroll.

Scope note: this implements payroll *accounting* — structured salary
components, deductions, TDS on salary using configured rules, payslips and the
resulting journal entry. It does not claim statutory PF/ESI/PT filing
compliance; see docs/TAX.md for exactly what is and is not covered.
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

from app.database import Money, Rate
from app.enums import EmployeeStatus, PayrollStatus
from app.models.base import Base, CompanyScoped, SoftDelete, TimestampMixin, UUIDPk


class Employee(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("company_id", "employee_code", name="uq_employee_code"),
        Index("ix_employee_name", "company_id", "full_name"),
    )

    employee_code: Mapped[str] = mapped_column(String(30), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")

    department_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    department: Mapped[str] = mapped_column(String(120), default="")
    designation: Mapped[str] = mapped_column(String(120), default="")
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    joining_date: Mapped[date] = mapped_column(Date, nullable=False)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=EmployeeStatus.ACTIVE, index=True)

    annual_ctc: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    monthly_gross: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    basic_percent: Mapped[Decimal] = mapped_column(Rate, default=Decimal("40.000000"))

    pan: Mapped[str] = mapped_column(String(10), default="")
    uan: Mapped[str] = mapped_column(String(12), default="")
    tax_regime: Mapped[str] = mapped_column(String(10), default="NEW")  # NEW | OLD
    declared_investments: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    # Bank details are referenced, never re-keyed at payment time.
    bank_account_name: Mapped[str] = mapped_column(String(200), default="")
    bank_account_number: Mapped[str] = mapped_column(String(40), default="")
    bank_ifsc: Mapped[str] = mapped_column(String(11), default="")

    pf_applicable: Mapped[bool] = mapped_column(Boolean, default=True)
    esi_applicable: Mapped[bool] = mapped_column(Boolean, default=False)
    professional_tax_applicable: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class SalaryComponent(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Configurable earning/deduction definition — no hardcoded pay structure."""

    __tablename__ = "salary_components"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_salary_component"),)

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="EARNING")  # EARNING | DEDUCTION
    calculation: Mapped[str] = mapped_column(String(20), default="FIXED")  # FIXED | PERCENT_OF_BASIC | PERCENT_OF_GROSS
    value: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    is_taxable: Mapped[bool] = mapped_column(Boolean, default=True)
    account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PayrollRun(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "payroll_runs"
    __table_args__ = (UniqueConstraint("company_id", "period", name="uq_payroll_period"),)

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # 2025-04
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=PayrollStatus.DRAFT, index=True)

    employee_count: Mapped[int] = mapped_column(Integer, default=0)
    total_gross: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_earnings: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_deductions: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_tds: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_employer_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_net: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    payslips: Mapped[list["Payslip"]] = relationship(cascade="all, delete-orphan")


class Payslip(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "payslips"
    __table_args__ = (
        UniqueConstraint("payroll_run_id", "employee_id", name="uq_payslip_employee"),
        Index("ix_payslip_employee", "company_id", "employee_id", "period"),
    )

    payroll_run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)

    days_in_period: Mapped[int] = mapped_column(Integer, default=30)
    days_paid: Mapped[Decimal] = mapped_column(Rate, default=Decimal("30.000000"))
    lop_days: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))

    basic: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    hra: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    other_allowances: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    gross_earnings: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    pf_employee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    pf_employer: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    esi_employee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    esi_employer: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    professional_tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    tds: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    other_deductions: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_deductions: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    net_pay: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    employer_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    breakdown: Mapped[list] = mapped_column(default=list)  # component-level detail
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    employee: Mapped[Employee] = relationship(lazy="joined")
