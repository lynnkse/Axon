"""
Axon Executive Dashboard
Streamlit web app — accessible via Tailscale from any device.
Tabs: Today's Food | Fitness Week | Alive State | File Viewer
"""

import json
import os
import html
import urllib.parse
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
# Instance label (2026-08-09): shown in the page title so it's obvious at a
# glance which machine's dashboard you're looking at -- same AXON_INSTANCE
# convention as config.py, with hostname fallback if unset.
import socket as _socket
AXON_INSTANCE = (os.environ.get("AXON_INSTANCE") or _env.get("AXON_INSTANCE", "")
                  or _socket.gethostname()).lower()

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


def _badge(text: str, color: str = "#4A90D9") -> str:
    """Small dashboard badge using the existing instance-label convention."""
    return (f'<span class="axon-badge" style="background:{color};">'
            f'{html.escape(str(text))}</span>')


def _compact_state(state: dict, limit: int = 8) -> list[tuple[str, object]]:
    """Pick scalar actor fields for the memory-block overview."""
    scalars = []
    for key, value in (state or {}).items():
        if value is None or isinstance(value, (dict, list)):
            continue
        scalars.append((key, value))
    return scalars[:limit]


# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(page_title=f"Axon Dashboard — {AXON_INSTANCE}", page_icon="⚡", layout="wide")

st.markdown("""
<style>
/* Remove Streamlit chrome padding */
#root > div:first-child { padding-top: 0 !important; }
.block-container { padding: 0.5rem 1rem 0 1rem !important; max-width: 100% !important; }
header[data-testid="stHeader"] { display: none !important; }
footer { display: none !important; }

/* ── Visual pass ─────────────────────────────────────────────────────────── */
html, body, [class*="css"] { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }

/* Tab bar: bigger touch targets, rounded pills, clearer active state */
.stTabs [data-baseweb="tab-list"] { gap: 6px; flex-wrap: wrap; }
.stTabs [data-baseweb="tab"] {
  padding: 8px 16px; border-radius: 8px 8px 0 0; font-weight: 500;
  min-height: 40px; display: flex; align-items: center;
}
.stTabs [aria-selected="true"] { background: rgba(74,144,217,0.12); font-weight: 700; }

/* Cards: give metrics/containers breathing room and a subtle boundary */
div[data-testid="stMetric"] {
  background: rgba(127,127,127,0.06); border-radius: 10px; padding: 12px 14px;
}
div[data-testid="stExpander"] { border-radius: 10px; }
.axon-badge {
  color: white; padding: 2px 9px; border-radius: 10px; font-size: 0.76em;
  font-weight: 650; display: inline-block; margin-right: 5px;
}
.memory-block {
  background: rgba(127,127,127,0.055); border: 1px solid rgba(127,127,127,0.18);
  border-left: 4px solid #7651A8; border-radius: 10px; padding: 10px 13px;
  margin: 2px 0 10px 0;
}
.project-block { border-left-color: #2E8B70; }
.memory-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.88em; }
.memory-meta { color: #7A8790; font-size: 0.82em; margin-top: 4px; }

/* Buttons: rounder, slightly bigger tap area for touch */
.stButton > button { border-radius: 8px; padding: 0.5rem 1rem; min-height: 42px; }

/* Remove subheader margin in split view */
.split-label { font-size: 0.75rem; color: #888; margin-bottom: 2px; }
/* Make iframes fill their column */
iframe { width: 100% !important; }

/* ── Mobile responsiveness (phones/small tablets) ───────────────────────── */
@media (max-width: 640px) {
  .block-container { padding: 0.4rem 0.6rem 0 0.6rem !important; }
  /* Streamlit column layouts wrap by default via flex-wrap below; this just
     tightens spacing so wrapped columns don't look sparse */
  div[data-testid="column"] { min-width: 100% !important; }
  div[data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .stTabs [data-baseweb="tab"] { padding: 8px 10px; font-size: 0.85rem; }
  h1, h2, h3 { font-size: 1.1rem !important; }
  h4, h5 { font-size: 1rem !important; }
  /* Graphviz/plotly charts: allow horizontal scroll instead of illegible squish */
  div[data-testid="stGraphVizChart"], .js-plotly-plot {
    overflow-x: auto !important; max-width: 100% !important;
  }
}
</style>
""", unsafe_allow_html=True)

_instance_color = {"rog": "#4A90D9", "aevadim09": "#D97A4A"}.get(AXON_INSTANCE, "#888")
st.markdown(
    f"##### ⚡ Axon Dashboard &nbsp; "
    f'<span style="background:{_instance_color}; color:white; padding:2px 10px; '
    f'border-radius:10px; font-size:0.7em; vertical-align:middle;">{AXON_INSTANCE.upper()}</span>',
    unsafe_allow_html=True,
)

tab_food, tab_fitness, tab_alive, tab_actors, tab_projects, tab_architecture, tab_files, tab_split, tab_manim = st.tabs(
    ["🍽 Food Today", "💪 Fitness Week", "🧠 Alive State", "🎭 Actors", "🧭 Projects", "🗺 Architecture", "📄 Files", "⚡ Split View", "🎬 Manim"]
)

MANIM_OUTPUT = Path.home() / "Axon/manim/output"
MANIM_SCENES = Path.home() / "Axon/manim/scenes"
MANIM_VIDEOS = Path.home() / "manim_videos"


def _render_file(full_path: Path, height: int = 700):
    """Render a single file inline."""
    if full_path.suffix == ".html":
        st.components.v1.html(full_path.read_text(encoding="utf-8", errors="replace"), height=height, scrolling=True)
    elif full_path.suffix == ".pdf":
        st.download_button("⬇ Download PDF", full_path.read_bytes(), file_name=full_path.name, mime="application/pdf")
    elif full_path.suffix in (".mp4", ".webm", ".mov"):
        st.video(str(full_path))

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

# ── Tab: Actors ───────────────────────────────────────────────────────────────
with tab_actors:
    st.subheader("Actor Memory Blocks")
    st.caption("Each block is one persistent actor thread. Open it to inspect current memory and its complete assigned event stream.")
    actors = _sb_get(
        "actor_state",
        f"instance=eq.{urllib.parse.quote(AXON_INSTANCE)}&order=actor_id.asc"
        "&select=actor_id,actor_type,disposition,revision,state,directory_projection,last_advanced_at",
    )
    if actors:
        disposition_colors = {
            "ready_again": "#2E8B70", "waiting_for_event": "#4A90D9",
            "waiting_for_human": "#D99A2B", "blocked": "#C94F4F",
            "completed": "#6B7A8F", "dormant": "#88739E",
        }
        for actor in actors:
            actor_id = actor.get("actor_id", "unknown")
            actor_type = actor.get("actor_type", "unknown")
            disposition = actor.get("disposition", "unknown")
            revision = actor.get("revision", 0)
            summary = (actor.get("directory_projection") or {}).get("summary", "")
            label = f"▦  {actor_type}  ·  {actor_id}  ·  {disposition}  ·  rev {revision}"
            with st.expander(label, expanded=False):
                badges = (_badge(disposition, disposition_colors.get(disposition, "#6B7A8F"))
                          + _badge(f"revision {revision}", "#7651A8")
                          + _badge(actor_type, "#2E6F9E"))
                st.markdown(
                    f'<div class="memory-block">{badges}'
                    f'<div class="memory-id">{html.escape(actor_id)}</div>'
                    f'<div>{html.escape(summary or "No directory summary")}</div>'
                    f'<div class="memory-meta">Last advanced: '
                    f'{html.escape(str(actor.get("last_advanced_at") or "never"))}</div></div>',
                    unsafe_allow_html=True,
                )

                state = actor.get("state") or {}
                key_fields = _compact_state(state)
                if key_fields:
                    columns = st.columns(min(4, len(key_fields)))
                    for index, (key, value) in enumerate(key_fields):
                        display = f"{value:.3f}" if isinstance(value, float) else str(value)
                        columns[index % len(columns)].metric(key.replace("_", " ").title(), display)

                with st.expander("Current typed state (full JSON)", expanded=False):
                    st.json(state)

                assignment_filter = urllib.parse.quote(
                    json.dumps([{"actor_id": actor_id}], separators=(",", ":")), safe=""
                )
                history = _sb_get(
                    "events",
                    f"assignments=cs.{assignment_filter}&order=sequence.asc"
                    "&select=sequence,event_type,occurred_at,recorded_at,source_actor_id,source_kind,payload,provenance,assignments",
                )
                st.markdown(f"##### Event history · {len(history)} event{'s' if len(history) != 1 else ''}")
                if history:
                    st.dataframe(history, use_container_width=True, hide_index=True, height=340)
                else:
                    st.caption("No assigned journal events yet.")
    else:
        st.info("No actor rows for this instance. Apply the migration and run the backfill first.")

    if st.button("🔄 Refresh actors"):
        st.rerun()

# ── Tab: Ongoing Projects ─────────────────────────────────────────────────────
with tab_projects:
    st.subheader("Ongoing Projects")
    st.caption("Anton's current work areas as cognitive threads. Open a project to inspect its recent accumulated insight history.")
    projects = _sb_get(
        "projects",
        "order=status.asc,name.asc&select=id,name,status,domain,description,started_at",
    )
    if projects:
        status_colors = {
            "active": "#2E8B70", "ongoing": "#2E8B70", "paused": "#D99A2B",
            "completed": "#6B7A8F", "archived": "#88739E",
        }
        for project in projects:
            project_id = project.get("id")
            name = project.get("name") or "Unnamed project"
            status = project.get("status") or "unknown"
            domain = project.get("domain") or "general"
            with st.expander(f"◫  {name}  ·  {domain}  ·  {status}", expanded=False):
                badges = (_badge(status, status_colors.get(status.lower(), "#6B7A8F"))
                          + _badge(domain, "#2E6F9E"))
                st.markdown(
                    f'<div class="memory-block project-block">{badges}'
                    f'<div><strong>{html.escape(name)}</strong></div>'
                    f'<div>{html.escape(project.get("description") or "No description")}</div>'
                    f'<div class="memory-meta">Started: '
                    f'{html.escape(str(project.get("started_at") or "unknown"))} · '
                    f'<span class="memory-id">{html.escape(str(project_id))}</span></div></div>',
                    unsafe_allow_html=True,
                )

                insights = _sb_get(
                    "insights",
                    f"project_id=eq.{urllib.parse.quote(str(project_id), safe='')}&order=created_at.desc&limit=50"
                    "&select=id,type,content,confidence,source,context,created_at",
                )
                st.markdown(f"##### Recent history · {len(insights)} insight{'s' if len(insights) != 1 else ''}")
                if insights:
                    for insight in insights:
                        insight_type = insight.get("type") or "insight"
                        confidence = insight.get("confidence")
                        heading = f"{insight_type} · {insight.get('created_at') or 'unknown date'}"
                        if confidence is not None:
                            heading += f" · confidence {confidence}"
                        st.markdown(f"**{heading}**")
                        st.write(insight.get("content") or "")
                        if insight.get("context"):
                            st.caption(f"Context: {insight['context']}")
                        st.markdown("---")
                else:
                    st.caption("No linked insights yet.")
    else:
        st.info("No projects found.")

    if st.button("🔄 Refresh projects"):
        st.rerun()

# ── Tab: Architecture ────────────────────────────────────────────────────────
# Diagrams drafted by Codex (GPT-5.6 Sol) against the real source on aevadim-09,
# 2026-08-11, then implemented here on ROG rather than letting Codex write the
# dashboard file directly -- keeps the actual write path on this side per the
# Reasoner/Executor split worked out earlier this session.
with tab_architecture:
    st.subheader("Axon Architecture")
    st.caption("Solid blue/orange views are deployed today; the dashed purple actor model is a proposed design.")

    view_current, view_actor, view_prompt, view_affect = st.tabs(
        ["Current system", "Actor-model design", "Prompt composition", "Affective loop"]
    )

    with view_current:
        st.markdown("#### Current deployed process and data flow")
        st.caption(
            "SessionManager owns the one persistent Claude Code PTY and serializes all conversational "
            "and reflection work. Supabase is shared persistence; the dashboard and curator use REST directly. "
            "RALPH is being superseded by the actor model (see next tab) — removed here as settled design; "
            "ralph_node.py itself hasn't been deleted from disk."
        )
        st.graphviz_chart(r'''
digraph current_axon {
  graph [rankdir=TB, bgcolor="transparent", pad=0.3, nodesep=0.4, ranksep=0.45,
         fontname="Arial", label="DEPLOYED TODAY — MODULES, TOP TO BOTTOM", labelloc=t, fontsize=20, fontcolor="#2E6F9E"];
  node [shape=plain, fontname="Arial"];
  edge [color="#2E6F9E", penwidth=3.5, arrowsize=1.3, fontname="Arial Bold", fontsize=11, fontcolor="#2E6F9E"];

  anton [label=<
    <TABLE BORDER="2" CELLBORDER="0" CELLSPACING="0" CELLPADDING="7" COLOR="#D99A2B" BGCOLOR="#FFF4D6">
      <TR><TD><FONT COLOR="#5A4109"><B>Anton</B></FONT></TD></TR>
    </TABLE>
  >];

  interfaces [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#D99A2B" BGCOLOR="#FFFBF0">
      <TR><TD BGCOLOR="#D99A2B" COLSPAN="1"><FONT COLOR="white"><B>Interfaces</B></FONT></TD></TR>
      <TR><TD>telegram_node.py &nbsp;— Telegram gateway</TD></TR>
      <TR><TD>cli_node.py &nbsp;— terminal client</TD></TR>
    </TABLE>
  >];

  hub [label=<
    <TABLE BORDER="3" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#2E6F9E" BGCOLOR="#F2F8FF">
      <TR><TD BGCOLOR="#2E6F9E" COLSPAN="1"><FONT COLOR="white"><B>Core relay</B></FONT></TD></TR>
      <TR><TD BGCOLOR="#D7ECFF"><B>session_manager.py</B> &nbsp;— hub, serialized input queue, PTY owner, response parser</TD></TR>
      <TR><TD>Claude Code &nbsp;— persistent interactive process</TD></TR>
      <TR><TD>Claude session JSONL &nbsp;— clean-response detection</TD></TR>
    </TABLE>
  >];

  background [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#6B7A8F" BGCOLOR="#F5F7F9">
      <TR><TD BGCOLOR="#6B7A8F" COLSPAN="1"><FONT COLOR="white"><B>Background nodes</B></FONT></TD></TR>
      <TR><TD>curator.py &nbsp;— daily rules / insights maintenance</TD></TR>
      <TR><TD>dashboard/app.py &nbsp;— Streamlit + ttyd views</TD></TR>
    </TABLE>
  >];

  persistence [label=<
    <TABLE BORDER="3" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#2E8B70" BGCOLOR="#EDFBF5">
      <TR><TD BGCOLOR="#2E8B70" COLSPAN="1"><FONT COLOR="white"><B>Persistence</B></FONT></TD></TR>
      <TR><TD><B>supabase_client.py</B> &nbsp;— REST / search / tag persistence</TD></TR>
      <TR><TD BGCOLOR="#DCF4E8">Supabase &nbsp;— messages, memory, goals, insights, dreams, rules,<BR/>alive_state + anton_model (per instance), fitness_log, food_entries, personal_tasks</TD></TR>
    </TABLE>
  >];

  anton -> interfaces [label="messages / keyboard"];
  interfaces -> anton [label="replies / display"];
  interfaces -> hub [label="input sockets"];
  hub -> interfaces [label="response sockets"];
  hub -> persistence [label="fetch context / save state"];
  background -> persistence [label="maintain / read"];
  background -> interfaces [style=dashed, label="dashboard ttyd iframe"];
}
''', use_container_width=True)

    with view_actor:
        st.markdown("#### Proposed persistent actor-model execution")
        st.caption(
            "Not implemented. Each stage is a record (fields stacked like an OS process control block) — "
            "a few big arrows show how an actor moves through one tick, and separately, how a response gets composed."
        )
        st.graphviz_chart(r'''
digraph actor_pcb {
  graph [rankdir=TB, bgcolor="transparent", pad=0.3, nodesep=0.6, ranksep=0.5,
         fontname="Arial", label="DESIGN — NOT YET BUILT  (one actor's tick, left · a response, right)", labelloc=t, fontsize=18, fontcolor="#7651A8"];
  node [shape=plain, fontname="Arial"];

  record [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#7651A8" BGCOLOR="#FAF7FE">
      <TR><TD BGCOLOR="#7651A8" COLSPAN="2"><FONT COLOR="white"><B>ACTOR RECORD  (like a PCB)</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT">actor_id / type</TD><TD ALIGN="LEFT">physical-state · ANPLOS · Axon-improve</TD></TR>
      <TR><TD ALIGN="LEFT">revision</TD><TD ALIGN="LEFT">N  (optimistic concurrency)</TD></TR>
      <TR><TD ALIGN="LEFT">state</TD><TD ALIGN="LEFT">typed fold, isolated per actor</TD></TR>
      <TR><TD ALIGN="LEFT">eligibility</TD><TD ALIGN="LEFT">dirty · unblocked · cooldown</TD></TR>
      <TR><TD ALIGN="LEFT">priority</TD><TD ALIGN="LEFT">scheduling class · niceness</TD></TR>
      <TR><TD ALIGN="LEFT">last_advanced_at</TD><TD ALIGN="LEFT">timestamp</TD></TR>
    </TABLE>
  >];

  sched [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#7651A8" BGCOLOR="#FAF7FE">
      <TR><TD BGCOLOR="#7651A8" COLSPAN="2"><FONT COLOR="white"><B>SCHEDULER DECISION</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT">eligible set</TD><TD ALIGN="LEFT">only actors past their gate</TD></TR>
      <TR><TD ALIGN="LEFT">ordering</TD><TD ALIGN="LEFT">virtual deadline · service debt</TD></TR>
      <TR><TD ALIGN="LEFT">budget</TD><TD ALIGN="LEFT">bounded quantum this pass</TD></TR>
      <TR><TD ALIGN="LEFT">selected?</TD><TD ALIGN="LEFT">yes → runs one tick</TD></TR>
    </TABLE>
  >];

  tick [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#7651A8" BGCOLOR="#FAF7FE">
      <TR><TD BGCOLOR="#7651A8" COLSPAN="2"><FONT COLOR="white"><B>ONE BOUNDED TICK</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT">input</TD><TD ALIGN="LEFT">assigned events + state at rev N</TD></TR>
      <TR><TD ALIGN="LEFT">work</TD><TD ALIGN="LEFT">deterministic, or one LLM call</TD></TR>
      <TR><TD ALIGN="LEFT">output</TD><TD ALIGN="LEFT">validated state patch</TD></TR>
      <TR><TD ALIGN="LEFT">commit</TD><TD ALIGN="LEFT">rev N→N+1, else retry</TD></TR>
    </TABLE>
  >];

  directory [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#D99A2B" BGCOLOR="#FFFBF0">
      <TR><TD BGCOLOR="#D99A2B" COLSPAN="2"><FONT COLOR="white"><B>ACTOR DIRECTORY  (compact)</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT">physical-state</TD><TD ALIGN="LEFT">active, energy-low, 2h ago</TD></TR>
      <TR><TD ALIGN="LEFT">anplos</TD><TD ALIGN="LEFT">paused until Friday</TD></TR>
      <TR><TD ALIGN="LEFT">commitments</TD><TD ALIGN="LEFT">3 due, 1 escalating</TD></TR>
    </TABLE>
  >];

  response [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="6" COLOR="#2E6F9E" BGCOLOR="#F2F8FF">
      <TR><TD BGCOLOR="#2E6F9E" COLSPAN="2"><FONT COLOR="white"><B>RESPONSE  (when Anton messages)</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT">reads</TD><TD ALIGN="LEFT">directory + eligible obligations</TD></TR>
      <TR><TD ALIGN="LEFT">never</TD><TD ALIGN="LEFT">waits on a background tick</TD></TR>
      <TR><TD ALIGN="LEFT">produces</TD><TD ALIGN="LEFT">one coherent reply</TD></TR>
    </TABLE>
  >];

  record -> sched [penwidth=3, color="#7651A8", arrowsize=1.2, label="eligible", fontname="Arial Bold", fontsize=11];
  sched -> tick [penwidth=3, color="#7651A8", arrowsize=1.2, label="selected", fontname="Arial Bold", fontsize=11];
  tick -> record [penwidth=3, color="#7651A8", arrowsize=1.2, label="new revision", fontname="Arial Bold", fontsize=11, style=dashed];
  record -> directory [penwidth=3, color="#D99A2B", arrowsize=1.2, label="projected", fontname="Arial Bold", fontsize=11];
  directory -> response [penwidth=3, color="#2E6F9E", arrowsize=1.2, label="read-only", fontname="Arial Bold", fontsize=11];
}
''', use_container_width=True)

    with view_prompt:
        st.markdown("#### Literal prompt composition order")
        st.caption(
            "The append-system-prompt layer is built once when Claude starts or resumes. The lower chain is rebuilt "
            "for every queued item; semantic dreams are fetched only for source=telegram."
        )
        st.graphviz_chart(r'''
digraph prompt_composition {
  graph [rankdir=LR, bgcolor="transparent", pad=0.25, nodesep=0.5, ranksep=0.6,
         fontname="Arial", label="MODEL-VISIBLE CONTEXT — OUTER TO INNER", labelloc=t, fontsize=17];
  node [shape=plain, fontname="Arial"];
  edge [color="#667A89", arrowsize=0.7, fontname="Arial", fontsize=9];

  spawn [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="5" COLOR="#D97A4A" BGCOLOR="#FFF8F2">
      <TR><TD BGCOLOR="#D97A4A" COLSPAN="1"><FONT COLOR="white"><B>A. Spawn time only  (--append-system-prompt)</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT">1&nbsp; User name + timezone</TD></TR>
      <TR><TD ALIGN="LEFT">2&nbsp; profile.md</TD></TR>
      <TR><TD ALIGN="LEFT">3&nbsp; Initial alive-state block + tag protocol</TD></TR>
      <TR><TD ALIGN="LEFT">4&nbsp; Active permanent skills index</TD></TR>
      <TR><TD ALIGN="LEFT">5&nbsp; Memory context (memory + goals)</TD></TR>
      <TR><TD ALIGN="LEFT">6&nbsp; Recent messages (last 20)</TD></TR>
      <TR><TD ALIGN="LEFT">7&nbsp; Tag-output instructions</TD></TR>
    </TABLE>
  >];

  turn [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="5" COLOR="#2E8B70" BGCOLOR="#F2FBF7">
      <TR><TD BGCOLOR="#2E8B70"><FONT COLOR="white"><B>B. Rebuilt every queued item</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT">
        <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4" COLOR="#7651A8" BGCOLOR="#F5F0FC">
          <TR><TD BGCOLOR="#7651A8" COLSPAN="2"><FONT COLOR="white"><B>ACTOR DIRECTORY  (today: 4 separate lines · design: 1 unified row)</B></FONT></TD></TR>
          <TR><TD ALIGN="LEFT">Axon actor</TD><TD ALIGN="LEFT">valence history, the "soul" — [ALIVE ...]</TD></TR>
          <TR><TD ALIGN="LEFT">Anton actor</TD><TD ALIGN="LEFT">mostly empty for now, running but undefined — [ANTON-MODEL ...]</TD></TR>
          <TR><TD ALIGN="LEFT">Reflection actor</TD><TD ALIGN="LEFT">idle-time output, was called "dreams" — semantic recall</TD></TR>
          <TR><TD ALIGN="LEFT"><FONT COLOR="#8A7A9C">… other actors</FONT></TD><TD ALIGN="LEFT"><FONT COLOR="#8A7A9C">ANPLOS-improvement, commitments, etc. — not fixed to these 3</FONT></TD></TR>
          <TR><TD ALIGN="LEFT" BGCOLOR="#EDE5F7">Directives</TD><TD ALIGN="LEFT" BGCOLOR="#EDE5F7">not an actor — imperative, computed from actor state each time</TD></TR>
        </TABLE>
      </TD></TR>
      <TR><TD ALIGN="LEFT">Permanent unnamed protocols — small critical rules, always in full</TD></TR>
      <TR><TD ALIGN="LEFT">Keyword-matched rule anchors</TD></TR>
      <TR><TD ALIGN="LEFT" BGCOLOR="#FFF4D6">Original queued item text</TD></TR>
    </TABLE>
  >];

  model [label="Claude Code\ncontext", shape=component, style=filled, fillcolor="#FFF1E8", color="#D97A4A", fontname="Arial", fontsize=11];

  spawn -> turn [style=dashed, label="same persistent session"];
  turn -> model [label="written to PTY"];
}
''', use_container_width=True)

    with view_affect:
        st.markdown("#### Alive state, Anton model, and reflection loop")
        st.caption(
            "Axon's affect uses delta-style Kalman updates plus per-message mean reversion. Anton's separate "
            "absolute-level filter accepts explicit and inferred observations, but only explicit observations move his baseline."
        )
        st.graphviz_chart(r'''
digraph affective_loop {
  graph [rankdir=LR, bgcolor="transparent", pad=0.3, nodesep=0.3, ranksep=0.85, compound=true,
         fontname="Arial", label="DEPLOYED AFFECTIVE LOOP", labelloc=t, fontsize=20, fontcolor="#2E6F9E"];
  node [shape=box, style="rounded,filled", fontname="Arial", fontsize=9.5, color="#4A90D9", fillcolor="#EDF6FF"];
  edge [fontname="Arial", fontsize=8.5, color="#5A7180", fontcolor="#455D6B", arrowsize=0.7];

  msg [label="Every queued message\ntick += 1; σ += .01", shape=oval, penwidth=2];

  subgraph cluster_axon_state {
    label="Axon's own alive-state filter"; style="rounded,filled"; color="#2E6F9E"; fillcolor="#F2F8FF"; fontname="Arial Bold"; fontsize=12;
    homeo [label="Homeostasis before inference\nV += .02(.15 − V)\nA += .02(0 − A)\ntension −=.01; bg ×=.98"];
    self [label="Self-report tags\n[VALENCE Δ] [AROUSAL Δ]\nΔ clamped ±.15, σ=.12"];
    done [label="Exteroception: [DONE]\nΔV=.06 each, capped .15, σ=.15"];
    empathy [label="Explicit Anton valence\nΔV=.3 × observation, σ=.18"];
    alive [label="Alive-state filter\nV,σV • A,σA • mood • curiosity • tension", shape=cylinder, penwidth=2.5, fillcolor="#D7ECFF"];
    directives [label="Deterministic directive table\nmax 2 per message"];
    prefix [label="Fresh [ALIVE] + [DIRECTIVE]\nmessage prefix"];
    alive_store [label="alive_state (Supabase)\nper instance", shape=cylinder, fillcolor="#EAF8F2", color="#2E8B70"];
  }

  subgraph cluster_anton_model {
    label="Persistent model of Anton"; style="rounded,filled"; color="#C47A28"; fillcolor="#FFFBF3"; fontname="Arial Bold"; fontsize=12;
    anton_tag [label="[ANTON_STATE]\nV / energy / mode\nexplicit flag + evidence"];
    provenance [label="Obs. noise by provenance\nexplicit σ=.10 • inferred σ=.20"];
    anton_predict [label="Prediction step\nσ drifts with elapsed hours\ntoward learned baseline"];
    anton_filter [label="Anton Kalman filter\nV,σV • energy,σE", shape=cylinder, penwidth=2.5, fillcolor="#FFEFD8"];
    baseline [label="Baseline EMA α=.03\nGATE: explicit=true only", fillcolor="#FFF5E8", color="#C47A28"];
    divergence [label="Sustained divergence\nV < baseline − .25 for >24h\n48h cooldown"];
    anton_store [label="anton_model + anton_state_log\n(Supabase, per instance)", shape=cylinder, fillcolor="#FFF0DC", color="#C47A28"];
  }

  subgraph cluster_reflection {
    label="Reflection loop"; style="rounded,filled"; color="#7651A8"; fillcolor="#FAF7FE"; fontname="Arial Bold"; fontsize=12;
    clock [label="Reflection ticker\ncheck every 10m\nidle ≥2h • last ≥20h ago\nAXON_REFLECTION enabled"];
    reflection [label="source=reflection\nnormal manager + Claude pipeline"];
    outputs [label="Parsed output\n[DREAM] → dreams\n[INSIGHT] → insights", shape=folder, fillcolor="#F0E6FA", color="#7651A8"];
    sink [label="Telegram sink\nno direct chat reply", fillcolor="#F4F4F4", color="#777777"];
  }

  msg -> homeo -> alive;
  self -> alive;
  done -> alive;
  empathy -> alive;
  alive -> alive_store [label="async upsert"];
  alive -> directives -> prefix;

  anton_tag -> provenance -> anton_predict -> anton_filter;
  anton_tag -> baseline [label="if explicit"];
  baseline -> anton_filter [label="baseline"];
  anton_filter -> divergence;
  divergence -> directives [label="check-in", ltail=cluster_anton_model, lhead=cluster_axon_state];
  anton_filter -> anton_store;
  anton_tag -> anton_store [label="observation"];
  anton_tag -> empathy [label="explicit V only", ltail=cluster_anton_model, lhead=cluster_axon_state];
  anton_filter -> prefix [label="[ANTON-MODEL]", ltail=cluster_anton_model, lhead=cluster_axon_state];

  anton_store -> clock [label="last_reflection_at", ltail=cluster_anton_model, lhead=cluster_reflection];
  clock -> reflection [label="stamp before enqueue"];
  reflection -> self [label="optional affect tags", ltail=cluster_reflection, lhead=cluster_axon_state];
  reflection -> outputs;
  outputs -> sink;
  outputs -> anton_store [style=dashed, label="analyzes model/log", ltail=cluster_reflection, lhead=cluster_anton_model];
}
''', use_container_width=True)

        st.markdown("---")
        st.markdown("#### Target: the same three, as actors")
        st.caption(
            "Design, not built. Same behaviors, but every cross-actor influence becomes an explicit typed "
            "event instead of one filter directly calling into another — this is what makes any one of the "
            "three independently redesignable later."
        )
        st.graphviz_chart(r'''
digraph affective_actors_target {
  graph [rankdir=LR, bgcolor="transparent", pad=0.3, nodesep=0.6, ranksep=0.9,
         fontname="Arial", label="DESIGN — NOT YET BUILT", labelloc=t, fontsize=18, fontcolor="#7651A8"];
  node [shape=box, style="rounded,filled,dashed", fontname="Arial", fontsize=10,
        color="#7651A8", fillcolor="#F5F0FC", fontcolor="#2D2140"];
  edge [fontname="Arial", fontsize=9, color="#7651A8", fontcolor="#5C427E", arrowsize=0.9];

  axon_actor [label="Axon actor\nvalence · mood · tension\nisolated fold, own revision", penwidth=2];
  anton_actor [label="Anton actor\nvalence/energy model\nisolated fold, own revision", penwidth=2];
  reflection_actor [label="Reflection actor\nidle-time analysis\nisolated fold, own revision", penwidth=2];

  anton_actor -> axon_actor [penwidth=2.5, label="event: high_confidence_valence_report\n(only if explicit — same gate as today)"];
  reflection_actor -> axon_actor [penwidth=2.5, label="event: affect_tag_emitted\n(optional)"];
  anton_actor -> reflection_actor [penwidth=2.5, style=dashed, label="read: state + log, for analysis"];
  reflection_actor -> anton_actor [penwidth=2.5, label="event: insight_emitted"];
}
''', use_container_width=True)

# ── Tab: File Viewer ──────────────────────────────────────────────────────────
with tab_files:
    st.subheader("File Viewer")
    all_files = (
        sorted(DOCS_DIR.glob("**/*.html")) +
        sorted(DOCS_DIR.glob("**/*.pdf")) +
        sorted(MANIM_OUTPUT.glob("*.mp4"))
    )
    if all_files:
        def _label(f):
            try:
                return str(f.relative_to(DOCS_DIR))
            except ValueError:
                return f"manim/{f.name}"
        labels = [_label(f) for f in all_files]
        selected = st.selectbox("Choose file", labels)
        _render_file(all_files[labels.index(selected)])
    else:
        st.info("No files found in ~/Axon/")

# ── Tab: Split View ───────────────────────────────────────────────────────────
with tab_split:
    # Multi-machine fix (2026-08-09): this was hardcoded to ROG's Tailscale IP,
    # so opening the dashboard on ANY machine embedded ROG's ttyd streams --
    # the outer Streamlit page was local, but the CLI/RALPH terminal iframes
    # silently pointed cross-machine at ROG regardless of which instance served
    # the page. Detect the local machine's own IP instead, same pattern as
    # start_axon.sh's TAILSCALE_IP detection.
    import subprocess as _subprocess
    try:
        TAILSCALE_IP = _subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
        ).stdout.strip() or "127.0.0.1"
    except Exception:
        TAILSCALE_IP = "127.0.0.1"
    PANE_H = 820

    # Left pane selector
    LEFT_OPTIONS = {"CLI (manager/cli)": 7681, "RALPH": 7682}
    RIGHT_OPTIONS = {"RALPH": 7682, "CLI (manager/cli)": 7681}

    ctrl_l, ctrl_r = st.columns(2, gap="small")
    with ctrl_l:
        left_choice = st.selectbox("Left pane", list(LEFT_OPTIONS.keys()), key="split_left", label_visibility="collapsed")
    with ctrl_r:
        right_choice = st.selectbox("Right pane", list(RIGHT_OPTIONS.keys()), key="split_right", label_visibility="collapsed")

    col_cli, col_viewer = st.columns(2, gap="small")

    with col_cli:
        st.markdown(f'<p class="split-label">{left_choice}</p>', unsafe_allow_html=True)
        st.components.v1.html(
            f'<iframe src="http://{TAILSCALE_IP}:{LEFT_OPTIONS[left_choice]}" '
            f'style="width:100%;height:{PANE_H}px;border:none;" '
            f'allow="clipboard-write; clipboard-read"></iframe>',
            height=PANE_H,
            scrolling=False,
        )

    with col_viewer:
        st.markdown(f'<p class="split-label">{right_choice}</p>', unsafe_allow_html=True)
        st.components.v1.html(
            f'<iframe src="http://{TAILSCALE_IP}:{RIGHT_OPTIONS[right_choice]}" '
            f'style="width:100%;height:{PANE_H}px;border:none;" '
            f'allow="clipboard-write; clipboard-read"></iframe>',
            height=PANE_H,
            scrolling=False,
        )

# ── Tab: Manim ────────────────────────────────────────────────────────────────
with tab_manim:
    st.subheader("🎬 Manim — Math Visualizations")

    col_left, col_right = st.columns([1, 2])

    with col_left:
        st.markdown("**Rendered clips**")
        mp4s = sorted(MANIM_OUTPUT.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
        if mp4s:
            chosen = st.radio("", [f.stem for f in mp4s], key="manim_pick", label_visibility="collapsed")
            chosen_path = MANIM_OUTPUT / f"{chosen}.mp4"
        else:
            st.info("No renders yet.")
            chosen_path = None

        st.markdown("---")
        st.markdown("**3b1b scene library**")
        year_dirs = sorted([d for d in MANIM_VIDEOS.iterdir() if d.is_dir() and d.name.startswith("_")], reverse=True) if MANIM_VIDEOS.exists() else []
        if year_dirs:
            year = st.selectbox("Year", [d.name for d in year_dirs], key="manim_year")
            topic_dirs = sorted((MANIM_VIDEOS / year).iterdir())
            topic = st.selectbox("Topic", [d.name for d in topic_dirs], key="manim_topic")
            scene_files = sorted((MANIM_VIDEOS / year / topic).glob("*.py"))
            if scene_files:
                scene_file = st.selectbox("File", [f.name for f in scene_files], key="manim_file")
                st.code(f"render.sh {year}/{topic}/{scene_file} <SceneName>", language="bash")
            else:
                st.info("No .py files in this topic.")
        else:
            st.info("3b1b videos repo not found at ~/manim_videos")

    with col_right:
        if chosen_path and chosen_path.exists():
            st.markdown(f"**{chosen_path.stem}**")
            st.video(str(chosen_path))
        else:
            st.info("Select a clip on the left to play it here.")
