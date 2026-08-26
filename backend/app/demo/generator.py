"""Synthetic demo company: NovaTech Solutions Pvt. Ltd.

Generates twelve months of *real* activity — every invoice, bill, receipt and
payment goes through the normal services and the normal accounting engine, so
the demo books are genuinely balanced rather than fabricated numbers written
into tables.

It deliberately seeds realistic problems for the AI to find: a duplicate bill,
a duplicate payment, an expense spike, a vendor bank-detail change before a
payment, unreconciled bank lines and slow-paying customers.

Determinism: a fixed random seed means the same demo every time, which makes
the demo testable.
"""
from __future__ import annotations

import random
from calendar import monthrange
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.engine import AccountingEngine, EntryInput, LineInput
from app.accounting.periods import ensure_fiscal_year
from app.enums import (
    BankAccountType,
    BankTxnDirection,
    DocumentStatus,
    EmployeeStatus,
    PaymentMethod,
    ReconStatus,
    StockMoveType,
    UserRole,
    VoucherType,
)
from app.models import (
    BankAccount,
    Company,
    Customer,
    Employee,
    Item,
    RecurringTransaction,
    User,
    Vendor,
    Warehouse,
)
from app.money import ZERO, D, money, quantity as to_qty
from app.security import hash_password
from app.services import banking as banking_service
from app.services import purchases as purchase_service
from app.services import sales as sales_service
from app.services.company import add_member, create_company
from app.services.sales import LineDraft

SEED = 20250401


def _attempt(db: Session, action, *args, **kwargs):
    """Run one document creation inside a SAVEPOINT.

    Demo generation creates thousands of documents. A single rejected one — a
    duplicate guard firing, stock running out mid-month — must not poison the
    session and abort the whole build. The nested transaction rolls back just
    that document and leaves everything already created intact.

    Returns the created object, or ``None`` if it was rejected.
    """
    nested = db.begin_nested()
    try:
        result = action(*args, **kwargs)
        nested.commit()
        return result
    except Exception:  # noqa: BLE001 - rejected documents are expected here
        nested.rollback()
        return None

DEMO_COMPANY_NAME = "NovaTech Solutions Pvt. Ltd."

CUSTOMER_NAMES = [
    "Aurora Retail Pvt Ltd", "Bluewave Technologies", "Cascade Logistics", "Dhruva Enterprises",
    "Everest Consulting", "Falcon Media Group", "Ganges Textiles", "Horizon Healthcare",
    "Indus Manufacturing", "Jupiter Software", "Kaveri Agro Foods", "Lotus Hospitality",
    "Meridian Finance", "Nimbus Cloud Services", "Orion Electricals", "Pinnacle Realty",
    "Quantum Analytics", "Ravi Traders", "Sapphire Jewellers", "Tamarind Foods",
    "Umang Pharma", "Vertex Engineering", "Westwind Exports", "Xenon Chemicals",
    "Yamuna Infrastructure", "Zenith Motors", "Anand Distributors", "Bharat Steel Works",
    "Chandra Publications", "Deccan Ceramics", "Ekta Garments", "Fortune Packaging",
    "Gagan Airlines Services", "Hindustan Tools", "Ideal Furniture", "Jyoti Lighting",
    "Konark Solar", "Laxmi Groceries", "Mahesh Automobiles", "Narmada Cements",
    "Ocean Freight India", "Prakash Instruments", "Rajdhani Foods", "Sagar Marine",
    "Trishul Security", "Uttam Plastics", "Vaibhav Interiors", "Wisdom Academy",
    "Yash Constructions", "Zephyr Fabrics",
]

# A curated list gives the demo recognisable names; a combinatorial tail gives it
# realistic scale. First words stay unique across both so the derived contact
# emails never collide.
_MORE_CUSTOMER_FIRST_WORDS = [
    "Abhinav", "Bodhi", "Chinar", "Dwarka", "Eklavya", "Firoza", "Girnar", "Harsha",
    "Ishaan", "Jaisal", "Kanchan", "Lalita", "Mandara", "Neelkanth", "Oorja", "Palash",
    "Rohini", "Shivalik", "Tungabhadra", "Ujjain", "Vindhya", "Warangal", "Aravalli",
    "Bhagirathi", "Chenab", "Damodar", "Erode", "Fatehpur", "Godavari", "Hampi",
    "Ithihas", "Jhelum", "Kalinga", "Lohit", "Mahanadi", "Nilgiri", "Ooty", "Periyar",
    "Rushikulya", "Sabarmati", "Teesta", "Udaipur", "Vaigai", "Wayanad", "Amaravati",
    "Bandipur", "Coorg", "Dandeli", "Eravikulam", "Gokarna", "Hosur", "Idukki",
    "Jalgaon", "Kollam", "Lonavala", "Munnar", "Nashik", "Ooteri", "Panvel", "Ratnagiri",
    "Satara", "Thrissur",
]
_MORE_CUSTOMER_SUFFIXES = [
    "Trading Co", "Industries Ltd", "Systems Pvt Ltd", "Enterprises", "Retail LLP",
    "Solutions Pvt Ltd", "Traders", "Exports Pvt Ltd", "Logistics Ltd", "Services LLP",
    "Agro Ltd", "Technologies", "Distributors", "Ventures Pvt Ltd",
]
CUSTOMER_NAMES += [
    f"{word} {_MORE_CUSTOMER_SUFFIXES[index % len(_MORE_CUSTOMER_SUFFIXES)]}"
    for index, word in enumerate(_MORE_CUSTOMER_FIRST_WORDS)
]

VENDOR_SPECS = [
    ("CloudScale Infrastructure", "6320", "Cloud Hosting", True, "194J"),
    ("DevTools Software Ltd", "6310", "Software Subscriptions", True, "194J"),
    ("Skyline Office Rentals", "6210", "Rent", True, "194I"),
    ("PowerGrid Utilities", "6220", "Electricity and Utilities", False, ""),
    ("ConnectNet Broadband", "6230", "Telephone and Internet", False, ""),
    ("Sharma & Associates CA", "6520", "Audit Fees", True, "194J"),
    ("Legal Partners LLP", "6530", "Legal Expenses", True, "194J"),
    ("BrightAds Marketing", "6410", "Advertising and Marketing", True, "194C"),
    ("SwiftCourier Logistics", "6450", "Freight Outward", True, "194C"),
    ("SecureGuard Services", "6270", "Security and Housekeeping", True, "194C"),
    ("OfficeMart Supplies", "6240", "Office Supplies", False, ""),
    ("TravelDesk India", "6420", "Travel and Conveyance", False, ""),
    ("Reliance Insurance Brokers", "6260", "Insurance", False, ""),
    ("FixIt Maintenance", "6250", "Repairs and Maintenance", True, "194C"),
    ("TalentFirst Recruiters", "6150", "Recruitment Expenses", True, "194J"),
    ("Component Traders Pvt Ltd", "", "", False, ""),
    ("Precision Hardware Supply", "", "", False, ""),
    ("Global Chip Distributors", "", "", False, ""),
]

# The fifteen expense vendors above are billed every month and drive the expense
# analysis, so that list stays curated and fixed. The tail below is supply-side:
# these vendors have no default expense account, so they participate in stock
# purchases rather than adding a recurring monthly bill each.
_MORE_VENDOR_FIRST_WORDS = [
    "Adarsh", "Bhavani", "Chetak", "Dhanush", "Eagle", "Fusion", "Ganpati", "Hexa",
    "Ingot", "Jaya", "Kirti", "Lumina", "Matrix", "Nucleus", "Omkar", "Pravin",
    "Quartz", "Rudra", "Silicon", "Tejas", "Ultra", "Vajra", "Wavelength", "Xylo",
    "Yuvraj", "Zircon", "Alpine", "Brahma", "Cygnus", "Delta", "Elektra", "Ferrum",
    "Gravitas", "Helix", "Ionix", "Jetstream", "Kinetic", "Lattice", "Momentum",
    "Nimble", "Optima", "Prism", "Quorum", "Radiant", "Spectra", "Tensor", "Univolt",
    "Vector", "Wattson", "Xenith", "Ytterb", "Zenon", "Ashwin", "Bharati", "Chandan",
    "Dinkar", "Eshwar", "Gaurav", "Hemant", "Ishwar", "Jagdish", "Kamlesh", "Lokesh",
    "Manohar", "Nandan", "Omprakash", "Pankaj", "Rakesh", "Sudhir", "Tarun", "Umesh",
    "Vasant", "Yogesh", "Anupam", "Bipin", "Chirag", "Devendra", "Eknath", "Falguni",
    "Gopal", "Hiralal", "Indrajit", "Jitendra", "Kailash", "Lalit", "Mukesh", "Nitin",
    "Pramod", "Raghav",
]
_MORE_VENDOR_SUFFIXES = [
    "Components Pvt Ltd", "Electronics Ltd", "Hardware Supply", "Peripherals Co",
    "Semiconductors", "Assemblies Pvt Ltd", "Circuits Ltd", "Devices LLP",
    "Instruments Pvt Ltd", "Modules Ltd", "Technologies", "Supply Chain Pvt Ltd",
]
VENDOR_SPECS += [
    (
        f"{word} {_MORE_VENDOR_SUFFIXES[index % len(_MORE_VENDOR_SUFFIXES)]}",
        "", "", False, "",
    )
    for index, word in enumerate(_MORE_VENDOR_FIRST_WORDS)
]

PRODUCT_SPECS = [
    # (sku, name, hsn, gst, purchase, selling, reorder, is_service)
    ("LAP-PRO-15", "ProBook 15 Laptop", "8471", 18, 52000, 68000, 8, False),
    ("LAP-AIR-13", "AirBook 13 Ultrabook", "8471", 18, 61000, 79000, 6, False),
    ("DSK-WS-01", "Workstation Desktop", "8471", 18, 74000, 95000, 4, False),
    ("MON-27-4K", '27" 4K Monitor', "8528", 18, 24000, 32000, 10, False),
    ("MON-24-FHD", '24" FHD Monitor', "8528", 18, 11000, 15500, 15, False),
    ("KBD-MECH", "Mechanical Keyboard", "8471", 18, 4200, 6500, 25, False),
    ("MOU-WL-01", "Wireless Mouse", "8471", 18, 1200, 2100, 40, False),
    ("DOCK-USB-C", "USB-C Docking Station", "8471", 18, 8500, 12500, 12, False),
    ("SSD-1TB", "1TB NVMe SSD", "8523", 18, 6800, 9500, 20, False),
    ("RAM-16GB", "16GB DDR5 Module", "8473", 18, 4500, 6800, 30, False),
    ("NAS-4BAY", "4-Bay NAS Server", "8471", 18, 42000, 56000, 3, False),
    ("SWT-24P", "24-Port Gigabit Switch", "8517", 18, 14000, 19500, 8, False),
    ("RTR-ENT", "Enterprise Router", "8517", 18, 28000, 38000, 5, False),
    ("UPS-2KVA", "2KVA Online UPS", "8504", 18, 18000, 24500, 6, False),
    ("PRN-LSR", "Laser Printer", "8443", 18, 16500, 22000, 5, False),
    ("SCN-DOC", "Document Scanner", "8471", 18, 21000, 28500, 4, False),
    ("CAM-CONF", "Conference Camera", "8525", 18, 32000, 43000, 4, False),
    ("HDS-NC", "Noise Cancelling Headset", "8518", 18, 7500, 11000, 15, False),
    ("CBL-HDMI", "HDMI 2.1 Cable", "8544", 18, 450, 850, 100, False),
    ("CBL-CAT6", "Cat6 Network Cable (305m)", "8544", 18, 6200, 8900, 10, False),
    ("SVC-AMC-BASIC", "Annual Maintenance — Basic", "9987", 18, 0, 24000, 0, True),
    ("SVC-AMC-PRO", "Annual Maintenance — Pro", "9987", 18, 0, 60000, 0, True),
    ("SVC-INSTALL", "Installation and Setup", "9987", 18, 0, 8500, 0, True),
    ("SVC-CONSULT", "IT Consulting (per day)", "9983", 18, 0, 15000, 0, True),
    ("SVC-TRAIN", "Staff Training (per session)", "9992", 18, 0, 22000, 0, True),
    ("SVC-CLOUD-MIG", "Cloud Migration Service", "9983", 18, 0, 145000, 0, True),
    ("SVC-AUDIT-SEC", "Security Audit", "9983", 18, 0, 85000, 0, True),
    ("SVC-SUPPORT", "Priority Support (monthly)", "9987", 18, 0, 12000, 0, True),
    ("LIC-OS-PRO", "OS Pro Licence", "8523", 18, 9500, 13500, 0, False),
    ("LIC-OFFICE", "Office Suite Licence", "8523", 18, 6800, 9800, 0, False),
    ("LIC-AV-ENT", "Antivirus Enterprise Licence", "8523", 18, 1800, 2900, 0, False),
    ("LIC-DB-STD", "Database Standard Licence", "8523", 18, 45000, 62000, 0, False),
    ("ACC-STAND", "Laptop Stand", "9403", 18, 1400, 2400, 30, False),
    ("ACC-BAG", "Laptop Backpack", "4202", 18, 1800, 3200, 25, False),
    ("ACC-HUB", "USB 3.0 Hub", "8471", 18, 900, 1650, 40, False),
    ("PWR-STRIP", "Surge Protected Power Strip", "8536", 18, 750, 1400, 50, False),
    ("TAB-10", '10" Business Tablet', "8471", 18, 23000, 31000, 8, False),
    ("PHN-DESK", "IP Desk Phone", "8517", 18, 5400, 8200, 12, False),
    ("PROJ-4K", "4K Conference Projector", "8528", 18, 68000, 89000, 2, False),
    ("WHB-SMART", "Smart Whiteboard", "8471", 18, 95000, 128000, 2, False),
    ("SRV-RACK", "Server Rack 42U", "8473", 18, 34000, 46000, 3, False),
    ("FW-APPLIANCE", "Firewall Appliance", "8517", 18, 52000, 71000, 3, False),
    ("AP-WIFI6", "WiFi 6 Access Point", "8517", 18, 9800, 14200, 12, False),
    ("BAT-UPS", "UPS Replacement Battery", "8507", 28, 4200, 6200, 10, False),
    ("TNR-LSR", "Laser Toner Cartridge", "3215", 18, 3400, 5100, 20, False),
    ("PPR-A4", "A4 Paper (box of 5 reams)", "4802", 12, 1250, 1950, 40, False),
    ("SW-ERP-USER", "ERP Named User Licence", "8523", 18, 18000, 26000, 0, False),
    ("SW-CRM-USER", "CRM Named User Licence", "8523", 18, 12000, 17500, 0, False),
    ("HW-BIOMETRIC", "Biometric Attendance Device", "8471", 18, 8900, 13500, 6, False),
    ("HW-CCTV-KIT", "CCTV 8-Camera Kit", "8525", 18, 42000, 58000, 3, False),
]

EMPLOYEE_NAMES = [
    ("Aarav Sharma", "Engineering", "Senior Engineer", 1800000),
    ("Diya Patel", "Engineering", "Engineering Manager", 2400000),
    ("Vihaan Reddy", "Engineering", "Software Engineer", 1200000),
    ("Ananya Iyer", "Sales", "Sales Director", 2200000),
    ("Arjun Nair", "Sales", "Account Executive", 1100000),
    ("Ishaan Gupta", "Operations", "Operations Lead", 1500000),
    ("Saanvi Joshi", "Finance", "Finance Manager", 1900000),
    ("Kabir Menon", "Finance", "Accountant", 900000),
    ("Myra Desai", "Marketing", "Marketing Manager", 1400000),
    ("Reyansh Kulkarni", "Support", "Support Engineer", 750000),
    ("Aadhya Rao", "Support", "Support Lead", 1050000),
    ("Vivaan Chopra", "Engineering", "DevOps Engineer", 1650000),
]

STATE_POOL = [
    ("Maharashtra", "27"), ("Karnataka", "29"), ("Delhi", "07"), ("Tamil Nadu", "33"),
    ("Gujarat", "24"), ("Telangana", "36"), ("West Bengal", "19"), ("Uttar Pradesh", "09"),
    ("Rajasthan", "08"), ("Kerala", "32"), ("Haryana", "06"), ("Punjab", "03"),
]

CITY_BY_STATE = {
    "27": "Mumbai", "29": "Bengaluru", "07": "New Delhi", "33": "Chennai",
    "24": "Ahmedabad", "36": "Hyderabad", "19": "Kolkata", "09": "Lucknow",
    "08": "Jaipur", "32": "Kochi", "06": "Gurugram", "03": "Ludhiana",
}


@dataclass
class DemoStats:
    company_id: str = ""
    company_name: str = ""
    customers: int = 0
    vendors: int = 0
    items: int = 0
    invoices: int = 0
    bills: int = 0
    receipts: int = 0
    payments: int = 0
    journal_entries: int = 0
    bank_transactions: int = 0
    employees: int = 0
    warehouses: int = 0
    bank_accounts: int = 0
    seeded_issues: list[str] = None  # type: ignore[assignment]
    login_email: str = ""
    login_password: str = ""

    def as_dict(self) -> dict:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "customers": self.customers,
            "vendors": self.vendors,
            "items": self.items,
            "invoices": self.invoices,
            "bills": self.bills,
            "receipts": self.receipts,
            "payments": self.payments,
            "journal_entries": self.journal_entries,
            "bank_transactions": self.bank_transactions,
            "employees": self.employees,
            "warehouses": self.warehouses,
            "bank_accounts": self.bank_accounts,
            "seeded_issues": self.seeded_issues or [],
            "login_email": self.login_email,
            "login_password": self.login_password,
        }


def _gstin_for(state_code: str, index: int) -> str:
    """Build a checksum-valid GSTIN so validation passes on demo data."""
    from app.services.tax import validate_gstin

    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pan = (
        f"{letters[index % 26]}{letters[(index // 26) % 26]}{letters[(index // 7) % 26]}"
        f"PA{letters[(index // 3) % 26]}"
        f"{(1000 + index * 7) % 10000:04d}"
        f"{letters[(index // 5) % 26]}"
    )
    base = f"{state_code}{pan}1Z"

    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for position, char in enumerate(base):
        factor = 2 if position % 2 else 1
        product = alphabet.index(char) * factor
        total += product // 36 + product % 36
    check = alphabet[(36 - total % 36) % 36]
    gstin = base + check

    valid, _ = validate_gstin(gstin)
    return gstin if valid else ""


def generate_demo_company(
    db: Session,
    *,
    owner_user_id: str | None = None,
    months: int = 12,
    create_login: bool = True,
) -> DemoStats:
    """Build the full demo dataset. Everything posts through the real engine."""
    rng = random.Random(SEED)
    stats = DemoStats(seeded_issues=[])

    today = date.today()
    # Books start on the fiscal-year boundary at least `months` back.
    start = (today.replace(day=1) - timedelta(days=months * 30)).replace(day=1)

    company = create_company(
        db,
        name="NovaTech Solutions",
        legal_name=DEMO_COMPANY_NAME,
        business_type="PRIVATE_LIMITED",
        state="Maharashtra",
        state_code="27",
        gstin=_gstin_for("27", 1),
        pan="AAAPA1007A",
        tan="MUMN12345B",
        address_line1="Unit 402, Ambit IT Park, Andheri East",
        city="Mumbai",
        pincode="400093",
        email="accounts@novatech.example",
        phone="+912266001234",
        books_begin_on=start,
        is_demo=True,
        owner_user_id=owner_user_id,
    )
    stats.company_id = company.id
    stats.company_name = company.legal_name

    # Fiscal years across the whole window.
    cursor = start
    while cursor <= today:
        ensure_fiscal_year(db, company.id, cursor)
        cursor = _add_months(cursor, 6)
    ensure_fiscal_year(db, company.id, today)

    if create_login and owner_user_id is None:
        user = _create_demo_user(db, company.id)
        stats.login_email = user.email
        stats.login_password = "demo1234"

    warehouses = _create_warehouses(db, company)
    stats.warehouses = len(warehouses)

    bank_accounts = _create_bank_accounts(db, company)
    stats.bank_accounts = len(bank_accounts)

    _seed_opening_capital(db, company, start)

    customers = _create_customers(db, company, rng, start)
    stats.customers = len(customers)

    vendors = _create_vendors(db, company, rng, start)
    stats.vendors = len(vendors)

    items = _create_items(db, company, warehouses[0], rng, start)
    stats.items = len(items)

    employees = _create_employees(db, company, start)
    stats.employees = len(employees)

    _create_recurring(db, company, today)

    counts = _generate_transactions(
        db,
        company,
        customers=customers,
        vendors=vendors,
        items=items,
        warehouses=warehouses,
        bank_accounts=bank_accounts,
        start=start,
        end=today,
        rng=rng,
        stats=stats,
    )
    stats.invoices = counts["invoices"]
    stats.bills = counts["bills"]
    stats.receipts = counts["receipts"]
    stats.payments = counts["payments"]

    stats.bank_transactions = _generate_bank_feed(
        db, company, bank_accounts[0], start, today, rng, stats
    )

    from sqlalchemy import func

    from app.models import JournalEntry

    stats.journal_entries = int(
        db.execute(
            select(func.count(JournalEntry.id)).where(JournalEntry.company_id == company.id)
        ).scalar_one()
    )
    return stats


# ------------------------------------------------------------ components --

def _create_demo_user(db: Session, company_id: str) -> User:
    from sqlalchemy import func

    email = "demo@ailedger.local"
    user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            full_name="Demo Owner",
            password_hash=hash_password("demo1234"),
        )
        db.add(user)
        db.flush()
    add_member(db, company_id, user.id, UserRole.OWNER, is_default=True)
    return user


def _create_warehouses(db: Session, company: Company) -> list[Warehouse]:
    existing = list(
        db.execute(select(Warehouse).where(Warehouse.company_id == company.id)).scalars()
    )
    specs = [
        ("MUM", "Mumbai Warehouse", "Maharashtra", "27", "Mumbai"),
        ("BLR", "Bengaluru Warehouse", "Karnataka", "29", "Bengaluru"),
        ("DEL", "Delhi Warehouse", "Delhi", "07", "New Delhi"),
    ]
    by_code = {warehouse.code: warehouse for warehouse in existing}

    # The provisioning step creates MAIN; rename it to the Mumbai hub.
    if "MAIN" in by_code:
        main = by_code["MAIN"]
        main.code, main.name = "MUM", "Mumbai Warehouse"
        main.state, main.state_code, main.city = "Maharashtra", "27", "Mumbai"
        by_code["MUM"] = main

    for code, name, state, state_code, city in specs:
        if code in by_code:
            continue
        warehouse = Warehouse(
            company_id=company.id,
            code=code,
            name=name,
            state=state,
            state_code=state_code,
            city=city,
            is_default=(code == "MUM"),
        )
        db.add(warehouse)
        by_code[code] = warehouse

    db.flush()
    return [by_code[code] for code, *_ in specs]


def _create_bank_accounts(db: Session, company: Company) -> list[BankAccount]:
    specs = [
        ("Primary Current Account", "HDFC Bank", "50200012345678", "HDFC0001234", BankAccountType.CURRENT),
        ("Operations Account", "ICICI Bank", "002105501234", "ICIC0000021", BankAccountType.CURRENT),
        ("Payroll Account", "Axis Bank", "918020012345678", "UTIB0001234", BankAccountType.CURRENT),
        ("Savings Reserve", "State Bank of India", "38291746501", "SBIN0004567", BankAccountType.SAVINGS),
        ("Petty Cash Wallet", "Paytm Payments Bank", "9198765432101", "PYTM0123456", BankAccountType.WALLET),
    ]
    accounts = []
    for name, bank, number, ifsc, kind in specs:
        account = banking_service.create_bank_account(
            db,
            company.id,
            name=name,
            bank_name=bank,
            account_number=number,
            ifsc=ifsc,
            account_type=kind,
        )
        accounts.append(account)
    return accounts


def _seed_opening_capital(db: Session, company: Company, start: date) -> None:
    """Fund the company: share capital into the primary bank account."""
    engine = AccountingEngine(db, company.id)
    bank_account = db.execute(
        select(BankAccount).where(BankAccount.company_id == company.id).limit(1)
    ).scalar_one()

    engine.create_and_post(
        EntryInput(
            entry_date=start,
            voucher_type=VoucherType.OPENING,
            narration="Opening share capital",
            is_opening=True,
            lines=[
                LineInput(
                    account_id=bank_account.gl_account_id,
                    debit=money("8000000"),
                    description="Share capital received",
                    bank_account_id=bank_account.id,
                ),
                LineInput(
                    account_id=engine.account_by_system_key("SHARE_CAPITAL").id,
                    credit=money("8000000"),
                    description="Issued share capital",
                ),
            ],
        )
    )

    # A modest fixed-asset base so depreciation and the balance sheet look real.
    engine.create_and_post(
        EntryInput(
            entry_date=start,
            voucher_type=VoucherType.OPENING,
            narration="Opening fixed assets",
            is_opening=True,
            lines=[
                LineInput(account_code="1220", debit=money("1850000"), description="Computers and IT equipment"),
                LineInput(account_code="1230", debit=money("620000"), description="Furniture and fixtures"),
                LineInput(account_code="1250", debit=money("310000"), description="Office equipment"),
                LineInput(
                    account_id=engine.account_by_system_key("BANK_DEFAULT").id,
                    credit=money("2780000"),
                    description="Paid for fixed assets",
                    bank_account_id=bank_account.id,
                ),
            ],
        )
    )


def _onboarded_at(
    start: date | None, index: int, total: int, rng: random.Random
) -> datetime:
    """Spread counterparty onboarding across the trading period.

    The first sixth of the book predates the opening balance — those are the
    relationships the company already had — and the rest arrive steadily.
    """
    if start is None:
        return datetime.now(timezone.utc)
    established = max(1, total // 6)
    if index < established:
        day = start - timedelta(days=rng.randint(200, 900))
    else:
        span = max(1, total - established)
        offset = int((index - established) / span * 330)
        day = start + timedelta(days=offset + rng.randint(0, 10))
    return datetime.combine(day, time(hour=rng.randint(9, 18), minute=rng.randint(0, 59)), tzinfo=timezone.utc)


def _create_customers(
    db: Session, company: Company, rng: random.Random, start: date | None = None
) -> list[Customer]:
    """Create the customer book, onboarded gradually rather than all at once.

    ``created_at`` is real data here, not bookkeeping: the anomaly detectors use
    it to decide what counts as a *new* counterparty. Stamping the whole book
    with the generation timestamp would make every customer look brand new and
    flood the exception centre on day one.
    """
    customers = []
    for index, name in enumerate(CUSTOMER_NAMES):
        state, state_code = STATE_POOL[index % len(STATE_POOL)]
        registered = index % 7 != 0  # roughly 1 in 7 is unregistered
        customer = Customer(
            company_id=company.id,
            code=f"CUST{index + 1:04d}",
            name=name,
            legal_name=name,
            email=f"accounts@{name.split()[0].lower()}.example",
            phone=f"+9198{rng.randint(10000000, 99999999)}",
            gstin=_gstin_for(state_code, index + 10) if registered else "",
            gst_treatment="REGISTERED" if registered else "UNREGISTERED",
            billing_address=f"{rng.randint(1, 400)}, {name.split()[0]} Complex",
            city=CITY_BY_STATE[state_code],
            state=state,
            state_code=state_code,
            pincode=f"{rng.randint(110000, 799999)}",
            payment_terms_days=rng.choice([15, 30, 30, 30, 45, 60]),
            credit_limit=money(rng.choice([500000, 1000000, 2000000, 5000000])),
        )
        customer.created_at = _onboarded_at(start, index, len(CUSTOMER_NAMES), rng)
        customer.updated_at = customer.created_at
        db.add(customer)
        customers.append(customer)
    db.flush()
    return customers


def _create_vendors(
    db: Session, company: Company, rng: random.Random, start: date | None = None
) -> list[Vendor]:
    from app.models import Account

    vendors = []
    for index, (name, account_code, _label, tds, section) in enumerate(VENDOR_SPECS):
        state, state_code = STATE_POOL[index % len(STATE_POOL)]
        expense_account = (
            db.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == account_code
                )
            ).scalar_one_or_none()
            if account_code
            else None
        )
        vendor = Vendor(
            company_id=company.id,
            code=f"VEND{index + 1:04d}",
            name=name,
            legal_name=name,
            email=f"billing@{name.split()[0].lower()}.example",
            phone=f"+9180{rng.randint(10000000, 99999999)}",
            gstin=_gstin_for(state_code, index + 60),
            pan=f"AAA{'ABCDEFGHIJ'[index % 10]}A{2000 + index:04d}{'ABCDEFGHIJ'[index % 10]}",
            gst_treatment="REGISTERED",
            address=f"{rng.randint(1, 200)}, {name.split()[0]} House",
            city=CITY_BY_STATE[state_code],
            state=state,
            state_code=state_code,
            payment_terms_days=rng.choice([15, 30, 30, 45]),
            tds_applicable=tds,
            default_tds_section=section,
            default_expense_account_id=expense_account.id if expense_account else None,
            is_msme=index % 5 == 0,
            bank_account_name=name,
            bank_account_number=f"{rng.randint(10000000000, 99999999999)}",
            bank_ifsc=f"HDFC000{rng.randint(1000, 9999)}",
        )
        vendor.created_at = _onboarded_at(start, index, len(VENDOR_SPECS), rng)
        vendor.updated_at = vendor.created_at
        db.add(vendor)
        vendors.append(vendor)
    db.flush()
    return vendors


def _create_items(
    db: Session, company: Company, warehouse: Warehouse, rng: random.Random, start: date
) -> list[Item]:
    from app.services.inventory import record_move

    engine = AccountingEngine(db, company.id)
    items = []
    opening_value = ZERO

    for sku, name, hsn, gst, purchase, selling, reorder, is_service in PRODUCT_SPECS:
        item = Item(
            company_id=company.id,
            sku=sku,
            name=name,
            item_type="SERVICE" if is_service else "GOODS",
            uom="LIC" if sku.startswith("LIC") or sku.startswith("SW-") else ("HRS" if is_service else "NOS"),
            hsn_sac=hsn,
            gst_rate=D(gst),
            purchase_price=money(purchase),
            selling_price=money(selling),
            reorder_level=to_qty(reorder),
            reorder_quantity=to_qty(reorder * 3),
            track_inventory=not is_service,
        )
        db.add(item)
        db.flush()
        items.append(item)

        if not is_service and purchase > 0:
            quantity = to_qty(rng.randint(max(4, reorder), max(12, reorder * 4)))
            record_move(
                db,
                company.id,
                item_id=item.id,
                warehouse_id=warehouse.id,
                move_date=start,
                move_type=StockMoveType.OPENING,
                quantity=quantity,
                rate=money(purchase),
                source_type="OpeningStock",
                source_id=item.id,
                narration="Opening stock",
            )
            item.opening_qty = quantity
            item.opening_rate = money(purchase)
            opening_value = money(opening_value + money(quantity * money(purchase)))

    if opening_value > ZERO:
        engine.create_and_post(
            EntryInput(
                entry_date=start,
                voucher_type=VoucherType.OPENING,
                narration="Opening stock",
                is_opening=True,
                lines=[
                    LineInput(
                        account_id=engine.account_by_system_key("INVENTORY").id,
                        debit=opening_value,
                        description="Opening stock",
                    ),
                    LineInput(
                        account_id=engine.account_by_system_key("OPENING_BALANCE_EQUITY").id,
                        credit=opening_value,
                        description="Opening stock",
                    ),
                ],
            )
        )

    db.flush()
    return items


def _create_employees(db: Session, company: Company, start: date) -> list[Employee]:
    employees = []
    for index, (name, department, designation, ctc) in enumerate(EMPLOYEE_NAMES):
        employee = Employee(
            company_id=company.id,
            employee_code=f"EMP{index + 1:04d}",
            full_name=name,
            email=f"{name.split()[0].lower()}@novatech.example",
            department=department,
            designation=designation,
            joining_date=start - timedelta(days=200 + index * 40),
            status=EmployeeStatus.ACTIVE,
            annual_ctc=money(ctc),
            monthly_gross=money(D(ctc) / 12),
            pan=f"BBB{'ABCDEFGHIJ'[index % 10]}B{3000 + index:04d}{'ABCDEFGHIJ'[index % 10]}",
            bank_account_name=name,
            bank_account_number=f"{50100000000 + index * 137}",
            bank_ifsc="HDFC0001234",
        )
        db.add(employee)
        employees.append(employee)
    db.flush()
    return employees


def _create_recurring(db: Session, company: Company, today: date) -> None:
    templates = [
        ("Office rent — Andheri", 285000, "MONTHLY"),
        ("Cloud hosting — production", 168000, "MONTHLY"),
        ("Software subscriptions", 94000, "MONTHLY"),
        ("Internet and connectivity", 32000, "MONTHLY"),
        ("Insurance premium", 145000, "QUARTERLY"),
    ]
    for name, amount, frequency in templates:
        db.add(
            RecurringTransaction(
                company_id=company.id,
                name=name,
                frequency=frequency,
                start_date=today.replace(day=1),
                next_run_date=_add_months(today.replace(day=1), 1),
                amount=money(amount),
                day_of_month=5,
                auto_post=False,
            )
        )
    db.flush()


def _generate_transactions(
    db: Session,
    company: Company,
    *,
    customers: list[Customer],
    vendors: list[Vendor],
    items: list[Item],
    warehouses: list[Warehouse],
    bank_accounts: list[BankAccount],
    start: date,
    end: date,
    rng: random.Random,
    stats: DemoStats,
) -> dict:
    """Twelve months of sales and purchases, all posted through the engine."""
    counts = {"invoices": 0, "bills": 0, "receipts": 0, "payments": 0}
    stock_items = [item for item in items if item.track_inventory]
    service_items = [item for item in items if not item.track_inventory]
    expense_vendors = [vendor for vendor in vendors if vendor.default_expense_account_id]
    stock_vendors = [vendor for vendor in vendors if not vendor.default_expense_account_id]

    primary_bank = bank_accounts[0]
    month_cursor = start.replace(day=1)
    month_index = 0

    while month_cursor <= end:
        days_in_month = monthrange(month_cursor.year, month_cursor.month)[1]
        month_end = min(month_cursor.replace(day=days_in_month), end)

        # Growth trend plus seasonality, so the trend chart tells a story.
        growth = 1 + (month_index * 0.045)
        seasonal = 1.25 if month_cursor.month in (3, 10, 11) else 1.0
        invoice_target = int(rng.randint(34, 46) * growth * seasonal / 1.2)

        # ---- Purchases: restock first so there is stock to sell -------------
        for _ in range(rng.randint(11, 15)):
            vendor = rng.choice(stock_vendors)
            day = rng.randint(1, min(20, days_in_month))
            doc_date = month_cursor.replace(day=day)
            if doc_date > end:
                continue

            # More bills, each smaller. Restocking has to track what actually
            # sells: raising the bill count without lowering the quantity per
            # bill buries the company in stock and overdraws its bank accounts.
            lines = []
            for item in rng.sample(stock_items, k=rng.randint(1, 3)):
                lines.append(
                    LineDraft(
                        item_id=item.id,
                        description=item.name,
                        hsn_sac=item.hsn_sac,
                        uom=item.uom,
                        quantity=D(rng.randint(3, 18)),
                        rate=money(D(item.purchase_price) * D(rng.uniform(0.96, 1.05))),
                        warehouse_id=warehouses[0].id,
                    )
                )
            created = _attempt(
                db,
                purchase_service.create_bill,
                db,
                company.id,
                vendor_id=vendor.id,
                doc_date=doc_date,
                vendor_invoice_number=f"{vendor.code[-4:]}-{doc_date.strftime('%y%m')}-{rng.randint(1000, 9999)}",
                lines=lines,
                post=True,
            )
            if created is not None:
                counts["bills"] += 1

        # ---- Operating expenses --------------------------------------------
        for vendor in expense_vendors:
            if rng.random() > 0.82:
                continue
            day = rng.randint(1, min(26, days_in_month))
            doc_date = month_cursor.replace(day=day)
            if doc_date > end:
                continue

            base = {
                "6320": 168000, "6310": 94000, "6210": 285000, "6220": 46000,
                "6230": 32000, "6520": 60000, "6530": 45000, "6410": 120000,
                "6450": 38000, "6270": 42000, "6240": 18000, "6420": 65000,
                "6260": 48000, "6250": 28000, "6150": 85000,
            }
            account_code = next(
                (spec[1] for spec in VENDOR_SPECS if spec[0] == vendor.name), "6890"
            )
            amount = base.get(account_code, 25000)

            # Seeded anomaly: a marketing spike in the most recent full month.
            if account_code == "6410" and month_cursor >= _add_months(end.replace(day=1), -1):
                amount = int(amount * 2.4)
                if "Expense spike (marketing)" not in stats.seeded_issues:
                    stats.seeded_issues.append("Expense spike (marketing)")

            created = _attempt(
                db,
                purchase_service.create_bill,
                db,
                company.id,
                vendor_id=vendor.id,
                doc_date=doc_date,
                vendor_invoice_number=f"{vendor.code[-4:]}/{doc_date.strftime('%Y%m')}/{rng.randint(100, 999)}",
                lines=[
                    LineDraft(
                        description=f"{vendor.name} — {doc_date.strftime('%B %Y')}",
                        hsn_sac="9983",
                        quantity=D(1),
                        rate=money(D(amount) * D(rng.uniform(0.92, 1.12))),
                        gst_rate=D(18),
                        account_id=vendor.default_expense_account_id,
                    )
                ],
                post=True,
            )
            if created is not None:
                counts["bills"] += 1

        # ---- Sales -----------------------------------------------------------
        for _ in range(invoice_target):
            customer = rng.choice(customers)
            day = rng.randint(1, days_in_month)
            doc_date = month_cursor.replace(day=day)
            if doc_date > end:
                continue

            lines = []
            for item in rng.sample(items, k=rng.randint(1, 4)):
                if item.track_inventory:
                    available = to_qty(item.current_qty)
                    if available <= 1:
                        continue
                    quantity = D(rng.randint(1, max(1, min(6, int(available)))))
                else:
                    quantity = D(rng.randint(1, 3))
                lines.append(
                    LineDraft(
                        item_id=item.id,
                        description=item.name,
                        hsn_sac=item.hsn_sac,
                        uom=item.uom,
                        quantity=quantity,
                        rate=money(D(item.selling_price) * D(rng.uniform(0.97, 1.06))),
                        discount_percent=D(rng.choice([0, 0, 0, 2, 5])),
                        warehouse_id=warehouses[0].id,
                    )
                )
            if not lines:
                continue

            invoice = _attempt(
                db,
                sales_service.create_invoice,
                db,
                company.id,
                customer_id=customer.id,
                doc_date=doc_date,
                lines=lines,
                post=True,
            )
            if invoice is None:
                continue
            counts["invoices"] += 1

            # ---- Collections: most pay, some late, a few not at all ---------
            roll = rng.random()
            if roll < 0.72:
                delay = rng.randint(3, customer.payment_terms_days + 5)
            elif roll < 0.88:
                delay = rng.randint(customer.payment_terms_days + 10, customer.payment_terms_days + 45)
            else:
                continue  # remains outstanding

            receipt_date = doc_date + timedelta(days=delay)
            if receipt_date > end:
                continue

            receipt = _attempt(
                db,
                sales_service.record_receipt,
                db,
                company.id,
                customer_id=customer.id,
                receipt_date=receipt_date,
                amount=invoice.total,
                bank_account_id=primary_bank.id,
                method=PaymentMethod.NEFT,
                reference=f"UTR{rng.randint(100000000000, 999999999999)}",
                allocations=[{"invoice_id": invoice.id, "amount": invoice.total}],
            )
            if receipt is not None:
                counts["receipts"] += 1

        month_cursor = _add_months(month_cursor, 1)
        month_index += 1

    # ---- Vendor payments: settle most bills -------------------------------
    from app.models import Bill

    open_bills = list(
        db.execute(
            select(Bill).where(
                Bill.company_id == company.id,
                Bill.status.in_([DocumentStatus.APPROVED, DocumentStatus.OVERDUE]),
            )
        ).scalars()
    )
    for bill in open_bills:
        if rng.random() > 0.78:
            continue  # left outstanding on purpose
        payment_date = bill.due_date - timedelta(days=rng.randint(0, 5))
        if payment_date > end or payment_date < bill.doc_date:
            payment_date = min(end, bill.doc_date + timedelta(days=10))
        payment = _attempt(
            db,
            purchase_service.record_payment,
            db,
            company.id,
            vendor_id=bill.vendor_id,
            payment_date=payment_date,
            amount=bill.balance_due,
            bank_account_id=primary_bank.id,
            method=PaymentMethod.NEFT,
            reference=f"PAY{rng.randint(100000000, 999999999)}",
            allocations=[{"bill_id": bill.id, "amount": bill.balance_due}],
            user_id=None,
            released_by_user_id="demo-generator",
        )
        if payment is not None:
            counts["payments"] += 1

    _seed_realistic_issues(db, company, vendors, primary_bank, end, rng, stats, counts)
    return counts


def _seed_realistic_issues(
    db: Session,
    company: Company,
    vendors: list[Vendor],
    bank_account: BankAccount,
    end: date,
    rng: random.Random,
    stats: DemoStats,
    counts: dict,
) -> None:
    """Plant the problems the AI agents are supposed to find."""
    from app.models import Bill

    recent_start = end - timedelta(days=60)

    # 1. Duplicate bill — same vendor, same amount, same reference.
    candidates = list(
        db.execute(
            select(Bill)
            .where(
                Bill.company_id == company.id,
                Bill.doc_date >= recent_start,
                Bill.status != DocumentStatus.CANCELLED,
            )
            .limit(30)
        ).scalars()
    )
    if candidates:
        original = candidates[len(candidates) // 2]
        # The database enforces uniqueness of a vendor's own invoice number, so a
        # byte-identical duplicate cannot exist. The realistic (and far more
        # dangerous) case is the same invoice re-entered with a slightly different
        # reference — a transposed digit, or a re-sent copy. That is exactly what
        # the "same vendor, same amount, close in time" detector is for.
        near_duplicate_ref = original.vendor_invoice_number + "-A"
        created = _attempt(
            db,
            purchase_service.create_bill,
            db,
            company.id,
            vendor_id=original.vendor_id,
            doc_date=original.doc_date + timedelta(days=6),
            vendor_invoice_number=near_duplicate_ref,
            lines=[
                LineDraft(
                    item_id=line.item_id,
                    description=line.description,
                    hsn_sac=line.hsn_sac,
                    uom=line.uom,
                    quantity=D(line.quantity),
                    rate=money(line.rate),
                    discount_percent=D(line.discount_percent),
                    gst_rate=D(line.gst_rate),
                    account_id=line.account_id,
                )
                for line in original.lines
            ],
            post=True,
            allow_duplicate=True,
        )
        if created is not None:
            counts["bills"] += 1
            stats.seeded_issues.append(
                "Near-duplicate bill (same vendor and amount, reference differs by one character)"
            )

    # 2. The same amount paid to the same vendor twice within days.
    #
    # Mirrors an *existing* payment exactly. Using a bill total instead would
    # differ from the original payment whenever TDS was deducted, and the
    # duplicate detector (correctly) would not flag it.
    from app.models import Payment

    original_payment = db.execute(
        select(Payment)
        .where(
            Payment.company_id == company.id,
            Payment.payment_date >= recent_start,
            Payment.status != DocumentStatus.CANCELLED,
        )
        .order_by(Payment.amount.desc())
        .limit(1)
    ).scalar_one_or_none()

    if original_payment is not None:
        repeat = _attempt(
            db,
            purchase_service.record_payment,
            db,
            company.id,
            vendor_id=original_payment.vendor_id,
            payment_date=min(end, original_payment.payment_date + timedelta(days=4)),
            amount=original_payment.amount,
            bank_account_id=bank_account.id,
            method=PaymentMethod.NEFT,
            reference=f"PAY{rng.randint(100000000, 999999999)}",
            auto_allocate=False,
            user_id=None,
            released_by_user_id="demo-generator",
        )
        if repeat is not None:
            counts["payments"] += 1
            stats.seeded_issues.append(
                "Duplicate payment — same vendor and amount four days apart"
            )

    # 3. Vendor bank details changed shortly before a payment went out.
    #
    # Ordering matters: the live payment-hold policy *blocks* a payment within
    # the hold window, so the payment is recorded first and the change is then
    # backdated to just before it. That reproduces the real-world situation the
    # fraud monitor exists to catch — a change that slipped through before the
    # control was in place — without disabling the control itself.
    vendor = vendors[3]
    payment = _attempt(
        db,
        purchase_service.record_payment,
        db,
        company.id,
        vendor_id=vendor.id,
        payment_date=end - timedelta(days=1),
        amount=money(185000),
        bank_account_id=bank_account.id,
        method=PaymentMethod.RTGS,
        reference=f"RTGS{rng.randint(100000000, 999999999)}",
        auto_allocate=False,
        user_id=None,
        released_by_user_id="demo-generator",
    )
    if payment is not None:
        counts["payments"] += 1
        vendor.bank_account_number = f"{rng.randint(10000000000, 99999999999)}"
        vendor.bank_ifsc = f"ICIC000{rng.randint(1000, 9999)}"
        vendor.bank_details_updated_at = end - timedelta(days=2)
        db.flush()
        stats.seeded_issues.append(
            "Payment released two days after a vendor bank-detail change"
        )

    # 4. Round-number manual journals (an estimate posted as an actual).
    engine = AccountingEngine(db, company.id)
    for offset, amount in ((10, 250000), (18, 150000), (25, 300000)):
        entry_date = end - timedelta(days=offset)
        _attempt(
            db,
            engine.create_and_post,
            EntryInput(
                entry_date=entry_date,
                voucher_type=VoucherType.JOURNAL,
                narration="Provision for professional charges",
                source="MANUAL",
                lines=[
                    LineInput(account_code="6510", debit=money(amount), description="Provision"),
                    LineInput(
                        account_id=engine.account_by_system_key("ACCRUED_EXPENSES").id,
                        credit=money(amount),
                        description="Provision",
                    ),
                ],
            ),
        )
    stats.seeded_issues.append("Round-number manual provisions")


def _generate_bank_feed(
    db: Session,
    company: Company,
    bank_account: BankAccount,
    start: date,
    end: date,
    rng: random.Random,
    stats: DemoStats,
) -> int:
    """Create a bank statement that mirrors the ledger, with realistic gaps.

    Most lines match a posted receipt or payment (so reconciliation has real
    work to do and mostly succeeds); a handful are bank-only items — charges,
    interest, an unidentified credit — which is exactly what an accountant has
    to investigate at month end.
    """
    from app.models import JournalLine

    lines = list(
        db.execute(
            select(JournalLine)
            .where(
                JournalLine.company_id == company.id,
                JournalLine.account_id == bank_account.gl_account_id,
                JournalLine.status == "POSTED",
                JournalLine.entry_date >= start,
                JournalLine.entry_date <= end,
            )
            .order_by(JournalLine.entry_date)
        ).scalars()
    )

    rows: list[banking_service.ImportRow] = []
    for line in lines:
        # ~6% of ledger movements have not hit the bank statement yet.
        if rng.random() < 0.06:
            continue

        is_inflow = line.debit > ZERO
        amount = money(line.debit if is_inflow else line.credit)
        if amount <= ZERO:
            continue

        entry = line.entry
        reference = entry.reference or entry.entry_number
        counterparty = (line.description or entry.narration or "")[:60]
        prefix = "NEFT-CR" if is_inflow else "NEFT-DR"

        # Banks post a day or two after the book date.
        value_date = line.entry_date + timedelta(days=rng.choice([0, 0, 1, 1, 2]))
        if value_date > end:
            value_date = end

        rows.append(
            banking_service.ImportRow(
                txn_date=value_date,
                description=f"{prefix}-{counterparty}-REF{reference}",
                amount=amount,
                direction=BankTxnDirection.CREDIT if is_inflow else BankTxnDirection.DEBIT,
                reference=reference,
            )
        )

    # Bank-only items the books do not know about yet.
    month_cursor = start.replace(day=1)
    bank_only = 0
    while month_cursor <= end:
        days = monthrange(month_cursor.year, month_cursor.month)[1]
        charge_date = min(month_cursor.replace(day=min(28, days)), end)
        rows.append(
            banking_service.ImportRow(
                txn_date=charge_date,
                description="BANK CHARGES-ACCOUNT MAINTENANCE FEE",
                amount=money(rng.choice([590, 826, 1180])),
                direction=BankTxnDirection.DEBIT,
                reference=f"CHG{rng.randint(100000, 999999)}",
            )
        )
        bank_only += 1
        if month_cursor.month % 3 == 0:
            rows.append(
                banking_service.ImportRow(
                    txn_date=charge_date,
                    description="INTEREST CREDIT-SAVINGS INTEREST",
                    amount=money(rng.randint(2400, 8600)),
                    direction=BankTxnDirection.CREDIT,
                    reference=f"INT{rng.randint(100000, 999999)}",
                )
            )
            bank_only += 1
        month_cursor = _add_months(month_cursor, 1)

    # An unidentified credit — the classic "who paid us this?" exception.
    rows.append(
        banking_service.ImportRow(
            txn_date=end - timedelta(days=4),
            description="IMPS-CR-UNIDENTIFIED REMITTER-REF884213",
            amount=money(147500),
            direction=BankTxnDirection.CREDIT,
            reference="IMPS884213",
        )
    )
    bank_only += 1
    stats.seeded_issues.append(f"{bank_only} bank-only transactions requiring investigation")

    rows.sort(key=lambda row: row.txn_date)

    # Running balance, so the statement looks like a real export.
    balance = ZERO
    for row in rows:
        balance = money(balance + (row.amount if row.direction == BankTxnDirection.CREDIT else -row.amount))
        row.balance = balance

    result = banking_service.import_transactions(
        db,
        company.id,
        bank_account.id,
        rows,
        filename="novatech-demo-statement.csv",
        source="CSV",
        provider="mock",
    )
    return result.imported


def _add_months(value: date, months: int) -> date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def demo_company_exists(db: Session) -> Company | None:
    return db.execute(select(Company).where(Company.is_demo.is_(True))).scalars().first()


def reset_demo(db: Session) -> int:
    """Delete every demo company and all of its data.

    Deletion is explicitly ordered rather than left to cascades. Several
    accounting tables deliberately use ``ondelete=RESTRICT`` — a journal line
    must never be silently removed just because someone deleted an account — so
    a plain ``DELETE FROM companies`` is (correctly) refused by the database.
    Children are therefore removed first, deepest dependency outwards.
    """
    from sqlalchemy import delete

    from app.models import (
        AccrualSchedule,
        AgentMetric,
        AgentRun,
        AIDecisionLog,
        AIProposal,
        AmortisationSchedule,
        Anomaly,
        AuditEvent,
        BankAccount,
        BankStatement,
        BankTransaction,
        Bill,
        BillLine,
        Branch,
        Budget,
        BudgetLine,
        CategorisationRule,
        CloseChecklist,
        CloseTask,
        Conversation,
        ConversationMessage,
        CostCenter,
        CreditNote,
        CreditNoteLine,
        Customer,
        DebitNote,
        DebitNoteLine,
        DeliveryNote,
        DeliveryNoteLine,
        Department,
        DepreciationEntry,
        EInvoice,
        Employee,
        EWayBill,
        ExceptionItem,
        ExpenseClaim,
        FiscalYear,
        FixedAsset,
        GoodsReceipt,
        GoodsReceiptLine,
        GSTReconciliationItem,
        GSTReturn,
        ImportBatch,
        Invoice,
        InvoiceLine,
        Item,
        ItemCategory,
        JournalEntry,
        JournalLine,
        JournalTemplate,
        Membership,
        Notification,
        NumberSequence,
        OpeningBalance,
        Payment,
        PaymentAllocation,
        PayrollRun,
        Payslip,
        Policy,
        PurchaseOrder,
        PurchaseOrderLine,
        Quotation,
        QuotationLine,
        Receipt,
        ReceiptAllocation,
        ReconciliationMatch,
        ReconciliationSession,
        RecurringTransaction,
        SalaryComponent,
        SalesOrder,
        SalesOrderLine,
        ScheduleEntry,
        StockAdjustment,
        StockAdjustmentLine,
        StockBalance,
        StockMove,
        StockTransfer,
        StockTransferLine,
        StoredDocument,
        TaxComponentTotal,
        TaxRateRule,
        TCSEntry,
        TCSRule,
        TDSEntry,
        TDSSection,
        UnitOfMeasure,
        Warehouse,
        ApprovalRule,
        AccountingPeriod,
        Account,
    )

    # Leaf tables first, working outwards to the roots.
    ORDER = [
        ReconciliationMatch, ReconciliationSession, BankTransaction, BankStatement,
        ReceiptAllocation, PaymentAllocation,
        InvoiceLine, CreditNoteLine, DeliveryNoteLine, QuotationLine, SalesOrderLine,
        BillLine, DebitNoteLine, GoodsReceiptLine, PurchaseOrderLine,
        StockTransferLine, StockAdjustmentLine, StockMove, StockBalance,
        ScheduleEntry, DepreciationEntry,
        Payslip, BudgetLine, CloseTask, ConversationMessage,
        AIDecisionLog, AIProposal, AgentMetric, AgentRun,
        Anomaly, ExceptionItem, Notification, AuditEvent,
        EInvoice, EWayBill, GSTReconciliationItem, GSTReturn,
        TDSEntry, TCSEntry, TaxComponentTotal,
        Receipt, Payment, CreditNote, DebitNote,
        Invoice, Bill, DeliveryNote, GoodsReceipt, Quotation, SalesOrder, PurchaseOrder,
        StockTransfer, StockAdjustment, ExpenseClaim, StoredDocument,
        AmortisationSchedule, AccrualSchedule, RecurringTransaction, FixedAsset,
        PayrollRun, SalaryComponent, Employee,
        CloseChecklist, Budget, Conversation, ImportBatch,
        CategorisationRule, BankAccount,
        Customer, Vendor, Item, ItemCategory, Warehouse, UnitOfMeasure,
        JournalLine, JournalEntry, OpeningBalance, JournalTemplate,
        AccountingPeriod, FiscalYear,
        TaxRateRule, TDSSection, TCSRule,
        ApprovalRule, Policy, NumberSequence,
        CostCenter, Department, Branch, Membership,
    ]

    companies = list(db.execute(select(Company).where(Company.is_demo.is_(True))).scalars())
    company_ids = [company.id for company in companies]
    if not company_ids:
        return 0

    # Objects already loaded in this session would otherwise be re-flushed.
    db.expunge_all()

    for model in ORDER:
        db.execute(delete(model).where(model.company_id.in_(company_ids)))

    # Accounts are self-referential (parent_id RESTRICT), so remove the deepest
    # levels first rather than in one statement.
    for _ in range(8):
        remaining = db.execute(
            select(Account.id).where(Account.company_id.in_(company_ids))
        ).scalars().all()
        if not remaining:
            break
        parents = set(
            db.execute(
                select(Account.parent_id).where(
                    Account.company_id.in_(company_ids), Account.parent_id.isnot(None)
                )
            ).scalars()
        )
        leaves = [account_id for account_id in remaining if account_id not in parents]
        if not leaves:
            break
        db.execute(delete(Account).where(Account.id.in_(leaves)))

    db.execute(delete(Company).where(Company.id.in_(company_ids)))
    db.flush()
    return len(companies)
