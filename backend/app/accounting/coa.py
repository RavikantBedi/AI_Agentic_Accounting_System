"""Default chart of accounts for an Indian business, and COA management.

``SYSTEM_KEYS`` are the roles the engine resolves by name (AR control account,
GST output, retained earnings, …). Everything that posts automatically looks
accounts up by system key, so a company can rename or renumber its accounts
freely without breaking automation.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import AccountSubtype, AccountType, NormalBalance
from app.errors import ConflictError, ValidationError
from app.models import Account


@dataclass(frozen=True)
class AccountSpec:
    code: str
    name: str
    type: str
    subtype: str
    is_group: bool = False
    parent_code: str | None = None
    system_key: str | None = None
    is_reconcilable: bool = False
    tax_category: str = ""


def _normal_balance(account_type: str) -> str:
    """Assets and expenses increase on the debit side; the rest on the credit side."""
    return (
        NormalBalance.DEBIT
        if account_type in (AccountType.ASSET, AccountType.EXPENSE)
        else NormalBalance.CREDIT
    )


# System roles referenced by automated postings.
SYSTEM_KEYS = {
    "ACCOUNTS_RECEIVABLE",
    "ACCOUNTS_PAYABLE",
    "CASH",
    "BANK_DEFAULT",
    "INVENTORY",
    "SALES_REVENUE",
    "SERVICE_REVENUE",
    "COGS",
    "PURCHASES",
    "GST_OUTPUT_CGST",
    "GST_OUTPUT_SGST",
    "GST_OUTPUT_IGST",
    "GST_OUTPUT_CESS",
    "GST_INPUT_CGST",
    "GST_INPUT_SGST",
    "GST_INPUT_IGST",
    "GST_INPUT_CESS",
    "TDS_PAYABLE",
    "TDS_RECEIVABLE",
    "TCS_PAYABLE",
    "ROUND_OFF",
    "RETAINED_EARNINGS",
    "SHARE_CAPITAL",
    "SUSPENSE",
    "OPENING_BALANCE_EQUITY",
    "PREPAID_EXPENSES",
    "ACCRUED_EXPENSES",
    "SALARY_EXPENSE",
    "SALARY_PAYABLE",
    "PF_PAYABLE",
    "ESI_PAYABLE",
    "PT_PAYABLE",
    "DEPRECIATION_EXPENSE",
    "ACCUMULATED_DEPRECIATION",
    "FIXED_ASSETS",
    "STOCK_ADJUSTMENT",
    "DISCOUNT_ALLOWED",
    "DISCOUNT_RECEIVED",
    "BANK_CHARGES",
    "INTEREST_INCOME",
    "FX_GAIN_LOSS",
}


DEFAULT_COA: list[AccountSpec] = [
    # ============================ ASSETS ============================
    AccountSpec("1000", "Assets", AccountType.ASSET, AccountSubtype.OTHER_ASSET, is_group=True),
    AccountSpec("1100", "Current Assets", AccountType.ASSET, AccountSubtype.OTHER_CURRENT_ASSET, is_group=True, parent_code="1000"),
    AccountSpec("1110", "Cash and Cash Equivalents", AccountType.ASSET, AccountSubtype.CASH, is_group=True, parent_code="1100"),
    AccountSpec("1111", "Cash in Hand", AccountType.ASSET, AccountSubtype.CASH, parent_code="1110", system_key="CASH"),
    AccountSpec("1112", "Petty Cash", AccountType.ASSET, AccountSubtype.CASH, parent_code="1110"),
    AccountSpec("1120", "Bank Accounts", AccountType.ASSET, AccountSubtype.BANK, is_group=True, parent_code="1100"),
    AccountSpec("1121", "Primary Current Account", AccountType.ASSET, AccountSubtype.BANK, parent_code="1120", system_key="BANK_DEFAULT", is_reconcilable=True),
    AccountSpec("1130", "Accounts Receivable", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE, is_group=True, parent_code="1100"),
    AccountSpec("1131", "Trade Receivables", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE, parent_code="1130", system_key="ACCOUNTS_RECEIVABLE", is_reconcilable=True),
    AccountSpec("1140", "Inventory", AccountType.ASSET, AccountSubtype.INVENTORY, is_group=True, parent_code="1100"),
    AccountSpec("1141", "Stock in Hand", AccountType.ASSET, AccountSubtype.INVENTORY, parent_code="1140", system_key="INVENTORY"),
    AccountSpec("1150", "Input Tax Credit", AccountType.ASSET, AccountSubtype.INPUT_TAX, is_group=True, parent_code="1100"),
    AccountSpec("1151", "Input CGST", AccountType.ASSET, AccountSubtype.INPUT_TAX, parent_code="1150", system_key="GST_INPUT_CGST", tax_category="GST"),
    AccountSpec("1152", "Input SGST", AccountType.ASSET, AccountSubtype.INPUT_TAX, parent_code="1150", system_key="GST_INPUT_SGST", tax_category="GST"),
    AccountSpec("1153", "Input IGST", AccountType.ASSET, AccountSubtype.INPUT_TAX, parent_code="1150", system_key="GST_INPUT_IGST", tax_category="GST"),
    AccountSpec("1154", "Input Cess", AccountType.ASSET, AccountSubtype.INPUT_TAX, parent_code="1150", system_key="GST_INPUT_CESS", tax_category="GST"),
    AccountSpec("1160", "Prepaid Expenses", AccountType.ASSET, AccountSubtype.PREPAID_EXPENSE, parent_code="1100", system_key="PREPAID_EXPENSES"),
    AccountSpec("1170", "TDS Receivable", AccountType.ASSET, AccountSubtype.OTHER_CURRENT_ASSET, parent_code="1100", system_key="TDS_RECEIVABLE", tax_category="TDS"),
    AccountSpec("1180", "Advances to Vendors", AccountType.ASSET, AccountSubtype.OTHER_CURRENT_ASSET, parent_code="1100"),
    AccountSpec("1190", "Other Current Assets", AccountType.ASSET, AccountSubtype.OTHER_CURRENT_ASSET, parent_code="1100"),
    AccountSpec("1200", "Fixed Assets", AccountType.ASSET, AccountSubtype.FIXED_ASSET, is_group=True, parent_code="1000"),
    AccountSpec("1210", "Plant and Machinery", AccountType.ASSET, AccountSubtype.FIXED_ASSET, parent_code="1200", system_key="FIXED_ASSETS"),
    AccountSpec("1220", "Computers and IT Equipment", AccountType.ASSET, AccountSubtype.FIXED_ASSET, parent_code="1200"),
    AccountSpec("1230", "Furniture and Fixtures", AccountType.ASSET, AccountSubtype.FIXED_ASSET, parent_code="1200"),
    AccountSpec("1240", "Vehicles", AccountType.ASSET, AccountSubtype.FIXED_ASSET, parent_code="1200"),
    AccountSpec("1250", "Office Equipment", AccountType.ASSET, AccountSubtype.FIXED_ASSET, parent_code="1200"),
    AccountSpec("1290", "Accumulated Depreciation", AccountType.ASSET, AccountSubtype.ACCUMULATED_DEPRECIATION, parent_code="1200", system_key="ACCUMULATED_DEPRECIATION"),

    # ========================== LIABILITIES =========================
    AccountSpec("2000", "Liabilities", AccountType.LIABILITY, AccountSubtype.OTHER_LIABILITY, is_group=True),
    AccountSpec("2100", "Current Liabilities", AccountType.LIABILITY, AccountSubtype.OTHER_CURRENT_LIABILITY, is_group=True, parent_code="2000"),
    AccountSpec("2110", "Accounts Payable", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE, is_group=True, parent_code="2100"),
    AccountSpec("2111", "Trade Payables", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE, parent_code="2110", system_key="ACCOUNTS_PAYABLE", is_reconcilable=True),
    AccountSpec("2120", "Output Tax Liability", AccountType.LIABILITY, AccountSubtype.OUTPUT_TAX, is_group=True, parent_code="2100"),
    AccountSpec("2121", "Output CGST", AccountType.LIABILITY, AccountSubtype.OUTPUT_TAX, parent_code="2120", system_key="GST_OUTPUT_CGST", tax_category="GST"),
    AccountSpec("2122", "Output SGST", AccountType.LIABILITY, AccountSubtype.OUTPUT_TAX, parent_code="2120", system_key="GST_OUTPUT_SGST", tax_category="GST"),
    AccountSpec("2123", "Output IGST", AccountType.LIABILITY, AccountSubtype.OUTPUT_TAX, parent_code="2120", system_key="GST_OUTPUT_IGST", tax_category="GST"),
    AccountSpec("2124", "Output Cess", AccountType.LIABILITY, AccountSubtype.OUTPUT_TAX, parent_code="2120", system_key="GST_OUTPUT_CESS", tax_category="GST"),
    AccountSpec("2130", "Statutory Dues", AccountType.LIABILITY, AccountSubtype.TAX_PAYABLE, is_group=True, parent_code="2100"),
    AccountSpec("2131", "TDS Payable", AccountType.LIABILITY, AccountSubtype.TAX_PAYABLE, parent_code="2130", system_key="TDS_PAYABLE", tax_category="TDS"),
    AccountSpec("2132", "TCS Payable", AccountType.LIABILITY, AccountSubtype.TAX_PAYABLE, parent_code="2130", system_key="TCS_PAYABLE", tax_category="TCS"),
    AccountSpec("2133", "PF Payable", AccountType.LIABILITY, AccountSubtype.PAYROLL_LIABILITY, parent_code="2130", system_key="PF_PAYABLE"),
    AccountSpec("2134", "ESI Payable", AccountType.LIABILITY, AccountSubtype.PAYROLL_LIABILITY, parent_code="2130", system_key="ESI_PAYABLE"),
    AccountSpec("2135", "Professional Tax Payable", AccountType.LIABILITY, AccountSubtype.PAYROLL_LIABILITY, parent_code="2130", system_key="PT_PAYABLE"),
    AccountSpec("2140", "Salary Payable", AccountType.LIABILITY, AccountSubtype.PAYROLL_LIABILITY, parent_code="2100", system_key="SALARY_PAYABLE"),
    AccountSpec("2150", "Accrued Expenses", AccountType.LIABILITY, AccountSubtype.ACCRUED_LIABILITY, parent_code="2100", system_key="ACCRUED_EXPENSES"),
    AccountSpec("2160", "Advances from Customers", AccountType.LIABILITY, AccountSubtype.OTHER_CURRENT_LIABILITY, parent_code="2100"),
    AccountSpec("2190", "Suspense Account", AccountType.LIABILITY, AccountSubtype.SUSPENSE, parent_code="2100", system_key="SUSPENSE"),
    AccountSpec("2200", "Non-Current Liabilities", AccountType.LIABILITY, AccountSubtype.OTHER_LIABILITY, is_group=True, parent_code="2000"),
    AccountSpec("2210", "Long Term Loans", AccountType.LIABILITY, AccountSubtype.LOAN, parent_code="2200"),

    # ============================ EQUITY ============================
    AccountSpec("3000", "Equity", AccountType.EQUITY, AccountSubtype.SHARE_CAPITAL, is_group=True),
    AccountSpec("3100", "Share Capital", AccountType.EQUITY, AccountSubtype.SHARE_CAPITAL, parent_code="3000", system_key="SHARE_CAPITAL"),
    AccountSpec("3200", "Reserves and Surplus", AccountType.EQUITY, AccountSubtype.RESERVES, parent_code="3000"),
    AccountSpec("3300", "Retained Earnings", AccountType.EQUITY, AccountSubtype.RETAINED_EARNINGS, parent_code="3000", system_key="RETAINED_EARNINGS"),
    AccountSpec("3400", "Opening Balance Equity", AccountType.EQUITY, AccountSubtype.RESERVES, parent_code="3000", system_key="OPENING_BALANCE_EQUITY"),
    AccountSpec("3500", "Drawings", AccountType.EQUITY, AccountSubtype.DRAWINGS, parent_code="3000"),

    # =========================== REVENUE ============================
    AccountSpec("4000", "Revenue", AccountType.REVENUE, AccountSubtype.OPERATING_REVENUE, is_group=True),
    AccountSpec("4100", "Operating Revenue", AccountType.REVENUE, AccountSubtype.OPERATING_REVENUE, is_group=True, parent_code="4000"),
    AccountSpec("4110", "Product Sales", AccountType.REVENUE, AccountSubtype.OPERATING_REVENUE, parent_code="4100", system_key="SALES_REVENUE"),
    AccountSpec("4120", "Service Revenue", AccountType.REVENUE, AccountSubtype.OPERATING_REVENUE, parent_code="4100", system_key="SERVICE_REVENUE"),
    AccountSpec("4130", "Subscription Revenue", AccountType.REVENUE, AccountSubtype.OPERATING_REVENUE, parent_code="4100"),
    AccountSpec("4190", "Sales Returns and Allowances", AccountType.REVENUE, AccountSubtype.OPERATING_REVENUE, parent_code="4100"),
    AccountSpec("4200", "Other Income", AccountType.REVENUE, AccountSubtype.OTHER_INCOME, is_group=True, parent_code="4000"),
    AccountSpec("4210", "Interest Income", AccountType.REVENUE, AccountSubtype.OTHER_INCOME, parent_code="4200", system_key="INTEREST_INCOME"),
    AccountSpec("4220", "Discount Received", AccountType.REVENUE, AccountSubtype.OTHER_INCOME, parent_code="4200", system_key="DISCOUNT_RECEIVED"),
    AccountSpec("4230", "Foreign Exchange Gain/Loss", AccountType.REVENUE, AccountSubtype.OTHER_INCOME, parent_code="4200", system_key="FX_GAIN_LOSS"),
    AccountSpec("4290", "Miscellaneous Income", AccountType.REVENUE, AccountSubtype.OTHER_INCOME, parent_code="4200"),

    # =========================== EXPENSES ===========================
    AccountSpec("5000", "Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COGS, is_group=True),
    AccountSpec("5100", "Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COGS, parent_code="5000", system_key="COGS"),
    AccountSpec("5110", "Purchases", AccountType.EXPENSE, AccountSubtype.COGS, parent_code="5000", system_key="PURCHASES"),
    AccountSpec("5120", "Freight Inward", AccountType.EXPENSE, AccountSubtype.COGS, parent_code="5000"),
    AccountSpec("5130", "Stock Adjustment", AccountType.EXPENSE, AccountSubtype.COGS, parent_code="5000", system_key="STOCK_ADJUSTMENT"),

    AccountSpec("6000", "Operating Expenses", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, is_group=True),
    AccountSpec("6100", "Employee Costs", AccountType.EXPENSE, AccountSubtype.PAYROLL_EXPENSE, is_group=True, parent_code="6000"),
    AccountSpec("6110", "Salaries and Wages", AccountType.EXPENSE, AccountSubtype.PAYROLL_EXPENSE, parent_code="6100", system_key="SALARY_EXPENSE"),
    AccountSpec("6120", "Employer PF Contribution", AccountType.EXPENSE, AccountSubtype.PAYROLL_EXPENSE, parent_code="6100"),
    AccountSpec("6130", "Employer ESI Contribution", AccountType.EXPENSE, AccountSubtype.PAYROLL_EXPENSE, parent_code="6100"),
    AccountSpec("6140", "Staff Welfare", AccountType.EXPENSE, AccountSubtype.PAYROLL_EXPENSE, parent_code="6100"),
    AccountSpec("6150", "Recruitment Expenses", AccountType.EXPENSE, AccountSubtype.PAYROLL_EXPENSE, parent_code="6100"),

    AccountSpec("6200", "Office and Administration", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, is_group=True, parent_code="6000"),
    AccountSpec("6210", "Rent", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6200"),
    AccountSpec("6220", "Electricity and Utilities", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6200"),
    AccountSpec("6230", "Telephone and Internet", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6200"),
    AccountSpec("6240", "Office Supplies", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6200"),
    AccountSpec("6250", "Repairs and Maintenance", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6200"),
    AccountSpec("6260", "Insurance", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6200"),
    AccountSpec("6270", "Security and Housekeeping", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6200"),

    AccountSpec("6300", "Technology", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, is_group=True, parent_code="6000"),
    AccountSpec("6310", "Software Subscriptions", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6300"),
    AccountSpec("6320", "Cloud Hosting", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6300"),
    AccountSpec("6330", "IT Support and Services", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6300"),

    AccountSpec("6400", "Sales and Marketing", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, is_group=True, parent_code="6000"),
    AccountSpec("6410", "Advertising and Marketing", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6400"),
    AccountSpec("6420", "Travel and Conveyance", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6400"),
    AccountSpec("6430", "Business Promotion", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6400"),
    AccountSpec("6440", "Commission and Brokerage", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6400"),
    AccountSpec("6450", "Freight Outward", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6400"),

    AccountSpec("6500", "Professional and Legal", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, is_group=True, parent_code="6000"),
    AccountSpec("6510", "Professional Fees", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6500"),
    AccountSpec("6520", "Audit Fees", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6500"),
    AccountSpec("6530", "Legal Expenses", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6500"),
    AccountSpec("6540", "Consultancy Charges", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE, parent_code="6500"),

    AccountSpec("6600", "Finance Costs", AccountType.EXPENSE, AccountSubtype.FINANCE_COST, is_group=True, parent_code="6000"),
    AccountSpec("6610", "Bank Charges", AccountType.EXPENSE, AccountSubtype.FINANCE_COST, parent_code="6600", system_key="BANK_CHARGES"),
    AccountSpec("6620", "Interest Expense", AccountType.EXPENSE, AccountSubtype.FINANCE_COST, parent_code="6600"),
    AccountSpec("6630", "Discount Allowed", AccountType.EXPENSE, AccountSubtype.FINANCE_COST, parent_code="6600", system_key="DISCOUNT_ALLOWED"),

    AccountSpec("6700", "Depreciation and Amortisation", AccountType.EXPENSE, AccountSubtype.DEPRECIATION_EXPENSE, is_group=True, parent_code="6000"),
    AccountSpec("6710", "Depreciation", AccountType.EXPENSE, AccountSubtype.DEPRECIATION_EXPENSE, parent_code="6700", system_key="DEPRECIATION_EXPENSE"),

    AccountSpec("6800", "Other Expenses", AccountType.EXPENSE, AccountSubtype.OTHER_EXPENSE, is_group=True, parent_code="6000"),
    AccountSpec("6810", "Rates and Taxes", AccountType.EXPENSE, AccountSubtype.OTHER_EXPENSE, parent_code="6800"),
    AccountSpec("6820", "Round Off", AccountType.EXPENSE, AccountSubtype.OTHER_EXPENSE, parent_code="6800", system_key="ROUND_OFF"),
    AccountSpec("6830", "Bad Debts Written Off", AccountType.EXPENSE, AccountSubtype.OTHER_EXPENSE, parent_code="6800"),
    AccountSpec("6890", "Miscellaneous Expenses", AccountType.EXPENSE, AccountSubtype.OTHER_EXPENSE, parent_code="6800"),
]


def seed_chart_of_accounts(db: Session, company_id: str) -> dict[str, Account]:
    """Create the default COA for a company. Idempotent."""
    existing = {
        account.code: account
        for account in db.execute(
            select(Account).where(Account.company_id == company_id)
        ).scalars()
    }
    created: dict[str, Account] = dict(existing)

    for spec in DEFAULT_COA:
        if spec.code in created:
            continue
        parent = created.get(spec.parent_code) if spec.parent_code else None
        account = Account(
            company_id=company_id,
            code=spec.code,
            name=spec.name,
            type=spec.type,
            subtype=spec.subtype,
            normal_balance=_normal_balance(spec.type),
            parent_id=parent.id if parent else None,
            path=f"{parent.path}/{spec.code}" if parent else spec.code,
            depth=(parent.depth + 1) if parent else 0,
            is_group=spec.is_group,
            system_key=spec.system_key,
            is_system=bool(spec.system_key),
            is_reconcilable=spec.is_reconcilable,
            tax_category=spec.tax_category,
        )
        db.add(account)
        db.flush()
        created[spec.code] = account

    return created


def create_account(
    db: Session,
    company_id: str,
    *,
    code: str,
    name: str,
    type: str,
    subtype: str,
    parent_id: str | None = None,
    is_group: bool = False,
    tax_category: str = "",
    hsn_sac: str = "",
    description: str = "",
    is_reconcilable: bool = False,
) -> Account:
    """Create one account, enforcing hierarchy and uniqueness rules."""
    if type not in {t.value for t in AccountType}:
        raise ValidationError(f"'{type}' is not a valid account type.")

    duplicate = db.execute(
        select(Account).where(Account.company_id == company_id, Account.code == code)
    ).scalar_one_or_none()
    if duplicate:
        raise ConflictError(f"Account code {code} already exists.", code="duplicate_account_code")

    parent = None
    if parent_id:
        parent = db.get(Account, parent_id)
        if parent is None or parent.company_id != company_id:
            raise ValidationError("Parent account not found.")
        if not parent.is_group:
            raise ValidationError(
                f"'{parent.code} {parent.name}' is a posting account and cannot have children."
            )
        if parent.type != type:
            raise ValidationError(
                f"A {type} account cannot sit under a {parent.type} parent."
            )

    account = Account(
        company_id=company_id,
        code=code,
        name=name,
        type=type,
        subtype=subtype,
        normal_balance=_normal_balance(type),
        parent_id=parent.id if parent else None,
        path=f"{parent.path}/{code}" if parent else code,
        depth=(parent.depth + 1) if parent else 0,
        is_group=is_group,
        tax_category=tax_category,
        hsn_sac=hsn_sac,
        description=description,
        is_reconcilable=is_reconcilable,
    )
    db.add(account)
    db.flush()
    return account


def deactivate_account(db: Session, company_id: str, account_id: str) -> Account:
    """Deactivate an account. System accounts and accounts with activity resist."""
    from app.models import JournalLine  # local import: avoids a cycle

    account = db.get(Account, account_id)
    if account is None or account.company_id != company_id:
        raise ValidationError("Account not found.")
    if account.is_system:
        raise ConflictError(
            f"'{account.code} {account.name}' is a system account required by automated postings."
        )

    has_children = db.execute(
        select(Account.id).where(Account.parent_id == account_id, Account.is_active.is_(True)).limit(1)
    ).scalar_one_or_none()
    if has_children:
        raise ConflictError("Deactivate or move the child accounts first.")

    account.is_active = False
    db.flush()
    return account
