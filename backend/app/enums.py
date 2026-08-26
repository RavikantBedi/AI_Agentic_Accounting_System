"""Canonical domain vocabulary shared by models, schemas, services and the AI layer."""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Python 3.10-compatible string enum."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# ---------------------------------------------------------------- identity --

class UserRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    ACCOUNTANT = "ACCOUNTANT"
    FINANCE_MANAGER = "FINANCE_MANAGER"
    SALES_MANAGER = "SALES_MANAGER"
    PURCHASE_MANAGER = "PURCHASE_MANAGER"
    AUDITOR = "AUDITOR"
    VIEWER = "VIEWER"


class BusinessType(StrEnum):
    PRIVATE_LIMITED = "PRIVATE_LIMITED"
    PUBLIC_LIMITED = "PUBLIC_LIMITED"
    LLP = "LLP"
    PARTNERSHIP = "PARTNERSHIP"
    PROPRIETORSHIP = "PROPRIETORSHIP"
    TRUST = "TRUST"
    OTHER = "OTHER"


# -------------------------------------------------------------- accounting --

class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class AccountSubtype(StrEnum):
    # Assets
    CASH = "CASH"
    BANK = "BANK"
    ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE"
    INVENTORY = "INVENTORY"
    PREPAID_EXPENSE = "PREPAID_EXPENSE"
    FIXED_ASSET = "FIXED_ASSET"
    ACCUMULATED_DEPRECIATION = "ACCUMULATED_DEPRECIATION"
    INPUT_TAX = "INPUT_TAX"
    OTHER_CURRENT_ASSET = "OTHER_CURRENT_ASSET"
    OTHER_ASSET = "OTHER_ASSET"
    # Liabilities
    ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE"
    OUTPUT_TAX = "OUTPUT_TAX"
    TAX_PAYABLE = "TAX_PAYABLE"
    ACCRUED_LIABILITY = "ACCRUED_LIABILITY"
    PAYROLL_LIABILITY = "PAYROLL_LIABILITY"
    LOAN = "LOAN"
    OTHER_CURRENT_LIABILITY = "OTHER_CURRENT_LIABILITY"
    OTHER_LIABILITY = "OTHER_LIABILITY"
    SUSPENSE = "SUSPENSE"
    # Equity
    SHARE_CAPITAL = "SHARE_CAPITAL"
    RETAINED_EARNINGS = "RETAINED_EARNINGS"
    DRAWINGS = "DRAWINGS"
    RESERVES = "RESERVES"
    # Revenue
    OPERATING_REVENUE = "OPERATING_REVENUE"
    OTHER_INCOME = "OTHER_INCOME"
    # Expense
    COGS = "COGS"
    OPERATING_EXPENSE = "OPERATING_EXPENSE"
    PAYROLL_EXPENSE = "PAYROLL_EXPENSE"
    DEPRECIATION_EXPENSE = "DEPRECIATION_EXPENSE"
    FINANCE_COST = "FINANCE_COST"
    TAX_EXPENSE = "TAX_EXPENSE"
    OTHER_EXPENSE = "OTHER_EXPENSE"


class NormalBalance(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class PeriodStatus(StrEnum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class JournalStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    POSTED = "POSTED"
    REVERSED = "REVERSED"
    VOID = "VOID"


class VoucherType(StrEnum):
    SALES = "SALES"
    PURCHASE = "PURCHASE"
    PAYMENT = "PAYMENT"
    RECEIPT = "RECEIPT"
    CONTRA = "CONTRA"
    JOURNAL = "JOURNAL"
    DEBIT_NOTE = "DEBIT_NOTE"
    CREDIT_NOTE = "CREDIT_NOTE"
    EXPENSE = "EXPENSE"
    TRANSFER = "TRANSFER"
    STOCK_JOURNAL = "STOCK_JOURNAL"
    DELIVERY_NOTE = "DELIVERY_NOTE"
    RECEIPT_NOTE = "RECEIPT_NOTE"
    PAYROLL = "PAYROLL"
    DEPRECIATION = "DEPRECIATION"
    ACCRUAL = "ACCRUAL"
    PREPAID = "PREPAID"
    OPENING = "OPENING"
    CLOSING = "CLOSING"


class EntrySource(StrEnum):
    MANUAL = "MANUAL"
    AI_PROPOSAL = "AI_PROPOSAL"
    IMPORT = "IMPORT"
    SYSTEM = "SYSTEM"
    RECURRING = "RECURRING"
    RECONCILIATION = "RECONCILIATION"


# ----------------------------------------------------------------- banking --

class BankAccountType(StrEnum):
    CURRENT = "CURRENT"
    SAVINGS = "SAVINGS"
    OD = "OD"
    CC = "CC"
    WALLET = "WALLET"


class BankTxnDirection(StrEnum):
    CREDIT = "CREDIT"   # money in
    DEBIT = "DEBIT"     # money out


class ReconStatus(StrEnum):
    MATCHED = "MATCHED"
    PARTIAL = "PARTIAL"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    EXCEPTION = "EXCEPTION"


class MatchStrategy(StrEnum):
    EXACT_REFERENCE = "EXACT_REFERENCE"
    EXACT_AMOUNT = "EXACT_AMOUNT"
    DATE_TOLERANCE = "DATE_TOLERANCE"
    DESCRIPTION_NORMALISED = "DESCRIPTION_NORMALISED"
    COUNTERPARTY = "COUNTERPARTY"
    HISTORICAL_PATTERN = "HISTORICAL_PATTERN"
    FUZZY_SCORE = "FUZZY_SCORE"
    AI_INVESTIGATION = "AI_INVESTIGATION"
    MANUAL = "MANUAL"


# ------------------------------------------------------- sales / purchases --

class DocumentStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SENT = "SENT"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class OrderStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"
    FULFILLED = "FULFILLED"
    INVOICED = "INVOICED"
    CANCELLED = "CANCELLED"


class PaymentMethod(StrEnum):
    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    NEFT = "NEFT"
    RTGS = "RTGS"
    IMPS = "IMPS"
    UPI = "UPI"
    CHEQUE = "CHEQUE"
    CARD = "CARD"
    OTHER = "OTHER"


class PartyType(StrEnum):
    CUSTOMER = "CUSTOMER"
    VENDOR = "VENDOR"
    EMPLOYEE = "EMPLOYEE"


# --------------------------------------------------------------- inventory --

class StockMoveType(StrEnum):
    OPENING = "OPENING"
    PURCHASE = "PURCHASE"
    SALE = "SALE"
    SALE_RETURN = "SALE_RETURN"
    PURCHASE_RETURN = "PURCHASE_RETURN"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    ADJUSTMENT = "ADJUSTMENT"
    MANUFACTURE = "MANUFACTURE"


class ValuationMethod(StrEnum):
    FIFO = "FIFO"
    WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE"


class ItemType(StrEnum):
    GOODS = "GOODS"
    SERVICE = "SERVICE"


# --------------------------------------------------------------------- tax --

class GSTTreatment(StrEnum):
    REGISTERED = "REGISTERED"
    UNREGISTERED = "UNREGISTERED"
    COMPOSITION = "COMPOSITION"
    SEZ = "SEZ"
    SEZ_WITHOUT_PAYMENT = "SEZ_WITHOUT_PAYMENT"
    EXPORT = "EXPORT"
    EXPORT_WITHOUT_PAYMENT = "EXPORT_WITHOUT_PAYMENT"
    OVERSEAS = "OVERSEAS"
    DEEMED_EXPORT = "DEEMED_EXPORT"


class SupplyType(StrEnum):
    INTRA_STATE = "INTRA_STATE"
    INTER_STATE = "INTER_STATE"
    EXPORT = "EXPORT"
    EXEMPT = "EXEMPT"
    NIL_RATED = "NIL_RATED"
    NON_GST = "NON_GST"


class TaxComponent(StrEnum):
    CGST = "CGST"
    SGST = "SGST"
    IGST = "IGST"
    UTGST = "UTGST"
    CESS = "CESS"


class ReturnStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PREPARED = "PREPARED"
    REVIEWED = "REVIEWED"
    FILED_EXTERNALLY = "FILED_EXTERNALLY"


class EDocStatus(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProviderMode(StrEnum):
    """Whether a regulatory/banking response came from a live API or a mock."""

    MOCK = "MOCK"
    LIVE = "LIVE"


# ----------------------------------------------------------------- payroll --

class PayrollStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    POSTED = "POSTED"
    PAID = "PAID"


class EmployeeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ON_LEAVE = "ON_LEAVE"
    RESIGNED = "RESIGNED"
    TERMINATED = "TERMINATED"


# ---------------------------------------------------------------- schedule --

class ScheduleFrequency(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"


class ScheduleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class DepreciationMethod(StrEnum):
    STRAIGHT_LINE = "STRAIGHT_LINE"
    WDV = "WDV"


class AssetStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FULLY_DEPRECIATED = "FULLY_DEPRECIATED"
    DISPOSED = "DISPOSED"


# ---------------------------------------------------------------------- AI --

class AgentKind(StrEnum):
    BOOKKEEPING = "BOOKKEEPING"
    RECONCILIATION = "RECONCILIATION"
    AP = "AP"
    AR = "AR"
    DOCUMENT = "DOCUMENT"
    INVENTORY = "INVENTORY"
    GST = "GST"
    TAX = "TAX"
    ANOMALY = "ANOMALY"
    FRAUD = "FRAUD"
    CASH_FORECAST = "CASH_FORECAST"
    REPORTING = "REPORTING"
    CLOSE = "CLOSE"
    AUDIT = "AUDIT"
    PAYROLL = "PAYROLL"
    EXPENSE = "EXPENSE"
    ANALYST = "ANALYST"
    COPILOT = "COPILOT"


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class ProposalType(StrEnum):
    JOURNAL = "JOURNAL"
    RECONCILIATION_MATCH = "RECONCILIATION_MATCH"
    INVOICE = "INVOICE"
    BILL = "BILL"
    ADJUSTMENT = "ADJUSTMENT"
    CATEGORISATION = "CATEGORISATION"
    PAYMENT_PRIORITY = "PAYMENT_PRIORITY"
    COLLECTION_ACTION = "COLLECTION_ACTION"
    INVENTORY_ACTION = "INVENTORY_ACTION"
    CLOSE_TASK = "CLOSE_TASK"


class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    AUTO_APPROVED = "AUTO_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class Permission(StrEnum):
    # read
    READ_ACCOUNTS = "read_accounts"
    READ_TRANSACTIONS = "read_transactions"
    READ_INVOICES = "read_invoices"
    READ_BILLS = "read_bills"
    READ_INVENTORY = "read_inventory"
    READ_REPORTS = "read_reports"
    READ_TAX = "read_tax"
    READ_PAYROLL = "read_payroll"
    READ_AUDIT = "read_audit"
    # propose
    PROPOSE_JOURNAL = "propose_journal"
    PROPOSE_RECONCILIATION = "propose_reconciliation"
    PROPOSE_INVOICE = "propose_invoice"
    PROPOSE_BILL = "propose_bill"
    PROPOSE_ADJUSTMENT = "propose_adjustment"
    # execute (never granted to an AI agent)
    POST_JOURNAL = "post_journal"
    APPROVE_PROPOSAL = "approve_proposal"
    MANAGE_SALES = "manage_sales"
    MANAGE_PURCHASES = "manage_purchases"
    MANAGE_INVENTORY = "manage_inventory"
    MANAGE_PAYROLL = "manage_payroll"
    MANAGE_TAX = "manage_tax"
    RELEASE_PAYMENT = "release_payment"
    CLOSE_PERIOD = "close_period"
    CHANGE_ACCOUNTING_POLICY = "change_accounting_policy"
    MANAGE_USERS = "manage_users"
    MANAGE_SETTINGS = "manage_settings"


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ExceptionCategory(StrEnum):
    ACCOUNTING = "ACCOUNTING"
    BANKING = "BANKING"
    AR = "AR"
    AP = "AP"
    GST = "GST"
    TAX = "TAX"
    INVENTORY = "INVENTORY"
    PAYROLL = "PAYROLL"
    ANOMALY = "ANOMALY"
    CLOSE = "CLOSE"


class ExceptionStatus(StrEnum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class AnomalyKind(StrEnum):
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    DUPLICATE_BILL = "DUPLICATE_BILL"
    UNUSUAL_VENDOR = "UNUSUAL_VENDOR"
    NEW_VENDOR = "NEW_VENDOR"
    LARGE_TRANSACTION = "LARGE_TRANSACTION"
    WEEKEND_TRANSACTION = "WEEKEND_TRANSACTION"
    AFTER_HOURS_JOURNAL = "AFTER_HOURS_JOURNAL"
    UNUSUAL_CATEGORY = "UNUSUAL_CATEGORY"
    EXPENSE_SPIKE = "EXPENSE_SPIKE"
    UNUSUAL_JOURNAL = "UNUSUAL_JOURNAL"
    ROUND_NUMBER = "ROUND_NUMBER"
    UNEXPECTED_TAX = "UNEXPECTED_TAX"
    INVENTORY_ANOMALY = "INVENTORY_ANOMALY"
    VENDOR_BANK_CHANGE = "VENDOR_BANK_CHANGE"
    DUPLICATE_BANK_POSTING = "DUPLICATE_BANK_POSTING"


# ------------------------------------------------------------------- close --

class CloseTaskStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


# ------------------------------------------------------------------ audit --

class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    POST = "POST"
    REVERSE = "REVERSE"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    EXPORT = "EXPORT"
    IMPORT = "IMPORT"
    CLOSE_PERIOD = "CLOSE_PERIOD"
    REOPEN_PERIOD = "REOPEN_PERIOD"
    AI_PROPOSAL = "AI_PROPOSAL"
    AI_RUN = "AI_RUN"
    POLICY_CHANGE = "POLICY_CHANGE"
    PERMISSION_DENIED = "PERMISSION_DENIED"


class ActorType(StrEnum):
    USER = "USER"
    AI_AGENT = "AI_AGENT"
    SYSTEM = "SYSTEM"


class DocumentKind(StrEnum):
    INVOICE = "INVOICE"
    BILL = "BILL"
    RECEIPT = "RECEIPT"
    BANK_STATEMENT = "BANK_STATEMENT"
    CONTRACT = "CONTRACT"
    TAX_DOCUMENT = "TAX_DOCUMENT"
    PAYSLIP = "PAYSLIP"
    OTHER = "OTHER"


class ExtractionStatus(StrEnum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    EXTRACTED = "EXTRACTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class NotificationKind(StrEnum):
    INVOICE_OVERDUE = "INVOICE_OVERDUE"
    BILL_DUE = "BILL_DUE"
    RECONCILIATION_EXCEPTION = "RECONCILIATION_EXCEPTION"
    GST_ISSUE = "GST_ISSUE"
    AI_REVIEW_REQUIRED = "AI_REVIEW_REQUIRED"
    ANOMALY = "ANOMALY"
    CLOSE_TASK = "CLOSE_TASK"
    CASH_WARNING = "CASH_WARNING"
    SYSTEM = "SYSTEM"
