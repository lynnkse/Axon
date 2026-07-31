"""
Axon Executive Dashboard
Streamlit web app — accessible via Tailscale from any device.
Tabs: Today's Food | Fitness Week | Alive State | File Viewer
"""

import json
import os
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go

# ── Config ────────────────────────────────────────────────────────────────────
_ENV_PATH = Path(__file__).parent.parent / ".env"

def _load_env():
    result = {}
    try:
        with open(_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return result

_env = _load_env()
SUPABASE_URL = os.environ.get("SUPABASE_URL") or _env.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY") or _env.get("SUPABASE_ANON_KEY", "")

DOCS_DIR = Path(__file__).parent.parent  # ~/Axon — serves HTMLs from here


def _sb_get(table: str, params: str = "") -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}?{params}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        st.error(f"Supabase error: {e}")
        return []


# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Axon Dashboard", page_icon="⚡", layout="wide")
st.title("⚡ Axon Dashboard")

tab_food, tab_fitness, tab_alive, tab_files = st.tabs(
    ["🍽 Food Today", "💪 Fitness Week", "🧠 Alive State", "📄 Files"]
)

# ── Tab: Food Today ───────────────────────────────────────────────────────────
with tab_food:
    st.subheader(f"Food — {date.today().isoformat()}")
    today = date.today().isoformat()
    rows = _sb_get("food_entries", f"date=eq.{today}&order=id.asc&select=food_item,portion,calories,protein_g,fat_g,carbs_g,notes")

    if rows:
        total_kcal = sum(r.get("calories", 0) or 0 for r in rows)
        total_p = sum(float(r.get("protein_g") or 0) for r in rows)
        total_f = sum(float(r.get("fat_g") or 0) for r in rows)
        total_c = sum(float(r.get("carbs_g") or 0) for r in rows)
        total_macro_kcal = total_p * 4 + total_f * 9 + total_c * 4 or 1

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total kcal", f"{total_kcal}")
        col2.metric("Protein", f"{total_p:.0f}g ({total_p*4/total_macro_kcal*100:.0f}%)")
        col3.metric("Fat", f"{total_f:.0f}g ({total_f*9/total_macro_kcal*100:.0f}%)")
        col4.metric("Carbs", f"{total_c:.0f}g ({total_c*4/total_macro_kcal*100:.0f}%)")

        # Progress bar toward 2000 kcal target
        st.progress(min(1.0, total_kcal / 2000), text=f"{total_kcal} / 2000 kcal target")

        # Macro pie
        fig = go.Figure(go.Pie(
            labels=["Protein", "Fat", "Carbs"],
            values=[total_p * 4, total_f * 9, total_c * 4],
            hole=0.4,
            marker_colors=["#4CAF50", "#FF9800", "#2196F3"],
        ))
        fig.update_layout(height=250, margin=dict(t=0, b=0, l=0, r=0))
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            [{
                "Food": r.get("food_item", ""),
                "Portion": r.get("portion", ""),
                "kcal": r.get("calories", 0),
                "P(g)": r.get("protein_g", 0),
                "F(g)": r.get("fat_g", 0),
                "C(g)": r.get("carbs_g", 0),
            } for r in rows],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No food logged today yet.")

    if st.button("🔄 Refresh food"):
        st.rerun()

# ── Tab: Fitness Week ─────────────────────────────────────────────────────────
with tab_fitness:
    st.subheader("Fitness — last 7 days")
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    rows = _sb_get("fitness_log", f"date=gte.{week_ago}&order=date.desc&select=date,weight_kg,pushups_sets,pushups_reps,pullups_sets,pullups_reps,pistol_sets,pistol_reps,notes")

    if rows:
        st.dataframe(
            [{
                "Date": r.get("date"),
                "Weight": r.get("weight_kg"),
                "Push-ups": f"{r.get('pushups_sets',0)}×{r.get('pushups_reps',0)}" if r.get("pushups_sets") else "-",
                "Pull-ups": f"{r.get('pullups_sets',0)}×{r.get('pullups_reps',0)}" if r.get("pullups_sets") else "-",
                "Pistols": f"{r.get('pistol_sets',0)}×{r.get('pistol_reps',0)}" if r.get("pistol_sets") else "-",
                "Notes": r.get("notes", ""),
            } for r in rows],
            use_container_width=True,
            hide_index=True,
        )

        weights = [(r["date"], r["weight_kg"]) for r in rows if r.get("weight_kg")]
        if weights:
            dates, wts = zip(*sorted(weights))
            fig = go.Figure(go.Scatter(x=list(dates), y=list(wts), mode="lines+markers", line_color="#4CAF50"))
            fig.update_layout(title="Weight (kg)", height=200, margin=dict(t=30, b=0))
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No fitness data this week.")

    if st.button("🔄 Refresh fitness"):
        st.rerun()

# ── Tab: Alive State ──────────────────────────────────────────────────────────
with tab_alive:
    st.subheader("Live Module State")
    rows = _sb_get("alive_state", "id=eq.1&limit=1")
    if rows:
        s = rows[0]
        col1, col2, col3 = st.columns(3)
        col1.metric("Tick", s.get("tick", 0))
        col1.metric("Mood", s.get("mood_label", "—"))
        col2.metric("Valence", f"{float(s.get('valence', 0)):+.2f}", delta=f"σ={float(s.get('valence_sigma', 0.2)):.2f}")
        col2.metric("Arousal", f"{float(s.get('arousal', 0)):+.2f}", delta=f"σ={float(s.get('arousal_sigma', 0.2)):.2f}")
        col3.metric("Tension", f"{float(s.get('tension', 0)):.2f}")
        col3.metric("Bg Affect", f"{float(s.get('background_affect', 0)):+.2f}")

        if s.get("curiosity_focus"):
            st.info(f"🔍 Curiosity focus: {s['curiosity_focus']}")
        if s.get("personality_note"):
            st.caption(s["personality_note"])

        # 2D circumplex plot
        v = float(s.get("valence", 0))
        a = float(s.get("arousal", 0))
        fig = go.Figure()
        fig.add_shape(type="circle", x0=-1, y0=-1, x1=1, y1=1, line_color="gray", opacity=0.2)
        fig.add_trace(go.Scatter(x=[v], y=[a], mode="markers+text",
                                  marker=dict(size=20, color="#7C4DFF"),
                                  text=["●"], textposition="top center"))
        fig.update_layout(
            title="Valence × Arousal (Russell circumplex)",
            xaxis=dict(range=[-1.1, 1.1], title="Valence (−=negative, +=positive)"),
            yaxis=dict(range=[-1.1, 1.1], title="Arousal (−=calm, +=intense)"),
            height=300, margin=dict(t=40, b=40),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(f"Last updated: {s.get('last_updated', '—')}")
    else:
        st.info("No alive state data.")

    if st.button("🔄 Refresh state"):
        st.rerun()

# ── Tab: File Viewer ──────────────────────────────────────────────────────────
with tab_files:
    st.subheader("File Viewer")
    html_files = sorted(DOCS_DIR.glob("**/*.html"))
    pdf_files = sorted(DOCS_DIR.glob("**/*.pdf"))
    all_files = html_files + pdf_files

    if all_files:
        selected = st.selectbox("Choose file", [str(f.relative_to(DOCS_DIR)) for f in all_files])
        full_path = DOCS_DIR / selected
        if full_path.suffix == ".html":
            content = full_path.read_text(encoding="utf-8", errors="replace")
            st.components.v1.html(content, height=700, scrolling=True)
        elif full_path.suffix == ".pdf":
            data = full_path.read_bytes()
            st.download_button("⬇ Download PDF", data, file_name=full_path.name, mime="application/pdf")
    else:
        st.info("No HTML or PDF files found in ~/Axon/")
