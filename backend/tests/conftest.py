"""Shared test fixtures.

Every test runs against a fresh in-file SQLite database in a temp directory, so
tests are isolated from each other and from the developer's real data.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# Configure the environment before any application module is imported.
_TMP = Path(tempfile.mkdtemp(prefix="ailedger-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-not-used-in-production-0123456789"
os.environ["ENVIRONMENT"] = "local"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["AUTH_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["PASSWORD_HASH_ITERATIONS"] = "1000"  # keep tests fast

from app.database import SessionLocal, create_all, drop_all, engine  # noqa: E402
from app.models import Company, User  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    drop_all()
    create_all()
    yield
    engine.dispose()


@pytest.fixture()
def db():
    """A session that rolls back everything it did when the test ends."""
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def company(db) -> Company:
    """A fully provisioned company: COA, periods, tax rules, policies."""
    from app.services.company import create_company

    return create_company(
        db,
        name="Test Co",
        legal_name="Test Company Private Limited",
        state="Maharashtra",
        state_code="27",
        books_begin_on=date.today().replace(month=1, day=1),
    )


@pytest.fixture()
def owner(db, company) -> User:
    from app.services.company import add_member

    user = User(
        email=f"owner-{company.id[:8]}@test.local",
        full_name="Test Owner",
        password_hash=hash_password("testpass123"),
    )
    db.add(user)
    db.flush()
    add_member(db, company.id, user.id, "OWNER", is_default=True)
    return user


@pytest.fixture()
def engine_for(db, company):
    from app.accounting.engine import AccountingEngine

    return AccountingEngine(db, company.id)


@pytest.fixture()
def customer(db, company):
    from app.models import Customer

    record = Customer(
        company_id=company.id,
        code="CUST0001",
        name="Acme Buyers Ltd",
        state="Maharashtra",
        state_code="27",
        payment_terms_days=30,
    )
    db.add(record)
    db.flush()
    return record


@pytest.fixture()
def interstate_customer(db, company):
    from app.models import Customer

    record = Customer(
        company_id=company.id,
        code="CUST0002",
        name="Bengaluru Traders",
        state="Karnataka",
        state_code="29",
        payment_terms_days=30,
    )
    db.add(record)
    db.flush()
    return record


@pytest.fixture()
def vendor(db, company):
    from app.models import Vendor

    record = Vendor(
        company_id=company.id,
        code="VEND0001",
        name="Supply Partners Pvt Ltd",
        state="Maharashtra",
        state_code="27",
        pan="AAAPA1234A",
        payment_terms_days=30,
    )
    db.add(record)
    db.flush()
    return record


@pytest.fixture()
def item(db, company):
    from app.models import Item

    record = Item(
        company_id=company.id,
        sku="WIDGET-01",
        name="Standard Widget",
        hsn_sac="8471",
        gst_rate=Decimal("18"),
        purchase_price=Decimal("1000.00"),
        selling_price=Decimal("1500.00"),
        uom="NOS",
        reorder_level=Decimal("10"),
        track_inventory=True,
    )
    db.add(record)
    db.flush()
    return record


@pytest.fixture()
def warehouse(db, company):
    from sqlalchemy import select

    from app.models import Warehouse

    return db.execute(
        select(Warehouse).where(Warehouse.company_id == company.id)
    ).scalars().first()


@pytest.fixture()
def bank_account(db, company):
    from app.services.banking import create_bank_account

    return create_bank_account(
        db,
        company.id,
        name="Test Current Account",
        bank_name="Test Bank",
        account_number="1234567890",
        ifsc="TEST0001234",
    )


@pytest.fixture()
def today() -> date:
    return date.today()


@pytest.fixture()
def api_client(db):
    """A TestClient wired to the test session, with an authenticated owner."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app
    from app.services.company import add_member, create_company

    company = create_company(
        db,
        name="API Test Co",
        legal_name="API Test Company Pvt Ltd",
        state_code="27",
        books_begin_on=date.today().replace(month=1, day=1),
    )
    user = User(
        email=f"api-{company.id[:8]}@test.local",
        full_name="API Owner",
        password_hash=hash_password("testpass123"),
    )
    db.add(user)
    db.flush()
    add_member(db, company.id, user.id, "OWNER", is_default=True)

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    from app.security import create_token

    token = create_token(
        user.id, token_type="access", company_id=company.id, role="OWNER", token_version=0
    )
    client.headers.update(
        {"Authorization": f"Bearer {token}", "X-Company-Id": company.id}
    )
    client.company_id = company.id  # type: ignore[attr-defined]
    client.user_id = user.id  # type: ignore[attr-defined]

    yield client
    app.dependency_overrides.clear()
