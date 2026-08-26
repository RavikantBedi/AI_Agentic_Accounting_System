# Tax — GST, TDS, TCS and e-Documents

## Read this first

**The tax rates shipped with this system are configuration, not legal advice.**

They are seeded as *editable, dated rules* in the `tax_rate_rules`,
`tds_sections` and `tcs_rules` tables, each with a `legal_reference` field that
says to verify it. Rates change with every Finance Act and with individual
notifications. Before relying on this system for a statutory filing, check the
seeded values against current law and correct them — the system is built to let
you do that, and to recompute correctly afterwards.

**This system does not file anything with the government.** It prepares returns
from your ledger and exports portal-shaped data. You file on the GST portal and
record the acknowledgement here.

---

## Dated rules, not constants

Every rate is a row with an `effective_from` and an optional `effective_to`.
Resolution is by document date:

```python
rate = resolve_gst_rate(db, company_id, hsn="998314", as_of=invoice.invoice_date)
```

This matters more than it sounds. An invoice dated March 2024 that is amended in
2026 must recompute with the March 2024 rate. A system with rates as constants
silently restates history the moment a rate changes. Here the rule that was in
force on the document's own date is the rule that applies, always.

### An unresolvable rate is never silently zero

`resolve_gst_rate` returns a `ResolvedRate`, not a number:

```python
@dataclass
class ResolvedRate:
    rate: Decimal = ZERO
    cess_rate: Decimal = ZERO
    rule_id: str | None = None
    source: str = "UNRESOLVED"

    @property
    def resolved(self) -> bool:
        return self.source != "UNRESOLVED"
```

`source` records *how* the rate was found — the item's own rate, its HSN
mapping, the company default, or nothing at all. Returning a bare `Decimal("0")`
when no rule matched would be indistinguishable from a genuine nil-rated supply,
and a bill would quietly book with no GST. Instead the caller sees
`resolved is False`, the document carries a note, and unresolved lines are
counted in `unresolved_tax_lines` so they can be found and fixed.

This was a real bug during development: expense bills were getting 0% GST
because no HSN rule matched, and nothing said so.

---

## GST

### Supply type

`determine_supply_type()` decides from the place of supply and the supplier's
state:

| Situation | Tax |
|---|---|
| Same state | CGST + SGST, half each |
| Different state | IGST, full rate |
| Union territory without legislature | CGST + UTGST |
| Export / SEZ | Zero-rated, with or without LUT |
| Unregistered customer, ≥ ₹2.5 lakh, interstate | B2CL — reported separately in GSTR-1 |

Compensation cess is computed alongside, at its own rate, on the same base.

### The split always sums exactly

```python
half = (total_tax / Decimal(2)).quantize(Decimal("0.01"), ROUND_HALF_UP)
breakdown.sgst = half
breakdown.cgst = money(total_tax - half)
```

CGST is derived by subtraction rather than computed independently. Rounding each
half separately makes ₹9.99 of tax split into 5.00 + 5.00 = 10.00 — a paisa
created out of nothing, on every odd-valued line, forever. Subtraction cannot
create or destroy value.

Invoice-level round-off is computed by `compute_round_off()` and posted to a
dedicated round-off account, so the ledger reflects it explicitly instead of
absorbing it into revenue.

### GSTIN validation

`validate_gstin()` checks the full 15-character structure — two-digit state
code, ten-character PAN, entity number, the literal `Z`, and the **checksum
digit** computed over base-36 values with the standard alternating weights. A
GSTIN that fails the checksum is rejected at entry, not at filing time.

`validate_pan()` checks the five-letter / four-digit / one-letter structure and
the fourth-character entity code.

### Returns

**GSTR-1** — outward supplies, built from posted invoices and credit/debit
notes, grouped into the portal's own sections: B2B, B2CL, B2CS, CDNR, CDNUR,
EXP, HSN summary, and the document series. Every figure traces to a journal
entry.

**GSTR-3B** — the summary return: outward taxable supplies, zero-rated, exempt,
inward reverse-charge, eligible ITC by category, and the net payable after
credit set-off in the statutory order (IGST first, then CGST/SGST).

**GSTR-2B reconciliation** — match your recorded purchase invoices against the
downloaded 2B. Every line lands in one of four buckets: matched, in-2B-not-in-
books, in-books-not-in-2B, or matched-with-difference. Amount tolerance is
configurable; GSTIN and invoice number are matched exactly.

### Filing is external, and labelled as such

```
prepare → review → export JSON → file on the GST portal → record ARN here
```

`mark_filed_externally()` records the ARN, the filing date and who recorded it.
The system never claims a return was filed. A prepared return is shown as
**PREPARED**, and only becomes **FILED** when a human enters a real
acknowledgement number.

---

## TDS

Sections seeded by default, with thresholds:

| Section | Nature of payment | Rate (with PAN) | Single-payment threshold | Annual threshold |
|---|---|---|---|---|
| 194C | Payments to contractors | 1% | ₹30,000 | ₹1,00,000 |
| 194J | Professional or technical services | 10% | ₹30,000 | ₹30,000 |
| 194I | Rent of land, building or furniture | 10% | — | ₹2,40,000 |
| 194H | Commission or brokerage | 5% | — | ₹15,000 |
| 194A | Interest other than on securities | 10% | — | ₹40,000 |
| 194Q | Purchase of goods | 0.1% | — | ₹50,00,000 |

Without a valid PAN the rate is **20%** under section 206AA.

Two threshold tests, both applied. A single payment over the single-payment
threshold attracts TDS on that payment. Cumulative payments to the same vendor
under the same section crossing the annual threshold attract TDS — and
`vendor_ytd_paid()` computes the year-to-date figure from posted transactions in
the current financial year, so the deduction happens on the payment that crosses
the line, on the full cumulative amount.

TDS is computed on the taxable value **excluding GST**, which is the correct
treatment and a common source of error when done by hand.

`validate_ai_tds_suggestion()` is the gate on the Tax Assistant agent: the agent
may suggest a section, but the rule engine independently resolves the section
and rate for that vendor and payment nature, and the suggestion is discarded if
it disagrees. A model never determines a tax rate here.

---

## TCS

Section 206C(1H) — 0.1% on sales of goods to a buyer beyond ₹50 lakh in a
financial year (1% without PAN). Computed on the amount *exceeding* the
threshold, not on the whole sale, and tracked per buyer per year.

---

## e-Invoice and e-Way Bill

This is where honesty matters most, so it is stated plainly.

**The system builds and validates the payloads. It does not generate real IRNs
or e-Way Bill numbers unless you configure real IRP credentials.**

`app/services/providers.py` defines one interface with two implementations:

**Mock provider** (default). Generates a deterministic identifier prefixed
`DEMO`, stores it with `provider_mode = MOCK`, and every surface that displays
it — the document, the list, the export, the API response — carries the mode.
The UI shows a **Demo mode** badge and the words "no legal validity". A demo IRN
is never presented as a real one.

**Live provider.** Calls the configured IRP. If credentials are missing or the
call fails, **it raises an error**. It does not fall back to the mock provider,
because a fabricated IRN in a customer's records is a far worse outcome than a
failed request. That fallback would be easy to write and is deliberately absent.

The payload building and validation are real work regardless of mode: schema
compliance, mandatory-field checks, GSTIN validation, HSN presence, value
cross-checks and the distance/vehicle rules for e-Way Bills. When you plug in
credentials, the payload that goes to the IRP is the one already being built.

Switch modes in `.env`:

```bash
EINVOICE_PROVIDER=mock      # or irp
EWAYBILL_PROVIDER=mock      # or nic
```

---

## What is out of scope

Stated so nobody discovers it at the wrong moment:

- **No return is filed with any government portal.** Preparation and export only.
- **No e-invoice or e-way bill is registered** without real credentials.
- **No income-tax computation or ITR preparation.** TDS/TCS accounting and
  reporting only.
- **No PF, ESI or professional-tax filing.** Payroll *accounting* is
  implemented; statutory payroll filing is not.
- **No GST portal API integration.** There is a provider abstraction; there is
  no pretend implementation behind it.

---

## Correcting a rate

1. **Settings → Tax rules**, or `PATCH /api/v1/tax/rules/{id}`.
2. Set `effective_to` on the outgoing rule and create the new one with its
   `effective_from`. Do not edit a rate in place — that would restate history.
3. Documents dated before the change keep their original rate. Documents dated
   after use the new one. Nothing is silently recomputed.
4. The change is written to the audit trail with the before and after values.
