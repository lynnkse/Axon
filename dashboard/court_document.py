"""Read-only court-draft and number-reconciliation pane for the Irina project."""
from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import streamlit as st


PROJECT_ROOT = Path.home() / "projects" / "water_bills_rabbi_akiva_vs_hertzl94_13"
RECONCILIATION_PATH = PROJECT_ROOT / "analysis" / "court_document_reconciliation.json"
HEBREW_DRAFT_TEX_PATH = PROJECT_ROOT / "court_draft" / "כתב_תביעה_טיוטה_ראשונה.tex"
HEBREW_DRAFT_PDF_PATH = PROJECT_ROOT / "court_draft" / "כתב_תביעה_טיוטה_ראשונה.pdf"
EVIDENCE_DB_PATH = PROJECT_ROOT / "analysis" / "case_evidence.sqlite"


def _load_reconciliation() -> dict:
    try:
        return json.loads(RECONCILIATION_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _money(value) -> str:
    return "—" if value is None else f"₪{float(value):,.2f}"


def _load_evidence_status() -> tuple[dict, list[dict]]:
    """Read only the evidence-inventory state; never invent a claim total."""
    if not EVIDENCE_DB_PATH.exists():
        return {}, []
    try:
        with sqlite3.connect(f"file:{EVIDENCE_DB_PATH}?mode=ro", uri=True) as conn:
            metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            rows = conn.execute(
                "SELECT severity, code, detail FROM review_flag "
                "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, code"
            ).fetchall()
        return metadata, [
            {"Severity": severity, "Code": code, "Detail": detail}
            for severity, code, detail in rows
        ]
    except sqlite3.Error:
        return {}, []


def _render_hebrew_draft(project_id: str) -> None:
    """Render the generated Hebrew pleading without changing source evidence."""
    with st.expander("טיוטת כתב התביעה בעברית · צפייה והורדה", expanded=True):
        st.warning(
            "טיוטה ראשונה לביקורת בלבד — אינה ייעוץ משפטי ואינה מוכנה להגשה. "
            "יש לאמת את הפרטים, האסמכתאות והנספחים לפני הגשה לבית המשפט."
        )
        if not HEBREW_DRAFT_PDF_PATH.exists():
            st.warning("קובץ ה־PDF של הטיוטה עדיין אינו זמין.")
            return

        pdf_bytes = HEBREW_DRAFT_PDF_PATH.read_bytes()
        left, middle, right = st.columns(3)
        evidence_metadata, evidence_flags = _load_evidence_status()
        left.metric("מצב חישוב הקרן", "נדרשת הצלבת מקור")
        middle.metric("הוצאות איתור מען", "₪40.00")
        right.metric("מסד ראיות", "SQL פעיל" if evidence_metadata else "לא זמין")
        st.caption(
            "סכום קרן סופי אינו מוצג עד להצלבה מתועדת של חשבונות מקור, תקופות חופפות והנחות אומדן. "
            "תרחיש ההשקעה מוצג כחלופה נפרדת ואינו ריבית חוקית."
        )
        if evidence_metadata:
            st.caption(
                f"נבנה: {evidence_metadata.get('built_at_utc', '—')} · "
                f"מצב חישוב: {evidence_metadata.get('calculation_status', '—')}"
            )
        if evidence_flags:
            with st.expander("דגלי בדיקה ממסד הראיות", expanded=False):
                st.dataframe(evidence_flags, width="stretch", hide_index=True)
        encoded_pdf = base64.b64encode(pdf_bytes).decode("ascii")
        st.iframe(
            f"data:application/pdf;base64,{encoded_pdf}#toolbar=1&navpanes=0",
            width="stretch",
            height=900,
        )
        st.download_button(
            "הורדת טיוטת כתב התביעה (PDF)",
            data=pdf_bytes,
            file_name="כתב_תביעה_אירינה_סירצבה_טיוטה.pdf",
            mime="application/pdf",
            key=f"hebrew_court_draft_pdf_{project_id}",
            type="primary",
        )
        if HEBREW_DRAFT_TEX_PATH.exists():
            st.download_button(
                "הורדת קובץ המקור (LaTeX)",
                data=HEBREW_DRAFT_TEX_PATH.read_bytes(),
                file_name="כתב_תביעה_אירינה_סירצבה_טיוטה.tex",
                mime="application/x-tex",
                key=f"hebrew_court_draft_tex_{project_id}",
            )


def render_court_document(project: dict) -> None:
    """Render the Hebrew PDF, preserved Russian draft, and evidence audit."""
    _render_hebrew_draft(str(project.get("id") or "irina"))

    metadata = project.get("metadata") or {}
    draft = metadata.get("court_document_draft") or {}
    batches = [item for item in draft.get("batches", []) if isinstance(item, dict)]
    batches.sort(key=lambda item: int(item.get("batch", 0)))

    with st.expander("Court filing draft · preserved text and number audit", expanded=True):
        if not batches:
            st.warning("No court-document batches are stored in this project record.")
            return

        full_text = "\n\n".join(str(item.get("text") or "").strip() for item in batches).strip()
        reconciliation = _load_reconciliation()
        status = str(draft.get("status") or "unknown")

        st.warning(
            "Review workspace only — not legal advice. The submitted Russian wording is preserved; "
            "the audit below flags differences without rewriting the draft."
        )
        left, middle, right = st.columns(3)
        left.metric("Stored batches", len(batches))
        middle.metric("Evidence bullets found", reconciliation.get("evidence_items_found", "—"))
        right.metric("Review flags", len(reconciliation.get("review_flags") or []))
        st.caption(f"Project-table status: {status} · last verified: {reconciliation.get('verified_at', 'unknown')}")

        tab_draft, tab_completeness, tab_numbers = st.tabs(
            ["Russian draft", "Completeness", "Number reconciliation"]
        )

        with tab_draft:
            st.text_area(
                "Verbatim merged draft",
                value=full_text,
                height=850,
                disabled=True,
                key=f"court_draft_{project.get('id')}",
            )
            st.download_button(
                "Download preserved draft (.txt)",
                full_text.encode("utf-8"),
                file_name="Irina_Sirtseva_explanatory_note_ru.txt",
                mime="text/plain; charset=utf-8",
                key=f"court_draft_download_{project.get('id')}",
            )

        with tab_completeness:
            checks = reconciliation.get("completeness_checks") or []
            if checks:
                st.dataframe(checks, width="stretch", hide_index=True)
            else:
                st.warning("Completeness artifact is unavailable.")
            hashes = [
                {"Batch": item.get("batch"), "SHA-256": item.get("sha256"), "Received": item.get("received_at")}
                for item in batches
            ]
            st.markdown("##### Stored-batch integrity")
            st.dataframe(hashes, width="stretch", hide_index=True)

        with tab_numbers:
            st.markdown("##### Internal arithmetic")
            arithmetic = reconciliation.get("internal_arithmetic") or []
            if arithmetic:
                st.dataframe(arithmetic, width="stretch", hide_index=True)

            st.markdown("##### Draft versus extracted evidence")
            comparisons = reconciliation.get("evidence_comparison") or []
            if comparisons:
                display_rows = []
                for row in comparisons:
                    display_rows.append({
                        "Item": row.get("item"),
                        "Draft": _money(row.get("draft_nis")),
                        "Extracted / dashboard": _money(row.get("evidence_nis")),
                        "Difference": _money(row.get("difference_nis")),
                        "Assessment": row.get("assessment"),
                    })
                st.dataframe(display_rows, width="stretch", hide_index=True)

            for flag in reconciliation.get("review_flags") or []:
                st.warning(str(flag))

            st.info(
                "The court draft and dashboard are currently different calculation models. "
                "Do not substitute one total for the other until the listed scope and evidence questions are resolved."
            )
