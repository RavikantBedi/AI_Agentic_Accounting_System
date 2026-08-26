"""Deterministic Indian tax engine (GST, TDS, TCS).

Design rules:

* **No rate is ever hardcoded in a calculation.** Every rate is read from a
  dated, versioned rule row, looked up by the *transaction's own date*, so
  historic documents keep recomputing to the same numbers after a law change.
* **AI may suggest, the engine decides.** ``suggest_tds_section`` returns a
  candidate; ``compute_tds`` only ever applies a rule that exists in the
  database, and records whether the AI suggestion agreed.
* Seeded rates below are *starting configuration* for a demo company, editable
  in Settings — they are not a claim of current statutory correctness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.enums import GSTTreatment, SupplyType
from app.errors import ValidationError
from app.models import Customer, TaxRateRule, TCSRule, TDSEntry, TDSSection, Vendor
from app.money import ZERO, D, money, rate as to_rate

# GST state codes used for the union-territory (UTGST) determination.
UNION_TERRITORY_CODES = {"04", "25", "26", "31", "34", "35", "38"}


@dataclass
class TaxBreakdown:
    """The tax result for one line or one document."""

    taxable_value: Decimal = ZERO
    rate: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO
    cess: Decimal = ZERO
    supply_type: str = SupplyType.INTRA_STATE
    is_union_territory: bool = False
    rule_id: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def total_tax(self) -> Decimal:
        return money(self.cgst + self.sgst + self.igst + self.cess)

    @property
    def total(self) -> Decimal:
        return money(self.taxable_value + self.total_tax)

    def as_dict(self) -> dict:
        return {
            "taxable_value": self.taxable_value,
            "rate": self.rate,
            "cgst": self.cgst,
            "sgst": self.sgst,
            "igst": self.igst,
            "cess": self.cess,
            "total_tax": self.total_tax,
            "total": self.total,
            "supply_type": self.supply_type,
            "is_union_territory": self.is_union_territory,
            "rule_id": self.rule_id,
            "notes": self.notes,
        }


# ------------------------------------------------------------------- GST --

def determine_supply_type(
    supplier_state_code: str,
    place_of_supply: str,
    *,
    treatment: str = GSTTreatment.REGISTERED,
) -> str:
    """Intra-state (CGST+SGST) vs inter-state (IGST) vs export."""
    if treatment in (
        GSTTreatment.EXPORT,
        GSTTreatment.EXPORT_WITHOUT_PAYMENT,
        GSTTreatment.OVERSEAS,
    ):
        return SupplyType.EXPORT
    if treatment in (GSTTreatment.SEZ, GSTTreatment.SEZ_WITHOUT_PAYMENT):
        # Supplies to an SEZ are treated as inter-state.
        return SupplyType.INTER_STATE

    supplier = (supplier_state_code or "").strip().zfill(2)
    recipient = (place_of_supply or "").strip().zfill(2)
    if not supplier or not recipient or supplier == "00" or recipient == "00":
        return SupplyType.INTRA_STATE
    return SupplyType.INTRA_STATE if supplier == recipient else SupplyType.INTER_STATE


@dataclass
class ResolvedRate:
    """A GST rate together with *where it came from*.

    ``source`` matters: a rate that could not be resolved at all is not the same
    thing as a genuine nil rating, and the two must never be confused. Silently
    charging 0% because no rule matched is a real-world way to under-collect
    tax, so an unresolved rate is reported as ``UNRESOLVED`` and surfaces as a
    warning on the document.
    """

    rate: Decimal = ZERO
    cess_rate: Decimal = ZERO
    rule_id: str | None = None
    source: str = "UNRESOLVED"

    @property
    def resolved(self) -> bool:
        return self.source != "UNRESOLVED"


def resolve_gst_rate(
    db: Session,
    company_id: str,
    *,
    on_date: date,
    hsn_sac: str = "",
    category: str = "",
    fallback_rate: Decimal | None = None,
) -> ResolvedRate:
    """Resolve the GST rate applicable on ``on_date``.

    Lookup order: HSN/SAC rule -> category rule -> the rate configured on the
    item or supplied by the caller. If none of those produce a rate, the result
    is UNRESOLVED rather than a silent zero.
    """
    stmt = (
        select(TaxRateRule)
        .where(
            TaxRateRule.company_id == company_id,
            TaxRateRule.is_active.is_(True),
            TaxRateRule.effective_from <= on_date,
            or_(TaxRateRule.effective_to.is_(None), TaxRateRule.effective_to >= on_date),
        )
        .order_by(TaxRateRule.version.desc())
    )

    if hsn_sac:
        rule = db.execute(stmt.where(TaxRateRule.hsn_sac == hsn_sac)).scalars().first()
        if rule:
            return ResolvedRate(
                to_rate(rule.total_rate), to_rate(rule.cess_rate), rule.id, "RULE_HSN"
            )
    if category:
        rule = db.execute(stmt.where(TaxRateRule.category == category)).scalars().first()
        if rule:
            return ResolvedRate(
                to_rate(rule.total_rate), to_rate(rule.cess_rate), rule.id, "RULE_CATEGORY"
            )

    if fallback_rate is not None:
        return ResolvedRate(to_rate(fallback_rate), ZERO, None, "ITEM_OR_CALLER")

    return ResolvedRate(ZERO, ZERO, None, "UNRESOLVED")


def compute_gst(
    taxable_value: Decimal | float | str,
    total_rate: Decimal | float | str,
    supply_type: str,
    *,
    cess_rate: Decimal | float | str = ZERO,
    place_of_supply: str = "",
    rule_id: str | None = None,
) -> TaxBreakdown:
    """Split a GST amount into its components.

    Intra-state splits the rate evenly into CGST and SGST (or UTGST). The split
    is computed on the *total tax* so the two halves always sum back exactly —
    computing each half independently can drift by a paisa.
    """
    base = money(taxable_value)
    rate_value = to_rate(total_rate)
    cess_value = to_rate(cess_rate)

    breakdown = TaxBreakdown(
        taxable_value=base,
        rate=rate_value,
        supply_type=supply_type,
        rule_id=rule_id,
        is_union_territory=(place_of_supply or "").strip().zfill(2) in UNION_TERRITORY_CODES,
    )

    if supply_type in (SupplyType.EXEMPT, SupplyType.NIL_RATED, SupplyType.NON_GST):
        breakdown.notes.append(f"{supply_type} supply — no GST charged.")
        return breakdown

    if supply_type == SupplyType.EXPORT:
        breakdown.notes.append("Export supply — zero-rated; GST not charged on the invoice.")
        return breakdown

    total_tax = money(base * rate_value / Decimal(100))

    if supply_type == SupplyType.INTER_STATE:
        breakdown.igst = total_tax
    else:
        # Halve the *total*, then give any odd paisa to CGST so the parts sum exactly.
        half = (total_tax / Decimal(2)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        breakdown.sgst = half
        breakdown.cgst = money(total_tax - half)
        if breakdown.is_union_territory:
            breakdown.notes.append("Union territory: the SGST component is UTGST.")

    if cess_value > 0:
        breakdown.cess = money(base * cess_value / Decimal(100))

    return breakdown


def sum_breakdowns(items: list[TaxBreakdown]) -> TaxBreakdown:
    """Aggregate line-level tax into a document total."""
    total = TaxBreakdown()
    if not items:
        return total
    total.supply_type = items[0].supply_type
    total.is_union_territory = items[0].is_union_territory
    for item in items:
        total.taxable_value = money(total.taxable_value + item.taxable_value)
        total.cgst = money(total.cgst + item.cgst)
        total.sgst = money(total.sgst + item.sgst)
        total.igst = money(total.igst + item.igst)
        total.cess = money(total.cess + item.cess)
    return total


def compute_round_off(amount: Decimal) -> tuple[Decimal, Decimal]:
    """Return ``(rounded_total, round_off_adjustment)`` to the nearest rupee."""
    exact = money(amount)
    rounded = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return money(rounded), money(rounded - exact)


def validate_gstin(gstin: str) -> tuple[bool, str]:
    """Structural GSTIN validation including the checksum digit.

    Format: 2 state digits, 10-char PAN, 1 entity digit, 'Z', 1 check char.
    """
    value = (gstin or "").strip().upper()
    if not value:
        return False, "GSTIN is empty."
    if len(value) != 15:
        return False, f"GSTIN must be 15 characters (got {len(value)})."
    if not value[:2].isdigit():
        return False, "The first two characters must be a numeric state code."
    if value[2:7].isalpha() is False or not value[7:11].isdigit() or not value[11].isalpha():
        return False, "Characters 3-12 must follow the PAN format (AAAAA9999A)."
    if value[13] != "Z":
        return False, "The 14th character must be 'Z'."

    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for index, char in enumerate(value[:14]):
        if char not in alphabet:
            return False, f"Invalid character '{char}' at position {index + 1}."
        factor = 2 if index % 2 else 1
        product = alphabet.index(char) * factor
        total += product // 36 + product % 36
    check_char = alphabet[(36 - total % 36) % 36]
    if check_char != value[14]:
        return False, "GSTIN checksum digit does not match."
    return True, "GSTIN is structurally valid."


def validate_pan(pan: str) -> tuple[bool, str]:
    value = (pan or "").strip().upper()
    if len(value) != 10:
        return False, "PAN must be 10 characters."
    if not (value[:5].isalpha() and value[5:9].isdigit() and value[9].isalpha()):
        return False, "PAN must follow the AAAAA9999A format."
    return True, "PAN is structurally valid."


def state_code_from_gstin(gstin: str) -> str:
    value = (gstin or "").strip()
    return value[:2] if len(value) >= 2 and value[:2].isdigit() else ""


# ------------------------------------------------------------------- TDS --

def resolve_tds_section(
    db: Session, company_id: str, section: str, *, on_date: date
) -> TDSSection | None:
    return (
        db.execute(
            select(TDSSection)
            .where(
                TDSSection.company_id == company_id,
                TDSSection.section == section,
                TDSSection.is_active.is_(True),
                TDSSection.effective_from <= on_date,
                or_(TDSSection.effective_to.is_(None), TDSSection.effective_to >= on_date),
            )
            .order_by(TDSSection.version.desc())
        )
        .scalars()
        .first()
    )


def vendor_ytd_paid(db: Session, company_id: str, vendor_id: str, section: str, fy_start: date) -> Decimal:
    """Base amount already subjected to this TDS section in the fiscal year."""
    rows = db.execute(
        select(TDSEntry.base_amount).where(
            TDSEntry.company_id == company_id,
            TDSEntry.vendor_id == vendor_id,
            TDSEntry.section == section,
            TDSEntry.deduction_date >= fy_start,
        )
    ).scalars()
    return money(sum((D(value) for value in rows), ZERO))


@dataclass
class TDSResult:
    applicable: bool
    section: str = ""
    rate: Decimal = ZERO
    base_amount: Decimal = ZERO
    tds_amount: Decimal = ZERO
    reason: str = ""
    threshold_applied: Decimal = ZERO
    rule_id: str | None = None
    used_no_pan_rate: bool = False


def compute_tds(
    db: Session,
    company_id: str,
    *,
    vendor: Vendor,
    section: str,
    base_amount: Decimal,
    on_date: date,
    fiscal_year_start: date,
) -> TDSResult:
    """Compute TDS deterministically from the configured section rule.

    Threshold logic: a deduction applies if this single payment crosses the
    single-payment threshold, **or** if the fiscal-year total (including this
    payment) crosses the annual threshold.
    """
    base = money(base_amount)
    if base <= ZERO:
        return TDSResult(applicable=False, section=section, reason="Base amount is zero.")

    rule = resolve_tds_section(db, company_id, section, on_date=on_date)
    if rule is None:
        return TDSResult(
            applicable=False,
            section=section,
            reason=(
                f"No TDS rule is configured for section {section} effective "
                f"{on_date.isoformat()}. Configure it in Settings > Tax before deducting."
            ),
        )

    ytd = vendor_ytd_paid(db, company_id, vendor.id, section, fiscal_year_start)
    cumulative = money(ytd + base)

    crosses_single = rule.single_payment_threshold > ZERO and base >= rule.single_payment_threshold
    crosses_annual = rule.annual_threshold > ZERO and cumulative >= rule.annual_threshold
    no_thresholds = rule.single_payment_threshold == ZERO and rule.annual_threshold == ZERO

    if not (crosses_single or crosses_annual or no_thresholds):
        return TDSResult(
            applicable=False,
            section=section,
            base_amount=base,
            rule_id=rule.id,
            reason=(
                f"Below the section {section} threshold: this payment {base} and "
                f"year-to-date {cumulative} are under the single-payment "
                f"({rule.single_payment_threshold}) and annual ({rule.annual_threshold}) limits."
            ),
        )

    has_pan = bool((vendor.pan or "").strip())
    applied_rate = to_rate(rule.rate_with_pan if has_pan else rule.rate_without_pan)
    tds_amount = money(base * applied_rate / Decimal(100))

    if crosses_annual and not crosses_single:
        reason = (
            f"Section {section} applies: year-to-date payments {cumulative} crossed "
            f"the annual threshold {rule.annual_threshold}."
        )
        threshold = rule.annual_threshold
    elif crosses_single:
        reason = (
            f"Section {section} applies: this payment {base} is at or above the "
            f"single-payment threshold {rule.single_payment_threshold}."
        )
        threshold = rule.single_payment_threshold
    else:
        reason = f"Section {section} applies with no threshold."
        threshold = ZERO

    if not has_pan:
        reason += " Vendor PAN is missing, so the higher no-PAN rate applies."

    return TDSResult(
        applicable=True,
        section=section,
        rate=applied_rate,
        base_amount=base,
        tds_amount=tds_amount,
        reason=reason,
        threshold_applied=money(threshold),
        rule_id=rule.id,
        used_no_pan_rate=not has_pan,
    )


def validate_ai_tds_suggestion(
    db: Session, company_id: str, suggested_section: str, *, on_date: date
) -> tuple[bool, str]:
    """The deterministic gate an AI section suggestion must pass."""
    if not suggested_section:
        return False, "No section suggested."
    rule = resolve_tds_section(db, company_id, suggested_section, on_date=on_date)
    if rule is None:
        return False, (
            f"Section '{suggested_section}' is not a configured TDS section as of "
            f"{on_date.isoformat()}; the suggestion was rejected."
        )
    return True, f"Section {suggested_section} ({rule.name}) is configured and effective."


# ------------------------------------------------------------------- TCS --

def compute_tcs(
    db: Session,
    company_id: str,
    *,
    customer: Customer,
    section: str,
    base_amount: Decimal,
    on_date: date,
    ytd_receipts: Decimal = ZERO,
) -> TDSResult:
    """TCS mirrors the TDS pattern: a configured, dated rule decides."""
    base = money(base_amount)
    rule = (
        db.execute(
            select(TCSRule)
            .where(
                TCSRule.company_id == company_id,
                TCSRule.section == section,
                TCSRule.is_active.is_(True),
                TCSRule.effective_from <= on_date,
                or_(TCSRule.effective_to.is_(None), TCSRule.effective_to >= on_date),
            )
            .order_by(TCSRule.version.desc())
        )
        .scalars()
        .first()
    )
    if rule is None:
        return TDSResult(
            applicable=False,
            section=section,
            reason=f"No TCS rule configured for {section} effective {on_date.isoformat()}.",
        )

    cumulative = money(D(ytd_receipts) + base)
    if rule.threshold > ZERO and cumulative <= rule.threshold:
        return TDSResult(
            applicable=False,
            section=section,
            base_amount=base,
            rule_id=rule.id,
            reason=(
                f"Year-to-date {cumulative} has not crossed the {section} "
                f"threshold {rule.threshold}."
            ),
        )

    # TCS applies only to the amount above the threshold.
    taxable = money(cumulative - rule.threshold) if rule.threshold > ZERO else base
    taxable = min(taxable, base)
    has_pan = bool((customer.pan or "").strip())
    applied_rate = to_rate(rule.rate if has_pan else rule.rate_without_pan)

    return TDSResult(
        applicable=True,
        section=section,
        rate=applied_rate,
        base_amount=taxable,
        tds_amount=money(taxable * applied_rate / Decimal(100)),
        reason=(
            f"Section {section} applies on {taxable} "
            f"(amount above the {rule.threshold} threshold)."
        ),
        threshold_applied=money(rule.threshold),
        rule_id=rule.id,
        used_no_pan_rate=not has_pan,
    )


# ------------------------------------------------------- seed configuration --

def seed_tax_rules(db: Session, company_id: str, *, effective_from: date | None = None) -> dict:
    """Seed starting GST slabs, TDS sections and TCS rules for a new company.

    These are editable defaults, versioned from ``effective_from``. They are a
    configuration starting point, not legal advice — see docs/TAX.md.
    """
    start = effective_from or date(2017, 7, 1)
    created = {"gst": 0, "tds": 0, "tcs": 0}

    gst_slabs = [
        ("GST_0", "GST Nil rated", Decimal("0")),
        ("GST_5", "GST 5%", Decimal("5")),
        ("GST_12", "GST 12%", Decimal("12")),
        ("GST_18", "GST 18%", Decimal("18")),
        ("GST_28", "GST 28%", Decimal("28")),
    ]
    for code, name, total in gst_slabs:
        exists = db.execute(
            select(TaxRateRule).where(
                TaxRateRule.company_id == company_id, TaxRateRule.code == code
            )
        ).scalar_one_or_none()
        if exists:
            continue
        half = total / Decimal(2)
        db.add(
            TaxRateRule(
                company_id=company_id,
                code=code,
                name=name,
                category=code,
                total_rate=to_rate(total),
                cgst_rate=to_rate(half),
                sgst_rate=to_rate(half),
                igst_rate=to_rate(total),
                effective_from=start,
                legal_reference="CGST/SGST/IGST Acts — configure per current notifications.",
            )
        )
        created["gst"] += 1

    tds_defaults = [
        ("194C", "Payments to contractors", Decimal("1"), Decimal("30000"), Decimal("100000")),
        ("194J", "Fees for professional or technical services", Decimal("10"), Decimal("30000"), Decimal("30000")),
        ("194I", "Rent of land, building or furniture", Decimal("10"), Decimal("0"), Decimal("240000")),
        ("194H", "Commission or brokerage", Decimal("5"), Decimal("0"), Decimal("15000")),
        ("194A", "Interest other than on securities", Decimal("10"), Decimal("0"), Decimal("40000")),
        ("194Q", "Purchase of goods", Decimal("0.1"), Decimal("0"), Decimal("5000000")),
    ]
    for section, name, rate_value, single, annual in tds_defaults:
        exists = db.execute(
            select(TDSSection).where(
                TDSSection.company_id == company_id, TDSSection.section == section
            )
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(
            TDSSection(
                company_id=company_id,
                section=section,
                name=name,
                nature_of_payment=name,
                rate_with_pan=to_rate(rate_value),
                rate_without_pan=to_rate(Decimal("20")),
                single_payment_threshold=money(single),
                annual_threshold=money(annual),
                effective_from=start,
                legal_reference="Income-tax Act, 1961 — verify against the current Finance Act.",
            )
        )
        created["tds"] += 1

    tcs_exists = db.execute(
        select(TCSRule).where(TCSRule.company_id == company_id, TCSRule.section == "206C(1H)")
    ).scalar_one_or_none()
    if not tcs_exists:
        db.add(
            TCSRule(
                company_id=company_id,
                section="206C(1H)",
                name="TCS on sale of goods",
                rate=to_rate(Decimal("0.1")),
                rate_without_pan=to_rate(Decimal("1")),
                threshold=money(Decimal("5000000")),
                applies_to="SALE_OF_GOODS",
                effective_from=start,
                legal_reference="Section 206C(1H) — verify against the current Finance Act.",
            )
        )
        created["tcs"] += 1

    db.flush()
    return created
