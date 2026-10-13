"""Read-only dashboard panel for the Irina provisional utility comparison."""
from __future__ import annotations

import base64
import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st


PROJECT_ROOT = Path.home() / "projects" / "water_bills_rabbi_akiva_vs_hertzl94_13"
ANALYSIS_DIR = PROJECT_ROOT / "analysis"
SUMMARY_PATH = ANALYSIS_DIR / "provisional_overcharge.json"
STATUS_PATH = ANALYSIS_DIR / "utility_analysis_status.json"
ADDRESS_EXPENSES_PATH = ANALYSIS_DIR / "address_search_expenses.json"
COURT_DRAFT_DIR = PROJECT_ROOT / "court_draft"
COURT_DRAFT_PDF_PATH = COURT_DRAFT_DIR / "כתב_תביעה_טיוטה_ראשונה.pdf"
COURT_DRAFT_TEX_PATH = COURT_DRAFT_DIR / "כתב_תביעה_טיוטה_ראשונה.tex"
ELECTRICITY_INVOICE_TABLES_PDF_PATH = ANALYSIS_DIR / "electricity_invoice_tables.pdf"
PANEL_BUILD = "2026-10-12.1"
VALUATION_DATE = date.today()
ANNUAL_RETURN_RATES = {
    2023: 0.0841,
    2024: 0.0958,
}
POST_2024_SCENARIO_RATE = 0.08993430077


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _load_csv(name: str) -> list[dict]:
    path = ANALYSIS_DIR / name
    try:
        with path.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except FileNotFoundError:
        return []


def _money(value) -> str:
    return "Unavailable" if value is None else f"₪{float(value):,.2f}"


def _estimated_money(value) -> str:
    return "Unavailable" if value is None else f"{_money(value)} (estimated)"


def _number(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _nonnegative_overcharge(value) -> float | None:
    number = _number(value)
    return None if number is None else max(0.0, number)


def _parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _adjusted_overcharge(principal: float | None, from_date: str) -> tuple[float | None, str]:
    """Compound the investment-return scenario quarterly through today.

    The supplied figures are realized annual returns, not nominal interest
    rates. Convert each annual return to its equivalent quarterly return so
    four quarters reproduce the documented annual figure exactly.
    """
    if principal is None:
        return None, "Unavailable — no overcharge principal"
    cursor = _parse_date(from_date)
    amount = float(principal)
    while cursor < VALUATION_DATE:
        segment_end = min(date(cursor.year, 12, 31) + timedelta(days=1), VALUATION_DATE)
        rate = ANNUAL_RETURN_RATES.get(cursor.year, POST_2024_SCENARIO_RATE)
        years = (segment_end - cursor).days / 365.2425
        quarterly_rate = (1.0 + rate) ** 0.25 - 1.0
        amount *= (1.0 + quarterly_rate) ** (years * 4.0)
        cursor = segment_end
    return round(amount, 2), (
        "Investment-return scenario; documented annual return converted to its equivalent quarterly "
        "rate because no cadence was found in the supplied Mor statements or the official fund page"
    )


def _scope_month_ends(year: int) -> list[date]:
    """Return tenancy-month endpoints for an annual-average scenario."""
    scope_start = max(date(year, 1, 1), date(2023, 2, 1))
    scope_end = min(date(year, 12, 31), date(2024, 12, 31))
    if scope_start > scope_end:
        return []
    output: list[date] = []
    cursor = date(scope_start.year, scope_start.month, 1)
    while cursor <= scope_end:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        output.append(min(next_month - timedelta(days=1), scope_end))
        cursor = next_month
    return output


def _annual_average_adjusted_total(rows: list[dict], month_ends: list[date]) -> float | None:
    """Apply one year's average monthly overcharge separately at each month end."""
    excess = [value for row in rows if (value := row.get("_excess")) is not None]
    if not excess or not month_ends:
        return None
    average_monthly_excess = sum(excess) / len(month_ends)
    adjusted = [
        value
        for month_end in month_ends
        if (value := _adjusted_overcharge(average_monthly_excess, month_end.isoformat())[0]) is not None
    ]
    return round(sum(adjusted), 2) if adjusted else None


def _summary_row(
    label: str,
    rows: list[dict],
    summary_type: str,
    month_ends: list[date],
    adjusted_override: float | None = None,
) -> dict:
    """Return monthly averages and annual-average scenario subtotals."""
    actual = [value for row in rows if (value := row.get("_actual")) is not None]
    fair = [value for row in rows if (value := row.get("_fair")) is not None]
    excess = [value for row in rows if (value := row.get("_excess")) is not None]
    month_count = len(month_ends)
    adjusted = (
        adjusted_override
        if adjusted_override is not None
        else _annual_average_adjusted_total(rows, month_ends)
    )
    actual_formatter = _estimated_money if any(row.get("_actual_estimated") for row in rows) else _money
    fair_formatter = _estimated_money if any(row.get("_fair_estimated") for row in rows) else _money
    return {
        "Year": label,
        "Summary": summary_type,
        "Rows shown": len(rows),
        "Months represented": month_count,
        "Rows with numeric comparison": len(excess),
        "Average monthly Herzl charge": actual_formatter(sum(actual) / month_count) if actual and month_count else "Missing",
        "Herzl subtotal": actual_formatter(sum(actual)) if actual else "Missing",
        "Average monthly reference charge": fair_formatter(sum(fair) / month_count) if fair and month_count else "Missing",
        "Reference subtotal": fair_formatter(sum(fair)) if fair else "Missing",
        "Average monthly overcharge": _money(sum(excess) / month_count) if excess and month_count else "Missing",
        "Overcharge subtotal": _money(sum(excess)) if excess else "Missing",
        f"Annual-average adjusted subtotal at {VALUATION_DATE.isoformat()}": _money(adjusted) if adjusted is not None else "Missing",
    }


def _annual_rows(rows: list[dict]) -> list[dict]:
    """Calculate the separate annual-average scenario and its grand total."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        year = str(row.get("_sort") or row.get("Period") or "")[:4]
        if year.isdigit():
            grouped.setdefault(year, []).append(row)
    output: list[dict] = []
    all_month_ends: list[date] = []
    annual_adjusted_totals: list[float] = []
    for year, year_rows in sorted(grouped.items()):
        month_ends = _scope_month_ends(int(year))
        all_month_ends.extend(month_ends)
        adjusted = _annual_average_adjusted_total(year_rows, month_ends)
        if adjusted is not None:
            annual_adjusted_totals.append(adjusted)
        output.append(_summary_row(year, year_rows, "Annual average repeated monthly", month_ends, adjusted))
    if rows:
        output.append(_summary_row(
            "GRAND TOTAL",
            rows,
            "Sum of annual-average scenarios",
            all_month_ends,
            round(sum(annual_adjusted_totals), 2) if annual_adjusted_totals else None,
        ))
    return output


def _period_total_row(rows: list[dict]) -> dict:
    """Build a display-only total row without changing the underlying artifacts."""
    displayed_keys = [key for key in rows[0] if not key.startswith("_")]
    total = {key: "" for key in displayed_keys}
    actual = [value for row in rows if (value := row.get("_actual")) is not None]
    fair = [value for row in rows if (value := row.get("_fair")) is not None]
    excess = [value for row in rows if (value := row.get("_excess")) is not None]
    adjusted = [value for row in rows if (value := row.get("_adjusted")) is not None]
    reference_key = next((key for key in displayed_keys if key.startswith("Reference")), None)
    adjusted_key = next((key for key in displayed_keys if key.startswith("Overcharge at ")), None)
    actual_formatter = _estimated_money if any(row.get("_actual_estimated") for row in rows) else _money
    fair_formatter = _estimated_money if any(row.get("_fair_estimated") for row in rows) else _money
    total["Period"] = "TOTAL"
    total["Herzl charge"] = actual_formatter(sum(actual)) if actual else "Missing"
    if reference_key:
        total[reference_key] = fair_formatter(sum(fair)) if fair else "Missing"
    total["Overcharge"] = _money(sum(excess)) if excess else "Missing"
    if adjusted_key:
        total[adjusted_key] = _money(sum(adjusted)) if adjusted else "Missing"
    if "Data status" in total:
        total["Data status"] = "Total of displayed billing-period rows"
    return total


def _display_water_period(row: dict) -> str:
    """Convert the water provider's MM-MM/YY labels to an unambiguous ISO-month range."""
    identifier = str(row.get("id") or "")
    mapping = {
        "herzl-2023-04-05": "2023-04-01 to 2023-05-31",
        "herzl-2023-06-07": "2023-06-01 to 2023-07-31",
        "herzl-2023-08-09": "2023-08-01 to 2023-09-30",
        "herzl-2023-10-11": "2023-10-01 to 2023-11-30",
        "herzl-2023-12-2024-01": "2023-12-01 to 2024-01-31",
        "herzl-2024-02-03": "2024-02-01 to 2024-03-31",
        "herzl-2024-04-05": "2024-04-01 to 2024-05-31",
        "herzl-2024-06-07": "2024-06-01 to 2024-07-31",
        "herzl-2024-08-09": "2024-08-01 to 2024-09-30",
        "herzl-2024-10-11": "2024-10-01 to 2024-11-30",
        "herzl-2024-12-2025-01": "2024-12-01 to 2025-01-31",
    }
    return mapping.get(identifier, str(row.get("period") or ""))


def _tenancy_clip(start: str, end: str) -> str:
    """Show the invoice portion in the agreed tenancy without changing its amount."""
    tenancy_start, tenancy_end = "2023-02-01", "2024-12-31"
    clipped_start, clipped_end = max(start, tenancy_start), min(end, tenancy_end)
    if clipped_start > clipped_end:
        return "Outside tenancy"
    return f"{clipped_start} to {clipped_end}"


def _render_pdf(path: Path, height: int = 820) -> None:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    st.iframe(
        f"data:application/pdf;base64,{payload}#toolbar=1&navpanes=0&view=FitH",
        width="stretch", height=height,
    )


def _source_path(relative: str) -> Path:
    return PROJECT_ROOT / "source" / relative


def _searchable_path(relative: str) -> Path:
    relative_path = Path(relative)
    if str(relative_path).startswith(("Arnona_Herzl/", "Electricity_Herzl/")):
        return PROJECT_ROOT / "extracted" / "new_evidence" / "searchable_pdf" / relative_path.with_suffix(".pdf")
    return PROJECT_ROOT / "extracted" / "ocr_pdf" / relative_path


def render_utility_overcharge(project: dict) -> None:
    """Render source-traceable provisional results without changing any data."""
    with st.expander("Provisional utility comparison · water, electricity, Arnona", expanded=True):
        st.caption(f"Utility panel build: {PANEL_BUILD}")
        result = _load_json(SUMMARY_PATH, {})
        if not result:
            st.error(f"Analysis artifact is missing: {SUMMARY_PATH}")
            return

        st.warning(result.get("disclaimer", "Provisional comparison only."))
        actual = result.get("actual_charges") or {}
        st.markdown("#### Actual source charges · not an overcharge calculation")
        st.caption(str(actual.get("scope") or ""))

        water_actual = _load_csv("water_extraction.csv")
        with st.expander("Water · periods and actual charges", expanded=False):
            st.dataframe([
                {"Property": row.get("property"), "Period": row.get("period"),
                 "Actual charge": _money(row.get("amount_nis")),
                 "Source / page": f"{row.get('source')} · p. {row.get('source_page')}"}
                for row in water_actual
            ], width="stretch", hide_index=True)
            water_summary = actual.get("water") or {}
            for property_name, subtotal in (water_summary.get("property_subtotals_nis") or {}).items():
                st.caption(f"{property_name} subtotal: {_money(subtotal)}")
            st.markdown(f"**Water total: {_money(water_summary.get('utility_total_nis'))}**")

        rabbi_electricity = _load_csv("electricity_rabbi_akiva_extraction.csv")
        herz_electricity = _load_csv("electricity_herzl_extraction.csv")
        with st.expander("Electricity · periods and actual charges", expanded=False):
            st.caption("Invoice tables: Rabbi Akiva 8 and Herzl 94.")
            if ELECTRICITY_INVOICE_TABLES_PDF_PATH.is_file():
                st.download_button(
                    "Download PDF: Даты · КВт · Сумма",
                    ELECTRICITY_INVOICE_TABLES_PDF_PATH.read_bytes(),
                    file_name="electricity_invoice_tables.pdf",
                    mime="application/pdf",
                    key=f"electricity_invoice_tables_pdf_{project.get('id')}",
                    type="primary",
                )
            else:
                st.error("Electricity tables PDF is not available.")

            electricity_rows = [
                {"Property": row.get("property"), "Period": row.get("period"),
                 "Actual charge": _money(row.get("actual_charge_nis")),
                 "Source / page": f"{row.get('source')} · p. {row.get('source_page')}"}
                for row in rabbi_electricity
            ] + [
                {"Property": row.get("property"),
                 "Period": f"{row.get('period_start')} to {row.get('period_end')}",
                 "Actual charge": _money(row.get("invoice_total_nis")),
                 "Source / page": f"{row.get('source')} · p. {row.get('source_page')}"}
                for row in herz_electricity
            ]
            st.dataframe(electricity_rows, width="stretch", hide_index=True)
            electricity_summary = actual.get("electricity") or {}
            for property_name, subtotal in (electricity_summary.get("property_subtotals_nis") or {}).items():
                st.caption(f"{property_name} subtotal: {_money(subtotal)}")
            st.markdown(f"**Electricity total: {_money(electricity_summary.get('utility_total_nis'))}**")

        arnona_actual = _load_csv("arnona_extraction.csv")
        with st.expander("Arnona · bimonthly bills · 62 m² billed area · 31.66 m² share", expanded=False):
            st.info(
                "The source invoices show 62 m² for the account. The apartment share is 31.66 m² according to the attached area calculation."
            )
            st.dataframe([
                {"Property": row.get("property"),
                 "Period": f"{row.get('period_start')} to {row.get('period_end')}",
                 "Charge": _money(row.get("amount_nis")), "Status": row.get("source_status"),
                 "Source": row.get("source")}
                for row in arnona_actual
            ], width="stretch", hide_index=True)
            arnona_summary = actual.get("arnona") or {}
            for component, subtotal in (arnona_summary.get("component_subtotals_nis") or {}).items():
                st.caption(f"{component}: {_money(subtotal)}")
            st.markdown(f"**Known bimonthly-bill subtotal: {_money(arnona_summary.get('utility_total_nis'))}**")
            controls = arnona_summary.get("statement_controls") or {}
            st.caption(
                "Annual statement controls (not added to the subtotal): "
                f"2023 prorated assessment {_money(controls.get('2023_prorated_assessment_nis'))}; "
                f"2024 annual assessment {_money(controls.get('2024_annual_assessment_nis'))}."
            )
            arnona_area = result.get("arnona_area_comparison") or {}
            st.caption(
                f"Allocated fair share for {arnona_area.get('share_area_m2', arnona_area.get('provisional_share_area_m2', 31.66))} / "
                f"{arnona_area.get('billed_area_m2', 62)} m²: {_estimated_money(arnona_area.get('provisional_fair_share_nis'))}; "
                f"allocated excess: {_estimated_money(_nonnegative_overcharge(arnona_area.get('provisional_excess_nis')))}."
            )

        st.info(
            f"Scoped grand total of all source charges listed above: {_money(actual.get('grand_total_nis'))}. "
            "This spans different properties and periods and is not an overcharge total."
        )
        st.caption(str(actual.get("exclusions") or ""))

        st.markdown("#### Provisional overcharge comparison")
        observed = result.get("observed_only") or {}
        estimate = result.get("estimate") or {}
        mode = st.radio(
            "Result view", ["Estimate / extrapolation", "Observed-only matched rows"],
            horizontal=True,
            index=0,
            key=f"utility_result_mode_v5_{project.get('id')}",
        )
        selected = observed if mode.startswith("Observed") else estimate
        comparison_suffix = "observed_comparison.csv" if mode.startswith("Observed") else "estimate_comparison.csv"
        water_comparison = _load_csv(f"water_{comparison_suffix}")
        electricity_comparison = _load_csv(f"electricity_{comparison_suffix}")
        current_arnona_rows = _load_csv("arnona_extraction.csv")
        water_excess = round(sum(_nonnegative_overcharge(row.get("overcharge_nis")) or 0.0 for row in water_comparison), 2)
        electricity_excess = round(sum(_nonnegative_overcharge(row.get("overcharge_nis")) or 0.0 for row in electricity_comparison), 2)
        arnona_excess = round(sum(_nonnegative_overcharge(row.get("provisional_excess_nis")) or 0.0 for row in current_arnona_rows), 2)
        if mode.startswith("Estimate"):
            arnona_missing_charges = (2453.76 - 1678.55) + (2753.42 - 2329.54)
            arnona_excess = round(arnona_excess + arnona_missing_charges * (62 - 31.66) / 62, 2)
        utility_total = round(water_excess + electricity_excess, 2)
        total_with_arnona = round(utility_total + arnona_excess, 2)
        columns = st.columns(4)
        columns[0].metric("Water", _money(water_excess))
        columns[1].metric("Electricity", _estimated_money(electricity_excess))
        columns[2].metric("Arnona", _estimated_money(arnona_excess))
        columns[3].metric("Partial total incl. Arnona", _estimated_money(total_with_arnona))
        st.caption(
            f"Utilities-only subtotal: {_estimated_money(utility_total)}; "
            "the displayed partial total now includes the Arnona area allocation."
        )
        st.caption(
            "Overcharge is floored at ₪0.00 for each billing period: a lower comparison charge "
            "does not create a reciprocal credit."
        )
        st.caption(str(selected.get("total_scope") or ""))
        if mode.startswith("Estimate") and selected.get("partial_range_nis"):
            low, high = selected["partial_range_nis"]
            st.info(
                "Sensitivity range for total incl. Arnona: "
                f"{_estimated_money(low + arnona_excess)}–{_estimated_money(high + arnona_excess)}"
            )

        address_expenses = _load_json(ADDRESS_EXPENSES_PATH, {})
        if address_expenses:
            expense_total = float(address_expenses.get("total_nis") or 0.0)
            combined = total_with_arnona + expense_total
            st.caption(
                f"Current dashboard amount plus documented address-search expenses: {_estimated_money(combined)} "
                "(arithmetic display only; legal treatment remains open). Details are in Bill / period rows."
            )

        tab_rows, tab_gaps, tab_method, tab_court_draft, tab_evidence = st.tabs(
            ["Bill / period rows", "Missing evidence", "Method and limits", "טיוטת כתב תביעה", "Evidence viewer"]
        )
        with tab_rows:
            st.markdown("##### Bills and documented case expenses")
            st.info(
                "Occupancy window used for this view: **2023-02-01 to 2024-12-31**. "
                "Source billing periods that extend outside it are clipped only for display; their recorded amounts are unchanged."
            )
            water_rows: list[dict] = []
            electricity_rows: list[dict] = []
            arnona_rows: list[dict] = []
            expense_rows: list[dict] = []
            adjusted_column = f"Overcharge at {VALUATION_DATE.isoformat()}"

            for row in water_comparison:
                display_period = _display_water_period(row)
                actual_value = _number(row.get("tariff_total_nis"))
                fair_value = _number(row.get("baseline_nis"))
                excess_value = _nonnegative_overcharge(row.get("overcharge_nis"))
                status = "Invoice volumes and tariff total; normalized Rabbi Akiva reference volume"
                adjusted_value, adjustment_status = _adjusted_overcharge(excess_value, display_period[-10:])
                water_rows.append({
                    "_sort": display_period[:10], "_actual": actual_value,
                    "_fair": fair_value, "_fair_estimated": False,
                    "_excess": excess_value, "_adjusted": adjusted_value,
                    "Period": display_period,
                    "Herzl volume (m³)": f"{float(row.get('billed_m3') or 0):.2f}",
                    "Rabbi Akiva reference (m³)": f"{float(row.get('reference_m3') or 0):.2f}",
                    "Excess volume (m³)": f"{float(row.get('excess_m3') or 0):.2f}",
                    "Weighted tariff (₪/m³)": f"{float(row.get('weighted_tariff_nis_per_m3') or 0):.4f}",
                    "Herzl tariff total": _money(actual_value),
                    "Reference cost": _money(fair_value),
                    "Overcharge": _money(excess_value),
                    adjusted_column: _money(adjusted_value),
                    "Data status": status,
                    "Adjustment status": adjustment_status,
                    "Method / source": f"{row.get('source')}; {row.get('notes') or ''}",
                })

            for row in electricity_comparison:
                start, end = str(row.get("period_start")), str(row.get("period_end"))
                actual_value = _number(row.get("invoice_total_nis"))
                fair_value = _number(row.get("baseline_nis"))
                excess_value = _nonnegative_overcharge(row.get("overcharge_nis"))
                estimated_actual = str(row.get("invoice_number") or "").startswith("EST-")
                missing_days = int(float(row.get("unobserved_baseline_days") or 0))
                if estimated_actual:
                    status = "Actual charge estimated — period invoice missing"
                else:
                    status = "Source charge"
                if missing_days:
                    status += f"; fair charge partly estimated ({missing_days} unobserved reference days)"
                else:
                    status += "; fair charge from observed Rabbi Akiva dates"
                fair_estimated = bool(missing_days)
                adjusted_value, adjustment_status = _adjusted_overcharge(excess_value, end)
                electricity_rows.append({
                    "_sort": start, "_actual": actual_value, "_actual_estimated": estimated_actual,
                    "_fair": fair_value, "_fair_estimated": fair_estimated,
                    "_excess": excess_value, "_adjusted": adjusted_value,
                    "Period": _tenancy_clip(start, end),
                    "Herzl charge": _estimated_money(actual_value) if estimated_actual else _money(actual_value),
                    "Reference / Rabbi Akiva charge": _estimated_money(fair_value) if fair_estimated else _money(fair_value),
                    "Overcharge": _money(excess_value),
                    adjusted_column: _money(adjusted_value),
                    "Data status": status,
                    "Adjustment status": adjustment_status,
                    "Method / source": f"{row.get('source')} · p. {row.get('source_page')}; {row.get('notes') or ''}",
                })

            arnona_by_start = {str(row.get("period_start")): row for row in arnona_actual}
            arnona_estimates = {
                # Allocate the 2023 statement-control residual across its three
                # missing tenancy months at one equal monthly rate.
                "2023-02-01": 258.40,
                "2023-05-01": 516.22,
                # The 2024 residual is the single missing bimonthly period.
                "2024-01-01": 423.88,
            }
            expected_arnona_periods = [
                ("2023-02-01", "2023-02-28", "Missing — February source bill not supplied"),
                ("2023-03-01", "2023-04-30", None),
                ("2023-05-01", "2023-06-30", "Missing — source bill not supplied"),
                ("2023-07-01", "2023-08-31", None),
                ("2023-09-01", "2023-10-31", None),
                ("2023-11-01", "2023-12-31", None),
                ("2024-01-01", "2024-02-29", "Missing — source bill not supplied"),
                ("2024-03-01", "2024-04-30", None),
                ("2024-05-01", "2024-06-30", None),
                ("2024-07-01", "2024-08-31", None),
                ("2024-09-01", "2024-10-31", None),
                ("2024-11-01", "2024-12-31", None),
            ]
            for start, end, missing_status in expected_arnona_periods:
                row = arnona_by_start.get(start)
                estimated_charge = arnona_estimates.get(start)
                actual_value = _number(row.get("amount_nis")) if row else estimated_charge
                fair_value = (
                    _number(row.get("provisional_fair_share_nis")) if row
                    else round(float(estimated_charge) * 31.66 / 62, 2)
                )
                excess_value = _nonnegative_overcharge(
                    _number(row.get("provisional_excess_nis")) if row
                    else round(float(estimated_charge) * (62 - 31.66) / 62, 2)
                )
                adjusted_value, adjustment_status = _adjusted_overcharge(excess_value, end)
                arnona_rows.append({
                    "_sort": start, "_actual": actual_value, "_actual_estimated": not bool(row),
                    "_fair": fair_value, "_fair_estimated": False,
                    "_excess": excess_value, "_adjusted": adjusted_value,
                    "Period": f"{start} to {end}",
                    "Herzl charge": _money(actual_value) if row else _estimated_money(actual_value),
                    "Reference charge (31.66/62 m²)": _money(fair_value),
                    "Overcharge": _money(excess_value),
                    adjusted_column: _money(adjusted_value),
                    "Data status": (
                        f"Estimated — {missing_status}; included in estimate view"
                        if not row else str(row.get("source_status"))
                    ),
                    "Adjustment status": adjustment_status,
                    "Method / source": (
                        f"Area allocation: charge × 31.66 / 62; {row.get('source')}; {row.get('notes')}"
                        if row else (
                            "Estimated from the annual statement-control residual after subtracting supplied bills; "
                            "2023 residual allocated equally by missing month, 2024 residual assigned to Jan–Feb. "
                            "Area allocation: estimated charge × 31.66 / 62."
                        )
                    ),
                })

            for row in address_expenses.get("records") or []:
                date = str(row.get("order_date") or "")[:10]
                expense_rows.append({
                    "_sort": date, "Date": date,
                    "Charge": _money(row.get("amount_nis")), "Status": "Paid confirmation",
                    "Source / explanation": (
                        f"Address-location request; confirmation {row.get('payment_confirmation')}; "
                        f"voucher {row.get('voucher_number')}; {row.get('source')}"
                    ),
                })

            def _render_period_table(title: str, rows: list[dict]) -> None:
                st.markdown(f"###### {title}")
                rows.sort(key=lambda row: row["_sort"])
                displayed_rows = [
                    {key: value for key, value in row.items() if not key.startswith("_")}
                    for row in rows
                ]
                if rows:
                    displayed_rows.append(_period_total_row(rows))
                st.dataframe(
                    displayed_rows,
                    width="stretch", hide_index=True,
                )
                st.caption(
                    "The separate annual-average scenario divides each year's subtotals across its tenancy months, "
                    "repeats that average monthly overcharge at every month-end, and adjusts each month separately. "
                    "It can therefore differ from the TOTAL row, which preserves each billing period's original timing. "
                    "Estimated charge/reference values remain labeled."
                )
                st.dataframe(_annual_rows(rows), width="stretch", hide_index=True)

            _render_period_table("Water", water_rows)
            _render_period_table("Electricity", electricity_rows)
            _render_period_table("Arnona", arnona_rows)
            st.caption(
                "Arnona uses the documented 31.66/62 m² area allocation, not a Rabbi Akiva baseline. Periods without invoices are estimated from annual account-control residuals and explicitly labeled. "
                f"Present-day values use the supplied Mor annual returns (2023: 8.41%; 2024: 9.58%) and the existing 8.9934% post-2024 scenario rate through {VALUATION_DATE.isoformat()}. "
                "No compounding cadence was found in the supplied statements or official Mor fund page, so each documented annual return is converted to its equivalent quarterly rate as directed; this is an investment-return scenario, not a legal-interest conclusion."
            )
            st.markdown("###### Other documented case expenses")
            expense_rows.sort(key=lambda row: row["_sort"])
            st.dataframe(
                [{key: value for key, value in row.items() if key != "_sort"} for row in expense_rows],
                width="stretch", hide_index=True,
            )
            st.caption(
                f"Address-search expenses: {_money(address_expenses.get('total_nis'))}. "
                "They are displayed separately and are not mixed into any utility table or utility-overcharge principal."
            )

            with st.expander("Comparison calculation rows and source fields", expanded=False):
                suffix = comparison_suffix
                for title, filename in (("Water", f"water_{suffix}"), ("Electricity", f"electricity_{suffix}")):
                    rows = _load_csv(filename)
                    st.markdown(f"###### {title}")
                    if rows:
                        st.dataframe(rows, width="stretch", hide_index=True)
                    else:
                        st.caption("No rows available.")
                st.markdown("###### Arnona · invoice-verified bimonthly bills · 31.66/62 m² allocation")
                st.dataframe(_load_csv("arnona_extraction.csv"), width="stretch", hide_index=True)
                st.caption("Arnona source rows are the nine supplied invoices. The comparison estimates three periods without invoices from annual account-control residuals and applies charge × (62 − 31.66) / 62.")

        with tab_gaps:
            for item in result.get("missing_evidence") or []:
                st.markdown(f"- {item}")
            status = _load_json(STATUS_PATH, {})
            if status:
                st.caption(
                    f"Checkpoint: {status.get('checkpoint', 'unknown')} · "
                    f"actor: {status.get('actor_id', 'unknown')} · advances only on ‘next’ or ‘go’."
                )

        with tab_method:
            summary_path = ANALYSIS_DIR / "provisional_overcharge_summary.md"
            if summary_path.is_file():
                st.markdown(summary_path.read_text(encoding="utf-8"))
            else:
                st.error(f"Methodology artifact is missing: {summary_path}")

        with tab_court_draft:
            st.markdown("##### טיוטה ראשונה של כתב התביעה")
            st.warning(
                "זהו מסמך עבודה ראשוני בלבד. לפני הגשה יש להשלים פרטים חסרים, "
                "לאמת את האסמכתאות והפסיקה במקור רשמי, ולקבל ייעוץ משפטי לפי הצורך."
            )
            if COURT_DRAFT_PDF_PATH.is_file():
                _render_pdf(COURT_DRAFT_PDF_PATH, height=900)
                download_columns = st.columns(2)
                download_columns[0].download_button(
                    "הורדת הטיוטה כקובץ PDF",
                    COURT_DRAFT_PDF_PATH.read_bytes(),
                    file_name=COURT_DRAFT_PDF_PATH.name,
                    mime="application/pdf",
                    key=f"court_draft_pdf_{project.get('id')}",
                )
                if COURT_DRAFT_TEX_PATH.is_file():
                    download_columns[1].download_button(
                        "הורדת קובץ המקור",
                        COURT_DRAFT_TEX_PATH.read_bytes(),
                        file_name=COURT_DRAFT_TEX_PATH.name,
                        mime="application/x-tex",
                        key=f"court_draft_tex_{project.get('id')}",
                    )
            else:
                st.error("קובץ הטיוטה טרם הופק.")

        with tab_evidence:
            load_preview = st.checkbox(
                "Load evidence preview",
                value=False,
                key=f"utility_load_evidence_v2_{project.get('id')}",
                help="PDF previews are loaded only on request so ordinary project-page reloads remain fast.",
            )
            if not load_preview:
                st.caption("Preview is paused. Enable it to inspect or download a source document.")
                return
            evidence = []
            for utility, filename in (
                ("Water", "water_extraction.csv"),
                ("Electricity · Rabbi Akiva", "electricity_rabbi_akiva_extraction.csv"),
                ("Electricity", "electricity_herzl_extraction.csv"),
                ("Arnona", "arnona_extraction.csv"),
            ):
                for row in _load_csv(filename):
                    source = str(row.get("source") or "")
                    if source and not source.startswith("MISSING"):
                        label = row.get("period") or row.get("invoice_number") or row.get("transaction") or source
                        evidence.append((utility, str(label), source, str(row.get("source_page") or "")))
            choices = list(range(len(evidence)))
            selected_index = st.selectbox(
                "Source to inspect", choices,
                format_func=lambda index: f"{evidence[index][0]} · {evidence[index][1]} · p. {evidence[index][3]}",
                key=f"utility_evidence_{project.get('id')}",
            )
            utility, label, relative, page = evidence[selected_index]
            original, searchable = _source_path(relative), _searchable_path(relative)
            st.caption(f"{relative} · cited page {page}")
            if original.suffix.lower() == ".pdf" and original.is_file():
                _render_pdf(original)
            elif original.is_file():
                st.image(str(original), caption=f"{utility} · {label}")
            elif searchable.is_file():
                _render_pdf(searchable)
            else:
                st.error("The selected evidence file is missing.")
            download_columns = st.columns(2)
            if original.is_file():
                download_columns[0].download_button(
                    "Download original", original.read_bytes(), file_name=original.name,
                    mime="application/pdf" if original.suffix.lower() == ".pdf" else "image/jpeg",
                    key=f"utility_original_{selected_index}",
                )
            if searchable.is_file():
                download_columns[1].download_button(
                    "Download searchable copy", searchable.read_bytes(), file_name=searchable.name,
                    mime="application/pdf", key=f"utility_searchable_{selected_index}",
                )
