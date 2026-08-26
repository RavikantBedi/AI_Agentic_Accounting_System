"""Money precision and the deterministic tax engine."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.money import (
    D,
    ZERO,
    allocate_proportionally,
    fmt_inr,
    money,
    split_evenly,
)
from app.services.tax import (
    compute_gst,
    compute_round_off,
    compute_tds,
    determine_supply_type,
    resolve_gst_rate,
    state_code_from_gstin,
    validate_ai_tds_suggestion,
    validate_gstin,
    validate_pan,
)


# ----------------------------------------------------------------- money --

def test_decimal_arithmetic_is_exact():
    """The canonical float trap: 0.1 + 0.2 must equal 0.3 exactly."""
    assert money("0.1") + money("0.2") == money("0.3")
    assert money(0.1) + money(0.2) == money("0.30")


def test_money_never_routes_through_binary_float():
    # repr-based conversion keeps 0.1 as 0.1, not 0.1000000000000000055511151231257827
    assert D(0.1) == Decimal("0.1")
    assert D(2.675) == Decimal("2.675")
    assert money(2.675) == Decimal("2.68")  # half-up, not banker's rounding


def test_money_parses_human_formats():
    assert money("1,23,456.78") == Decimal("123456.78")
    assert money("(1,200.00)") == Decimal("-1200.00")
    assert money("₹1,500") == Decimal("1500.00")
    assert money("") == ZERO
    assert money(None) == ZERO


def test_split_evenly_never_loses_a_paisa():
    for total, parts in [("100.00", 3), ("1000.00", 7), ("0.05", 4), ("120000.00", 12)]:
        shares = split_evenly(total, parts)
        assert len(shares) == parts
        assert sum(shares) == money(total), f"{total} split {parts} ways lost money"


def test_allocate_proportionally_preserves_the_total():
    weights = [Decimal("1"), Decimal("2"), Decimal("3")]
    shares = allocate_proportionally("1000.00", weights)
    assert sum(shares) == money("1000.00")
    assert shares[0] < shares[1] < shares[2]


def test_prepaid_amortisation_example():
    """120,000 over 12 months is exactly 10,000 a month."""
    shares = split_evenly("120000.00", 12)
    assert all(share == money("10000.00") for share in shares)
    assert sum(shares) == money("120000.00")


def test_indian_digit_grouping():
    assert fmt_inr("12345678.91") == "₹1,23,45,678.91"
    assert fmt_inr("1000") == "₹1,000.00"
    assert fmt_inr("-5000") == "-₹5,000.00"
    assert fmt_inr("100") == "₹100.00"


def test_decimal_survives_a_database_round_trip(db, company):
    """SQLite must not silently convert money to float."""
    from sqlalchemy import select

    from app.models import ApprovalRule

    rule = ApprovalRule(
        company_id=company.id,
        name="Precision probe",
        entity_type="TEST",
        min_amount=Decimal("0.10"),
        max_amount=Decimal("99999999.99"),
    )
    db.add(rule)
    db.flush()
    db.expire(rule)

    reloaded = db.execute(
        select(ApprovalRule).where(ApprovalRule.id == rule.id)
    ).scalar_one()
    assert reloaded.min_amount == Decimal("0.10")
    assert reloaded.min_amount * 3 == Decimal("0.30")
    assert reloaded.max_amount == Decimal("99999999.99")


# ------------------------------------------------------------------- GST --

def test_intrastate_splits_into_cgst_and_sgst():
    result = compute_gst("100000", "18", "INTRA_STATE")
    assert result.cgst == money("9000")
    assert result.sgst == money("9000")
    assert result.igst == ZERO
    assert result.total_tax == money("18000")
    assert result.total == money("118000")


def test_interstate_uses_igst_only():
    result = compute_gst("100000", "18", "INTER_STATE")
    assert result.igst == money("18000")
    assert result.cgst == result.sgst == ZERO


def test_cgst_and_sgst_always_sum_to_the_total_tax():
    """Halving the total (not the rate) means odd paise can never be lost."""
    for amount in ["1234.57", "999.99", "0.01", "7777.77", "33333.33"]:
        result = compute_gst(amount, "5", "INTRA_STATE")
        assert result.cgst + result.sgst == result.total_tax, amount


def test_export_is_zero_rated_with_a_note():
    result = compute_gst("100000", "18", "EXPORT")
    assert result.total_tax == ZERO
    assert any("zero-rated" in note for note in result.notes)


def test_union_territory_is_flagged():
    result = compute_gst("10000", "18", "INTRA_STATE", place_of_supply="04")
    assert result.is_union_territory is True
    assert any("UTGST" in note for note in result.notes)


def test_supply_type_determination():
    assert determine_supply_type("27", "27") == "INTRA_STATE"
    assert determine_supply_type("27", "29") == "INTER_STATE"
    assert determine_supply_type("27", "29", treatment="EXPORT") == "EXPORT"
    assert determine_supply_type("27", "29", treatment="SEZ") == "INTER_STATE"


def test_round_off_to_the_nearest_rupee():
    assert compute_round_off(Decimal("118000.47")) == (money("118000"), money("-0.47"))
    assert compute_round_off(Decimal("117999.60")) == (money("118000"), money("0.40"))
    assert compute_round_off(Decimal("100.00")) == (money("100"), ZERO)


def test_unresolved_gst_rate_is_reported_not_silently_zero(db, company, today):
    """A rate that cannot be found must not masquerade as a nil rating."""
    resolved = resolve_gst_rate(db, company.id, on_date=today, hsn_sac="99999")
    assert resolved.rate == ZERO
    assert resolved.resolved is False
    assert resolved.source == "UNRESOLVED"


def test_configured_category_rule_resolves(db, company, today):
    resolved = resolve_gst_rate(db, company.id, on_date=today, category="GST_18")
    assert resolved.rate == Decimal("18.000000")
    assert resolved.resolved is True
    assert resolved.source == "RULE_CATEGORY"


def test_caller_supplied_rate_is_used_when_no_rule_matches(db, company, today):
    resolved = resolve_gst_rate(
        db, company.id, on_date=today, hsn_sac="8471", fallback_rate=Decimal("12")
    )
    assert resolved.rate == Decimal("12.000000")
    assert resolved.source == "ITEM_OR_CALLER"


# ---------------------------------------------------------- identifiers --

@pytest.mark.parametrize(
    "gstin,expected",
    [
        ("27AAPFU0939F1ZV", True),
        ("27AAPFU0939F1ZX", False),   # bad checksum
        ("27AAPFU0939F1AV", False),   # 14th char must be Z
        ("27AAPFU0939F1Z", False),    # too short
        ("", False),
    ],
)
def test_gstin_validation(gstin, expected):
    valid, _ = validate_gstin(gstin)
    assert valid is expected


def test_pan_validation():
    assert validate_pan("AAPFU0939F")[0] is True
    assert validate_pan("AAPFU0939")[0] is False
    assert validate_pan("12345ABCDE")[0] is False


def test_state_code_extraction():
    assert state_code_from_gstin("27AAPFU0939F1ZV") == "27"
    assert state_code_from_gstin("") == ""


# ------------------------------------------------------------------- TDS --

def test_tds_below_threshold_does_not_apply(db, company, vendor, today):
    fy_start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)
    result = compute_tds(
        db,
        company.id,
        vendor=vendor,
        section="194J",
        base_amount=Decimal("5000"),
        on_date=today,
        fiscal_year_start=fy_start,
    )
    assert result.applicable is False
    assert "threshold" in result.reason.lower()


def test_tds_applies_above_threshold(db, company, vendor, today):
    fy_start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)
    result = compute_tds(
        db,
        company.id,
        vendor=vendor,
        section="194J",
        base_amount=Decimal("100000"),
        on_date=today,
        fiscal_year_start=fy_start,
    )
    assert result.applicable is True
    assert result.rate == Decimal("10.000000")
    assert result.tds_amount == money("10000")
    assert result.used_no_pan_rate is False


def test_missing_pan_triggers_the_higher_rate(db, company, vendor, today):
    vendor.pan = ""
    db.flush()
    fy_start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)
    result = compute_tds(
        db,
        company.id,
        vendor=vendor,
        section="194J",
        base_amount=Decimal("100000"),
        on_date=today,
        fiscal_year_start=fy_start,
    )
    assert result.used_no_pan_rate is True
    assert result.rate == Decimal("20.000000")
    assert result.tds_amount == money("20000")
    assert "PAN is missing" in result.reason


def test_unconfigured_section_does_not_deduct(db, company, vendor, today):
    fy_start = date(today.year, 4, 1)
    result = compute_tds(
        db,
        company.id,
        vendor=vendor,
        section="194ZZZ",
        base_amount=Decimal("500000"),
        on_date=today,
        fiscal_year_start=fy_start,
    )
    assert result.applicable is False
    assert result.tds_amount == ZERO
    assert "No TDS rule is configured" in result.reason


def test_ai_tds_suggestion_must_pass_the_rule_gate(db, company, today):
    """A hallucinated section is rejected by the deterministic gate."""
    valid, message = validate_ai_tds_suggestion(db, company.id, "194J", on_date=today)
    assert valid is True

    valid, message = validate_ai_tds_suggestion(db, company.id, "194XYZ", on_date=today)
    assert valid is False
    assert "not a configured TDS section" in message

    valid, message = validate_ai_tds_suggestion(db, company.id, "", on_date=today)
    assert valid is False


def test_tax_rules_are_dated_and_versioned(db, company):
    """A rule that is not yet effective must not be applied."""
    from app.models import TaxRateRule

    resolved = resolve_gst_rate(
        db, company.id, on_date=date(2000, 1, 1), category="GST_18"
    )
    assert resolved.resolved is False, "a rule effective from 2017 must not apply in 2000"
