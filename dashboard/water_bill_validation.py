"""Native Axon project view for manually validating Irina's water bills."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import base64
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st


PROJECT_ID = "16eb1008-d5fb-40bd-9707-8f34e18684f1"
PROJECT_ROOT = (
    Path.home() / "projects" / "water_bills_rabbi_akiva_vs_hertzl94_13"
)
DATA_DIR = PROJECT_ROOT / "web_validation" / "data"
BILLS_PATH = DATA_DIR / "bills.json"
VALIDATION_PATH = DATA_DIR / "validation_state.json"

VALUE_COLUMNS = {
    "Days": "days",
    "Private m³": "privateM3",
    "Shared m³": "sharedM3",
    "Billed m³": "billedM3",
    "Amount ₪": "amountNis",
    "People": "people",
    "Printed": "printed",
    "Due": "due",
}
STATUSES = ["unreviewed", "verified", "needs_correction"]


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _merged_row(bill: dict, validation: dict) -> dict:
    review = validation.get(bill["id"], {})
    values = review.get("values") or {}
    return {
        "ID": bill["id"],
        "Status": review.get("status", "unreviewed"),
        "Property": bill.get("property"),
        "Period": bill.get("period"),
        "Days": values.get("days", bill.get("days")),
        "Private m³": values.get("privateM3", bill.get("privateM3")),
        "Shared m³": values.get("sharedM3", bill.get("sharedM3")),
        "Billed m³": values.get("billedM3", bill.get("billedM3")),
        "Amount ₪": values.get("amountNis", bill.get("amountNis")),
        "People": values.get("people", bill.get("people")),
        "Printed": values.get("printed", bill.get("printed")),
        "Due": values.get("due", bill.get("due")),
        "Confidence": bill.get("confidence"),
        "Reviewer notes": review.get("reviewerNotes", ""),
    }


def _clean_cell(value):
    """Convert dataframe values to JSON-safe Python scalars."""
    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    return value


def _build_validation(edited_rows, old_validation: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    result = {"_updatedAt": now}
    for row in edited_rows:
        bill_id = str(row["ID"])
        previous = old_validation.get(bill_id, {})
        values = {
            source_key: _clean_cell(row.get(display_name))
            for display_name, source_key in VALUE_COLUMNS.items()
        }
        current = {
            "status": str(row.get("Status") or "unreviewed"),
            "reviewerNotes": str(row.get("Reviewer notes") or ""),
            "values": values,
        }
        comparable_previous = {
            "status": previous.get("status", "unreviewed"),
            "reviewerNotes": previous.get("reviewerNotes", ""),
            "values": previous.get("values") or {},
        }
        current["updatedAt"] = (
            previous.get("updatedAt")
            if current == comparable_previous and previous.get("updatedAt")
            else now
        )
        result[bill_id] = current
    return result


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _evidence_paths(bill: dict) -> tuple[Path, Path]:
    relative = Path(str(bill.get("source") or ""))
    return PROJECT_ROOT / "source" / relative, PROJECT_ROOT / "extracted" / "ocr_pdf" / relative


def _render_pdf(path: Path, height: int = 820) -> None:
    pdf_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    st.iframe(
        f"data:application/pdf;base64,{pdf_b64}#toolbar=1&navpanes=0&view=FitH",
        width="stretch",
        height=height,
    )


def render_water_bill_validation(project: dict) -> None:
    """Render the validator inside the canonical Axon Projects tab."""
    with st.expander("Optional manual check · water source rows", expanded=False):
        st.caption(
            "This section concerns water only. ‘Not manually checked’ means an extracted row has not yet "
            "been confirmed by you; it does not mean Arnona, a failed extraction, or an invalid bill. "
            "Changes are saved to the project validation file; the original PDFs are never modified."
        )

        bills = _load_json(BILLS_PATH, [])
        validation = _load_json(VALIDATION_PATH, {})
        if not bills:
            st.error(f"Validation data is missing: {BILLS_PATH}")
            return

        merged_rows = [_merged_row(bill, validation) for bill in bills]
        status_counts = {
            status: sum(row["Status"] == status for row in merged_rows)
            for status in STATUSES
        }
        metric_columns = st.columns(4)
        metric_columns[0].metric("Detailed bills", len(bills))
        metric_columns[1].metric("Verified", status_counts["verified"])
        metric_columns[2].metric("Need correction", status_counts["needs_correction"])
        metric_columns[3].metric("Not manually checked", status_counts["unreviewed"])

        filter_columns = st.columns(2)
        properties = sorted({str(row["Property"]) for row in merged_rows})
        property_filter = filter_columns[0].selectbox(
            "Property", ["All", *properties], key=f"water_property_{project.get('id')}"
        )
        status_filter = filter_columns[1].selectbox(
            "Review status", ["All", *STATUSES], key=f"water_status_{project.get('id')}"
        )
        shown_rows = [
            row for row in merged_rows
            if (property_filter == "All" or row["Property"] == property_filter)
            and (status_filter == "All" or row["Status"] == status_filter)
        ]

        edited = st.data_editor(
            shown_rows,
            key=f"water_bill_editor_{project.get('id')}_{property_filter}_{status_filter}",
            width="stretch",
            hide_index=True,
            num_rows="fixed",
            disabled=["ID", "Property", "Period", "Confidence"],
            column_config={
                "ID": None,
                "Status": st.column_config.SelectboxColumn(
                    "Status", options=STATUSES, required=True, width="medium"
                ),
                "Property": st.column_config.TextColumn("Property", width="medium"),
                "Period": st.column_config.TextColumn("Period", width="small"),
                "Days": st.column_config.NumberColumn("Days", step=1, format="%d"),
                "Private m³": st.column_config.NumberColumn("Private m³", format="%.3f"),
                "Shared m³": st.column_config.NumberColumn("Shared m³", format="%.3f"),
                "Billed m³": st.column_config.NumberColumn("Billed m³", format="%.3f"),
                "Amount ₪": st.column_config.NumberColumn("Amount ₪", format="%.2f"),
                "People": st.column_config.NumberColumn("People", step=1, format="%d"),
                "Reviewer notes": st.column_config.TextColumn("Reviewer notes", width="large"),
            },
        )

        action_columns = st.columns([1, 1, 4])
        if action_columns[0].button(
            "Save validation", type="primary", key=f"save_water_{project.get('id')}"
        ):
            edited_records = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
            by_id = {str(row["ID"]): row for row in merged_rows}
            by_id.update({str(row["ID"]): row for row in edited_records})
            ordered_rows = [by_id[bill["id"]] for bill in bills]
            _atomic_json_write(VALIDATION_PATH, _build_validation(ordered_rows, validation))
            st.success("Validation saved in the Irina project.")
            st.rerun()

        export_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
        action_columns[1].download_button(
            "Export CSV",
            _csv_bytes(export_rows),
            file_name="water-bill-validation.csv",
            mime="text/csv",
            key=f"export_water_{project.get('id')}",
        )
        updated_at = validation.get("_updatedAt")
        if updated_at:
            action_columns[2].caption(f"Last saved: {updated_at}")

        st.markdown("##### Source evidence")
        evidence_options = [bill["id"] for bill in bills]
        chosen_id = st.selectbox(
            "Bill to inspect",
            evidence_options,
            format_func=lambda bill_id: next(
                f"{bill['property']} · {bill['period']}" for bill in bills if bill["id"] == bill_id
            ),
            key=f"water_evidence_{project.get('id')}",
        )
        bill = next(item for item in bills if item["id"] == chosen_id)
        original_pdf, searchable_pdf = _evidence_paths(bill)
        evidence_columns = st.columns([2, 1])
        with evidence_columns[0]:
            if original_pdf.is_file():
                _render_pdf(original_pdf)
            elif searchable_pdf.is_file():
                _render_pdf(searchable_pdf)
            else:
                st.error("The source PDF for this row is missing.")
        with evidence_columns[1]:
            st.markdown(f"**{bill['property']} · {bill['period']}**")
            st.caption(f"OCR confidence: {bill.get('confidence', 'unknown')}")
            if bill.get("notes"):
                st.warning(str(bill["notes"]))
            if original_pdf.is_file():
                st.download_button(
                    "Download original PDF",
                    original_pdf.read_bytes(),
                    file_name=original_pdf.name,
                    mime="application/pdf",
                    key=f"original_water_{chosen_id}",
                )
            if searchable_pdf.is_file():
                st.download_button(
                    "Download searchable OCR PDF",
                    searchable_pdf.read_bytes(),
                    file_name=f"searchable_{searchable_pdf.name}",
                    mime="application/pdf",
                    key=f"searchable_water_{chosen_id}",
                )
