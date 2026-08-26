"""Authentication, RBAC, and the HTTP API end-to-end."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.enums import Permission, UserRole
from app.errors import PermissionDenied, Unauthorized
from app.security import (
    create_token,
    decode_token,
    hash_password,
    needs_rehash,
    password_strength,
    verify_password,
)
from app.services.policy import ROLE_PERMISSIONS, role_at_least, role_has_permission


# -------------------------------------------------------------- passwords --

def test_password_hash_is_salted_and_verifiable():
    first = hash_password("correct horse battery")
    second = hash_password("correct horse battery")
    assert first != second, "identical passwords must not produce identical hashes"
    assert verify_password("correct horse battery", first)
    assert verify_password("correct horse battery", second)
    assert not verify_password("wrong password", first)


def test_password_hash_never_stores_the_plaintext():
    stored = hash_password("supersecret123")
    assert "supersecret123" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_verify_password_survives_malformed_input():
    for bad in ["", "garbage", "a$b$c", "pbkdf2_sha256$notanint$x$y"]:
        assert verify_password("anything", bad) is False


def test_weak_passwords_are_rejected():
    assert password_strength("short")[0] is False
    assert password_strength("alllettersonly")[0] is False
    assert password_strength("12345678")[0] is False
    assert password_strength("password1")[0] is False
    assert password_strength("Str0ngEnough")[0] is True


def test_rehash_is_flagged_when_iterations_rise():
    weak = hash_password("testpass123", iterations=100)
    assert needs_rehash(weak) is True


# ----------------------------------------------------------------- tokens --

def test_token_round_trip():
    token = create_token("user-1", company_id="co-1", role="OWNER", token_version=3)
    claims = decode_token(token)
    assert claims["sub"] == "user-1"
    assert claims["company_id"] == "co-1"
    assert claims["tv"] == 3


def test_expired_token_is_rejected():
    token = create_token("user-1", expires_minutes=-1)
    with pytest.raises(Unauthorized) as exc:
        decode_token(token)
    assert exc.value.code == "token_expired"


def test_tampered_token_is_rejected():
    token = create_token("user-1")
    tampered = token[:-6] + "AAAAAA"
    with pytest.raises(Unauthorized):
        decode_token(tampered)


def test_refresh_token_cannot_be_used_as_an_access_token():
    token = create_token("user-1", token_type="refresh")
    with pytest.raises(Unauthorized) as exc:
        decode_token(token, expected_type="access")
    assert exc.value.code == "token_wrong_type"


# ------------------------------------------------------------------- RBAC --

def test_viewer_cannot_post_or_approve():
    assert not role_has_permission(UserRole.VIEWER, Permission.POST_JOURNAL.value)
    assert not role_has_permission(UserRole.VIEWER, Permission.APPROVE_PROPOSAL.value)
    assert not role_has_permission(UserRole.VIEWER, Permission.RELEASE_PAYMENT.value)


def test_auditor_is_read_only():
    permissions = ROLE_PERMISSIONS[UserRole.AUDITOR]
    assert Permission.READ_AUDIT.value in permissions
    for write_permission in (
        Permission.POST_JOURNAL.value,
        Permission.MANAGE_SALES.value,
        Permission.RELEASE_PAYMENT.value,
        Permission.CLOSE_PERIOD.value,
    ):
        assert write_permission not in permissions


def test_accountant_cannot_release_payments_or_close_periods():
    permissions = ROLE_PERMISSIONS[UserRole.ACCOUNTANT]
    assert Permission.POST_JOURNAL.value in permissions
    assert Permission.RELEASE_PAYMENT.value not in permissions
    assert Permission.CLOSE_PERIOD.value not in permissions


def test_finance_manager_can_release_and_close():
    permissions = ROLE_PERMISSIONS[UserRole.FINANCE_MANAGER]
    assert Permission.RELEASE_PAYMENT.value in permissions
    assert Permission.CLOSE_PERIOD.value in permissions


def test_owner_has_everything():
    assert len(ROLE_PERMISSIONS[UserRole.OWNER]) == len(list(Permission))


def test_role_seniority_ordering():
    assert role_at_least(UserRole.OWNER, UserRole.ACCOUNTANT) is True
    assert role_at_least(UserRole.FINANCE_MANAGER, UserRole.ACCOUNTANT) is True
    assert role_at_least(UserRole.VIEWER, UserRole.ACCOUNTANT) is False
    assert role_at_least(UserRole.ACCOUNTANT, UserRole.FINANCE_MANAGER) is False


def test_sales_manager_cannot_touch_purchases():
    permissions = ROLE_PERMISSIONS[UserRole.SALES_MANAGER]
    assert Permission.MANAGE_SALES.value in permissions
    assert Permission.MANAGE_PURCHASES.value not in permissions


# -------------------------------------------------------------- HTTP API --

def test_health_endpoint(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ai_provider"] == "mock"


def test_unauthenticated_request_is_rejected(api_client):
    client_without_auth = api_client
    response = client_without_auth.get(
        "/api/v1/accounting/accounts", headers={"Authorization": ""}
    )
    assert response.status_code == 401
    assert response.json()["code"] in ("missing_token", "bad_auth_header", "unauthorized")


def test_malformed_auth_header_is_rejected(api_client):
    response = api_client.get(
        "/api/v1/accounting/accounts", headers={"Authorization": "NotBearer xyz"}
    )
    assert response.status_code == 401


def test_chart_of_accounts_is_returned_as_a_tree(api_client):
    response = api_client.get("/api/v1/accounting/accounts")
    assert response.status_code == 200
    roots = response.json()
    assert len(roots) >= 5
    codes = {node["code"] for node in roots}
    assert {"1000", "2000", "3000", "4000"} <= codes
    assets = next(node for node in roots if node["code"] == "1000")
    assert assets["children"], "the tree must be nested"


def test_money_crosses_the_api_as_a_string(api_client):
    """Floats in JSON would corrupt money exactly as they would in the database."""
    response = api_client.get("/api/v1/accounting/trial-balance")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["total_debit"], str)
    assert "." in body["total_debit"]


def test_full_invoice_lifecycle_over_http(api_client):
    today = date.today().isoformat()

    customer = api_client.post(
        "/api/v1/sales/customers",
        json={"name": "HTTP Customer Ltd", "state_code": "27", "payment_terms_days": 30},
    )
    assert customer.status_code == 201, customer.text
    customer_id = customer.json()["id"]

    invoice = api_client.post(
        "/api/v1/sales/invoices",
        json={
            "customer_id": customer_id,
            "doc_date": today,
            "lines": [
                {"description": "Consulting", "quantity": "10", "rate": "5000", "gst_rate": "18"}
            ],
        },
    )
    assert invoice.status_code == 201, invoice.text
    body = invoice.json()
    assert body["taxable_value"] == "50000.00"
    assert body["cgst"] == "4500.00"
    assert body["total"] == "59000.00"
    assert body["journal_entry_id"]

    # The books must balance immediately afterwards.
    invariants = api_client.get("/api/v1/accounting/invariants").json()
    assert invariants["all_passed"] is True

    receipt = api_client.post(
        "/api/v1/sales/receipts",
        json={
            "customer_id": customer_id,
            "receipt_date": today,
            "amount": "59000.00",
            "method": "NEFT",
        },
    )
    assert receipt.status_code == 201, receipt.text

    refreshed = api_client.get(f"/api/v1/sales/invoices/{body['id']}").json()
    assert refreshed["status"] == "PAID"
    assert refreshed["balance_due"] == "0.00"


def test_validation_errors_are_structured(api_client):
    response = api_client.post(
        "/api/v1/sales/invoices",
        json={"customer_id": "nope", "doc_date": "not-a-date", "lines": []},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["details"]["problems"]


def test_negative_quantity_is_rejected_by_the_schema(api_client):
    customer_id = api_client.post(
        "/api/v1/sales/customers", json={"name": "Neg Qty Co", "state_code": "27"}
    ).json()["id"]

    response = api_client.post(
        "/api/v1/sales/invoices",
        json={
            "customer_id": customer_id,
            "doc_date": date.today().isoformat(),
            "lines": [{"description": "X", "quantity": "-5", "rate": "100"}],
        },
    )
    assert response.status_code == 422


def test_unbalanced_journal_is_rejected_over_http(api_client):
    response = api_client.post(
        "/api/v1/accounting/journals",
        json={
            "entry_date": date.today().isoformat(),
            "narration": "Deliberately broken",
            "lines": [
                {"account_code": "1111", "debit": "100", "credit": "0"},
                {"account_code": "4110", "debit": "0", "credit": "90"},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "unbalanced_journal"


def test_duplicate_bill_is_rejected_over_http(api_client):
    vendor_id = api_client.post(
        "/api/v1/purchases/vendors", json={"name": "HTTP Vendor", "state_code": "27"}
    ).json()["id"]

    payload = {
        "vendor_id": vendor_id,
        "doc_date": date.today().isoformat(),
        "vendor_invoice_number": "HTTP-DUP-1",
        "lines": [{"description": "Service", "quantity": "1", "rate": "1000", "gst_rate": "18"}],
    }
    assert api_client.post("/api/v1/purchases/bills", json=payload).status_code == 201

    second = api_client.post("/api/v1/purchases/bills", json=payload)
    assert second.status_code == 409
    assert second.json()["code"] == "duplicate_document"


def test_reports_are_reachable_and_consistent(api_client):
    customer_id = api_client.post(
        "/api/v1/sales/customers", json={"name": "Report Co", "state_code": "27"}
    ).json()["id"]
    api_client.post(
        "/api/v1/sales/invoices",
        json={
            "customer_id": customer_id,
            "doc_date": date.today().isoformat(),
            "lines": [{"description": "X", "quantity": "1", "rate": "20000", "gst_rate": "18"}],
        },
    )

    for path in (
        "/api/v1/reports/profit-and-loss",
        "/api/v1/reports/balance-sheet",
        "/api/v1/reports/cash-flow",
        "/api/v1/reports/ar-aging",
        "/api/v1/reports/ap-aging",
        "/api/v1/reports/ratios",
        "/api/v1/reports/trend",
        "/api/v1/reports/dashboard",
    ):
        response = api_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.text[:200]}"

    balance_sheet = api_client.get("/api/v1/reports/balance-sheet").json()
    assert balance_sheet["balanced"] is True


def test_csv_export_returns_a_file(api_client):
    response = api_client.get("/api/v1/reports/export/trial-balance.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "Code,Account,Type,Debit,Credit" in response.text


def test_copilot_answers_from_real_data(api_client):
    response = api_client.post("/api/v1/ai/ask", json={"question": "What is our cash position?"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "cash_position"
    assert "get_cash_position" in body["tools_used"]
    assert isinstance(body["confidence"], str)
    assert body["confidence_label"]


def test_ai_provider_status_never_leaks_keys(api_client, monkeypatch):
    """Status may name the env var; it must never expose the value."""
    from app.config import settings

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "AIzaSyTOPSECRETVALUE0123456789abcdef")
    monkeypatch.setattr(settings, "GROK_API_KEY", "xai-TOPSECRETGROKVALUE0123456789")

    from app.ai.providers import clear_availability_cache

    clear_availability_cache()

    response = api_client.get("/api/v1/ai/providers")
    assert response.status_code == 200

    body = response.text
    assert "AIzaSyTOPSECRETVALUE0123456789abcdef" not in body
    assert "xai-TOPSECRETGROKVALUE0123456789" not in body
    assert "TOPSECRET" not in body

    payload = response.json()
    assert {item["name"] for item in payload} == {"gemini", "grok", "ollama", "mock"}
    # Configuration state is reported without the secret itself.
    gemini = next(item for item in payload if item["name"] == "gemini")
    assert gemini["configured"] is True
    clear_availability_cache()


def test_permission_matrix_documents_ai_limits(api_client):
    response = api_client.get("/api/v1/settings/permissions")
    assert response.status_code == 200
    body = response.json()
    assert "post_journal" in body["ai_forbidden"]
    assert "release_payment" in body["ai_forbidden"]
    assert "propose_journal" in body["ai_allowed"]


def test_request_id_is_returned_on_every_response(api_client):
    response = api_client.get("/health")
    assert response.headers.get("X-Request-Id")


def test_security_headers_are_set(api_client):
    response = api_client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_unknown_route_returns_structured_404(api_client):
    response = api_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_global_search_spans_entities(api_client):
    api_client.post(
        "/api/v1/sales/customers", json={"name": "Searchable Traders", "state_code": "27"}
    )
    response = api_client.get("/api/v1/search", params={"q": "Searchable"})
    assert response.status_code == 200
    results = response.json()["results"]
    assert any(item["type"] == "customer" for item in results)


def test_audit_trail_records_and_verifies(api_client):
    api_client.post("/api/v1/sales/customers", json={"name": "Audited Co", "state_code": "27"})

    events = api_client.get("/api/v1/audit").json()
    assert events["total"] > 0

    verification = api_client.get("/api/v1/audit/verify").json()
    assert verification["valid"] is True


def test_file_upload_rejects_an_unsupported_type(api_client):
    response = api_client.post(
        "/api/v1/documents/upload",
        files={"file": ("evil.exe", b"MZ\x90\x00executable", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_file_type"


def test_file_upload_rejects_a_disguised_extension(api_client):
    """The type is decided by content, never by the filename."""
    response = api_client.post(
        "/api/v1/documents/upload",
        files={"file": ("invoice.pdf", b"MZ\x90\x00not really a pdf", "application/pdf")},
    )
    assert response.status_code == 422


def test_valid_pdf_upload_is_accepted(api_client):
    minimal_pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
    response = api_client.post(
        "/api/v1/documents/upload",
        files={"file": ("invoice.pdf", minimal_pdf, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mime_type"] == "application/pdf"
    # No text layer means it must be flagged, never silently accepted as empty.
    assert body["extraction_status"] in ("NEEDS_REVIEW", "EXTRACTED")


def test_duplicate_document_upload_is_detected(api_client):
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF-dup"
    first = api_client.post(
        "/api/v1/documents/upload", files={"file": ("a.pdf", pdf, "application/pdf")}
    )
    assert first.status_code == 201

    second = api_client.post(
        "/api/v1/documents/upload", files={"file": ("b.pdf", pdf, "application/pdf")}
    )
    assert second.status_code == 409
    assert second.json()["code"] == "duplicate_document"


def test_provider_status_declares_demo_mode_honestly(api_client):
    response = api_client.get("/api/v1/tax/providers")
    assert response.status_code == 200
    body = response.json()
    assert body["einvoice"]["mode"] == "MOCK"
    assert "DEMO" in body["einvoice"]["notice"]
    assert body["gst_filing"]["supported"] is False


def test_einvoice_demo_identifiers_are_clearly_marked(api_client):
    customer_id = api_client.post(
        "/api/v1/sales/customers",
        json={"name": "EInv Co", "gstin": "27AAPFU0939F1ZV", "state_code": "27"},
    ).json()["id"]
    invoice_id = api_client.post(
        "/api/v1/sales/invoices",
        json={
            "customer_id": customer_id,
            "doc_date": date.today().isoformat(),
            "lines": [
                {"description": "Goods", "hsn_sac": "8471", "quantity": "1",
                 "rate": "10000", "gst_rate": "18"}
            ],
        },
    ).json()["id"]

    # The company itself needs a GSTIN before e-invoicing is possible.
    api_client.patch("/api/v1/companies/current", json={"gstin": "27AAPFU0939F1ZV"})

    response = api_client.post("/api/v1/tax/einvoice/generate", json={"invoice_id": invoice_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "MOCK"
    assert body["is_government_confirmed"] is False
    assert body["data"]["Irn"].startswith("DEMO-")
    assert "no legal standing" in body["disclaimer"]
