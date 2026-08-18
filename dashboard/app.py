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
from datetime import date, datetime, timedelta
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
# service_role bypasses RLS -- required since food_entries/fitness_log/
# compulsive_behavior_tracking only grant SELECT to authenticated/service_role,
# not anon. Falls back to the anon key if service_role isn't set yet.
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or _env.get("SUPABASE_SERVICE_ROLE_KEY", "")
    or os.environ.get("SUPABASE_ANON_KEY") or _env.get("SUPABASE_ANON_KEY", "")
)
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

/* ── Axon design system ─────────────────────────────────────────────────── */
:root {
  --axon-bg: #0b0e14;
  --axon-surface: rgba(18,23,32,0.94);
  --axon-surface-raised: #151b25;
  --axon-text: #edf2fa;
  --axon-muted: #a1adbd;
  --axon-faint: #748094;
  --axon-line: rgba(169,184,207,0.12);
  --axon-line-strong: rgba(169,184,207,0.22);
  --axon-accent: #8b8cff;
  --axon-accent-bright: #a9aaff;
  --axon-accent-soft: rgba(139,140,255,0.12);
  --axon-cyan: #64d8ec;
  --axon-green: #4dd4a7;
  --axon-radius: 12px;
  --axon-shadow: 0 1px 0 rgba(255,255,255,.025) inset, 0 12px 36px rgba(0,0,0,.18);
}

html, body, [class*="css"], .stApp {
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--axon-text);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
.stApp { background: var(--axon-bg); }
[data-testid="stAppViewContainer"] {
  background:
    radial-gradient(circle at 12% -10%, rgba(98,96,255,.105), transparent 32rem),
    radial-gradient(circle at 92% 8%, rgba(55,188,218,.055), transparent 30rem),
    var(--axon-bg);
}
[data-testid="stDecoration"], [data-testid="stToolbar"], #MainMenu { display: none !important; }
.block-container {
  max-width: 1440px !important;
  padding: 1.25rem 2rem 3.5rem !important;
}

/* Brand masthead */
.axon-masthead {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  min-height: 56px; margin: 0 0 8px; padding: 2px 2px 10px;
}
.axon-wordmark { display: flex; align-items: center; gap: 11px; }
.axon-mark {
  width: 36px; height: 36px; display: grid; place-items: center;
  color: #fff; background: linear-gradient(145deg, #8b8cff, #5557d8);
  border: 1px solid rgba(196,197,255,.35); border-radius: 10px;
  box-shadow: 0 0 22px rgba(117,118,255,.30), 0 6px 16px rgba(0,0,0,.28);
  font-size: 17px;
}
.axon-title { font-size: 19px; line-height: 1.15; font-weight: 760; letter-spacing: -0.025em; }
.axon-kicker {
  color: var(--axon-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; margin-top: 3px; letter-spacing: .035em;
}
.axon-instance {
  display: inline-flex; align-items: center; gap: 8px; padding: 7px 11px;
  color: #c6cfdb; background: rgba(20,26,36,.85); border: 1px solid var(--axon-line-strong);
  border-radius: 999px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px; font-weight: 750; letter-spacing: .09em;
  box-shadow: 0 0 18px rgba(100,216,236,.055);
}
.axon-instance-dot { width: 7px; height: 7px; border-radius: 50%; box-shadow: 0 0 0 3px rgba(77,212,167,.10), 0 0 10px currentColor; }

/* Navigation: quiet segmented control, including nested architecture tabs */
.stTabs [data-baseweb="tab-list"] {
  width: fit-content; max-width: 100%; gap: 3px; padding: 4px;
  background: rgba(14,18,26,.82); border: 1px solid var(--axon-line);
  border-radius: 11px; box-shadow: 0 8px 28px rgba(0,0,0,.16), inset 0 1px rgba(255,255,255,.018);
  overflow-x: auto; scrollbar-width: none;
}
.stTabs [data-baseweb="tab-list"]::-webkit-scrollbar { display: none; }
.stTabs [data-baseweb="tab"] {
  min-height: 39px; padding: 8px 14px; border-radius: 8px;
  color: var(--axon-muted); font-size: 14px; font-weight: 560;
  letter-spacing: -0.005em; white-space: nowrap;
  transition: color .16s ease, background-color .16s ease, box-shadow .16s ease, transform .16s ease;
}
.stTabs [data-baseweb="tab"]:hover { color: var(--axon-text); background: rgba(139,140,255,.075); }
.stTabs [aria-selected="true"] {
  color: #fff !important; background: rgba(139,140,255,.14) !important; font-weight: 700;
  box-shadow: inset 0 0 0 1px rgba(169,170,255,.20), 0 0 18px rgba(116,117,255,.09);
}
.stTabs [data-baseweb="tab-highlight"] { display: none; }
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.75rem; }

/* Type scale */
h1, h2, h3, h4, h5, h6 { color: var(--axon-text); letter-spacing: -0.025em; }
h2 { font-size: 1.72rem !important; line-height: 1.2 !important; font-weight: 760 !important; margin-bottom: .45rem !important; }
h3 { font-size: 1.34rem !important; line-height: 1.3 !important; font-weight: 720 !important; }
h4 { font-size: 1.13rem !important; font-weight: 690 !important; }
h5, h6 {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 1rem !important;
  font-weight: 680 !important; letter-spacing: .01em;
}
p, li, [data-testid="stMarkdownContainer"] { font-size: 15.5px; line-height: 1.62; }
[data-testid="stCaptionContainer"] { color: var(--axon-muted); font-size: 13px; font-weight: 450; }
hr { border: 0 !important; border-top: 1px solid var(--axon-line) !important; margin: 1rem 0 !important; }

/* Metrics and cards */
div[data-testid="stMetric"] {
  min-height: 92px; padding: 15px 17px;
  background: var(--axon-surface); border: 1px solid var(--axon-line);
  border-radius: var(--axon-radius); box-shadow: var(--axon-shadow);
  transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
}
div[data-testid="stMetric"]:hover {
  transform: translateY(-1px); border-color: var(--axon-line-strong);
  box-shadow: 0 0 0 1px rgba(139,140,255,.08), 0 0 24px rgba(112,113,255,.075), 0 14px 32px rgba(0,0,0,.22);
}
[data-testid="stMetricLabel"] {
  color: var(--axon-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; font-weight: 650; letter-spacing: .045em;
}
[data-testid="stMetricValue"] { color: var(--axon-text); font-size: 1.72rem; font-weight: 760; letter-spacing: -.04em; }
[data-testid="stMetricDelta"] { font-size: 12px; font-weight: 560; }

div[data-testid="stExpander"] {
  margin: 0 0 10px; overflow: hidden; background: var(--axon-surface);
  border: 1px solid var(--axon-line) !important; border-radius: var(--axon-radius) !important;
  box-shadow: 0 1px 0 rgba(255,255,255,.02) inset, 0 8px 24px rgba(0,0,0,.12);
  transition: border-color .18s ease, box-shadow .18s ease, transform .18s ease;
}
div[data-testid="stExpander"]:hover {
  border-color: var(--axon-line-strong) !important;
  box-shadow: 0 0 22px rgba(112,113,255,.065), 0 12px 28px rgba(0,0,0,.18);
}
div[data-testid="stExpander"] details > summary {
  min-height: 54px; padding: 5px 10px; color: #dce4ef; font-size: 14.5px; font-weight: 650;
}
div[data-testid="stExpander"] details > summary:hover { color: var(--axon-accent); }
div[data-testid="stExpander"] details[open] > summary { border-bottom: 1px solid var(--axon-line); }
div[data-testid="stExpanderDetails"] { padding: 14px 16px 16px; }

.memory-block {
  background: linear-gradient(135deg, rgba(139,140,255,.10), rgba(19,25,35,.82));
  border: 1px solid rgba(151,152,255,.20); border-left: 2px solid var(--axon-accent);
  border-radius: 10px; padding: 13px 15px; margin: 1px 0 14px;
  box-shadow: inset 0 1px rgba(255,255,255,.018), 0 0 24px rgba(112,113,255,.045); line-height: 1.6;
}
.project-block {
  background: linear-gradient(135deg, rgba(77,212,167,.075), rgba(19,25,35,.82));
  border-color: rgba(77,212,167,.18); border-left-color: var(--axon-green);
  box-shadow: inset 0 1px rgba(255,255,255,.018), 0 0 24px rgba(77,212,167,.035);
}
.project-stages {
  display: flex; align-items: flex-start; width: 100%; overflow-x: auto;
  padding: 11px 4px 17px; margin: 2px 0 15px;
}
.project-stage {
  position: relative; flex: 1 0 120px; min-width: 120px; text-align: center;
  color: var(--axon-faint);
}
.project-stage:not(:last-child)::after {
  content: ""; position: absolute; z-index: 0; top: 15px; left: calc(50% + 17px);
  width: calc(100% - 34px); height: 1px; background: rgba(169,184,207,.20);
}
.project-stage.done:not(:last-child)::after {
  background: linear-gradient(90deg, var(--axon-green), rgba(77,212,167,.42));
  box-shadow: 0 0 9px rgba(77,212,167,.20);
}
.project-stage-node {
  position: relative; z-index: 1; display: grid; place-items: center;
  width: 31px; height: 31px; margin: 0 auto 8px; border-radius: 50%;
  border: 1px solid rgba(169,184,207,.25); background: #111721;
  color: var(--axon-faint); font: 750 13px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.project-stage.done .project-stage-node {
  color: #07140f; border-color: var(--axon-green); background: var(--axon-green);
  box-shadow: 0 0 15px rgba(77,212,167,.28);
}
.project-stage.in-progress .project-stage-node {
  color: white; border-color: var(--axon-accent-bright); background: var(--axon-accent);
  box-shadow: 0 0 0 4px rgba(139,140,255,.10), 0 0 19px rgba(139,140,255,.38);
  animation: axon-stage-pulse 2.2s ease-in-out infinite;
}
.project-stage-name {
  color: #8792a4; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; font-weight: 620; line-height: 1.35;
}
.project-stage.done .project-stage-name { color: #b5e9d7; }
.project-stage.in-progress .project-stage-name { color: #dadaff; font-weight: 760; }

/* Anton state: cyber-dojo discipline board */
.dojo-shell {
  position: relative; overflow: hidden; margin: 0 0 20px; padding: 24px 26px;
  border: 1px solid rgba(100,216,236,.28); border-radius: 16px;
  background: linear-gradient(125deg, rgba(13,22,29,.98), rgba(19,15,30,.96));
  box-shadow: 0 0 34px rgba(100,216,236,.08), inset 0 1px rgba(255,255,255,.04);
}
.dojo-shell::after {
  content: "道"; position: absolute; right: 24px; top: -20px;
  color: rgba(100,216,236,.055); font: 800 128px/1 ui-serif, Georgia, serif;
}
.dojo-eyebrow, .dojo-label {
  color: #64d8ec; font: 720 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: .16em; text-transform: uppercase;
}
.dojo-title { margin: 5px 0; color: #f1f7fb; font-size: 30px; font-weight: 790; letter-spacing: -.035em; }
.dojo-subtitle { max-width: 700px; color: #9eafbd; font-size: 15px; }
.dojo-card {
  min-height: 150px; margin: 4px 0 14px; padding: 17px 18px;
  border: 1px solid rgba(100,216,236,.17); border-top: 2px solid #64d8ec;
  border-radius: 13px; background: rgba(14,20,28,.82);
  box-shadow: 0 10px 28px rgba(0,0,0,.2), 0 0 20px rgba(100,216,236,.045);
}
.dojo-card.avoid { border-top-color: #ff5fc8; box-shadow: 0 0 22px rgba(255,95,200,.05); }
.dojo-card.build { border-top-color: #4dd4a7; box-shadow: 0 0 22px rgba(77,212,167,.05); }
.dojo-value { margin: 12px 0 3px; color: #f4fbff; font: 780 34px/1 ui-monospace, SFMono-Regular, Menlo, monospace; }
.dojo-value span { color: #82909e; font-size: 13px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; }
.dojo-detail { color: #9eafbd; font-size: 14px; line-height: 1.5; }
.dojo-belt { height: 5px; margin-top: 14px; border-radius: 999px; background: rgba(255,255,255,.07); overflow: hidden; }
.dojo-belt > i { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg,#4dd4a7,#64d8ec); box-shadow: 0 0 12px #64d8ec; }
.dojo-belt.avoid > i { background: linear-gradient(90deg,#ff5fc8,#9b7cff); box-shadow: 0 0 12px #ff5fc8; }
.dojo-section { margin: 24px 0 10px; color: #dce9ef; font: 760 16px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .04em; }
.dojo-actor { border-left-color: #64d8ec; background: linear-gradient(110deg,rgba(100,216,236,.06),rgba(139,140,255,.04)); }
.stage-detail-heading {
  display: flex; align-items: center; gap: 8px; margin-bottom: 7px;
  color: #dadaff; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; font-weight: 760; letter-spacing: .045em; text-transform: uppercase;
}
.stage-detail-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--axon-accent);
  box-shadow: 0 0 12px rgba(139,140,255,.55);
}
@keyframes axon-stage-pulse {
  0%, 100% { box-shadow: 0 0 0 4px rgba(139,140,255,.08), 0 0 15px rgba(139,140,255,.28); }
  50% { box-shadow: 0 0 0 6px rgba(139,140,255,.14), 0 0 25px rgba(139,140,255,.48); }
}
.axon-badge {
  padding: 4px 9px; border: 1px solid rgba(255,255,255,.13); border-radius: 999px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
  font-weight: 720; letter-spacing: .055em; text-transform: uppercase; box-shadow: 0 0 12px rgba(139,140,255,.09);
}
.memory-id { margin: 10px 0 4px; color: #d4dce8; font-size: 13px; }
.memory-meta { color: var(--axon-muted); font-size: 13px; margin-top: 7px; }
.insight-label-row {
  display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin: 0 0 8px;
}
.insight-label {
  display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 999px;
  color: #dadaff; background: rgba(139,140,255,.10); border: 1px solid rgba(169,170,255,.20);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10px;
  font-weight: 720; letter-spacing: .055em; text-transform: uppercase;
}
.insight-label.secondary {
  color: var(--axon-muted); background: rgba(169,184,207,.045); border-color: var(--axon-line);
}
[data-testid="stVerticalBlockBorderWrapper"] {
  margin: 0 0 13px; padding: 14px 16px !important;
  background: linear-gradient(135deg, rgba(139,140,255,.065), rgba(18,23,32,.88));
  border: 1px solid rgba(151,152,255,.16) !important; border-left: 2px solid var(--axon-accent) !important;
  border-radius: 10px !important;
  box-shadow: inset 0 1px rgba(255,255,255,.018), 0 0 22px rgba(112,113,255,.035);
}
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] p:last-child { margin-bottom: 0; }

/* Data, plots and media */
[data-testid="stDataFrame"], [data-testid="stTable"] {
  overflow: hidden; border: 1px solid var(--axon-line); border-radius: var(--axon-radius);
  background: var(--axon-surface-raised); box-shadow: 0 10px 28px rgba(0,0,0,.14);
  --gdg-bg-cell: #131923; --gdg-bg-header: #171e29; --gdg-text-dark: #e6edf7;
  --gdg-border-color: rgba(169,184,207,.12); font-size: 14px;
}
[data-testid="stPlotlyChart"], [data-testid="stGraphVizChart"] {
  padding: 10px; background: rgba(18,23,32,.86); border: 1px solid var(--axon-line);
  border-radius: var(--axon-radius); box-shadow: 0 10px 28px rgba(0,0,0,.14);
}
[data-testid="stJson"] { border: 1px solid var(--axon-line); border-radius: 10px; overflow: hidden; font-size: 13px; }
video, iframe { border-radius: var(--axon-radius); border: 1px solid var(--axon-line); background: #10151d; }

/* Controls and feedback */
.stButton > button, .stDownloadButton > button {
  min-height: 40px; padding: 8px 14px; color: #dce4ef; background: #151b25;
  border: 1px solid var(--axon-line-strong); border-radius: 9px;
  font-size: 13.5px; font-weight: 680; box-shadow: 0 5px 16px rgba(0,0,0,.14);
  transition: transform .15s ease, border-color .15s ease, box-shadow .15s ease, color .15s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover {
  color: var(--axon-accent-bright); border-color: rgba(139,140,255,.48);
  box-shadow: 0 0 20px rgba(112,113,255,.12), 0 8px 18px rgba(0,0,0,.20); transform: translateY(-1px);
}
.stButton > button:active, .stDownloadButton > button:active { transform: translateY(0); box-shadow: none; }
[data-baseweb="select"] > div, [data-baseweb="input"] > div, textarea {
  color: var(--axon-text) !important; background: #131923 !important;
  border-color: var(--axon-line-strong) !important; border-radius: 9px !important; font-size: 14px !important;
}
[data-testid="stAlert"] { border: 1px solid var(--axon-line); border-radius: 10px; box-shadow: none; }
.split-label {
  color: var(--axon-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; font-weight: 720; letter-spacing: .09em; text-transform: uppercase;
}

/* Polished scrollbars */
* { scrollbar-width: thin; scrollbar-color: #394457 transparent; }
*::-webkit-scrollbar { width: 8px; height: 8px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb { background: #394457; border: 2px solid transparent; border-radius: 99px; background-clip: padding-box; }
*::-webkit-scrollbar-thumb:hover { background-color: #536077; }

/* ── Mobile responsiveness (phones/small tablets) ───────────────────────── */
@media (max-width: 640px) {
  .block-container { padding: .7rem .75rem 2rem !important; }
  .axon-masthead { min-height: 46px; margin-bottom: 4px; }
  .axon-kicker { display: none; }
  /* Streamlit column layouts wrap by default via flex-wrap below; this just
     tightens spacing so wrapped columns don't look sparse */
  div[data-testid="column"] { min-width: 100% !important; }
  div[data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .stTabs [data-baseweb="tab-list"] { flex-wrap: nowrap; width: 100%; }
  .stTabs [data-baseweb="tab"] { padding: 7px 11px; font-size: 13px; }
  .stTabs [data-baseweb="tab-panel"] { padding-top: 1.2rem; }
  h2 { font-size: 1.45rem !important; }
  h3 { font-size: 1.22rem !important; }
  h4, h5 { font-size: 1.05rem !important; }
  div[data-testid="stMetric"] { min-height: 82px; padding: 12px 13px; }
  .project-stage { flex-basis: 105px; min-width: 105px; }
  .project-stage-name { font-size: 11px; }
  /* Graphviz/plotly charts: allow horizontal scroll instead of illegible squish */
  div[data-testid="stGraphVizChart"], .js-plotly-plot {
    overflow-x: auto !important; max-width: 100% !important;
  }
}
</style>
""", unsafe_allow_html=True)

_instance_color = {"rog": "#4A90D9", "aevadim09": "#D97A4A"}.get(AXON_INSTANCE, "#888")
st.markdown(
    '<div class="axon-masthead">'
    '<div class="axon-wordmark"><div class="axon-mark">⚡</div><div>'
    '<div class="axon-title">Axon</div><div class="axon-kicker">Executive intelligence dashboard</div>'
    '</div></div>'
    f'<div class="axon-instance"><span class="axon-instance-dot" style="background:{_instance_color}"></span>'
    f'{html.escape(AXON_INSTANCE.upper())}</div></div>',
    unsafe_allow_html=True,
)

tab_food, tab_fitness, tab_alive, tab_anton, tab_actors, tab_projects, tab_architecture, tab_files, tab_manim, tab_database = st.tabs(
    ["🍽 Food Today", "💪 Fitness Week", "🧠 Alive State", "🥋 Anton State", "🎭 Actors", "🧭 Projects", "🗺 Architecture", "📄 Files", "🎬 Manim", "◈ Database"]
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

# ── Tab: Anton State ──────────────────────────────────────────────────────────
with tab_anton:
    st.markdown(
        '<div class="dojo-shell"><div class="dojo-eyebrow">Discipline telemetry · cyber dojo</div>'
        '<div class="dojo-title">Anton State 道</div>'
        '<div class="dojo-subtitle">Concrete signals only. Avoid streaks reset on logged incidents; '
        'build practices compound through steady repetition.</div></div>',
        unsafe_allow_html=True,
    )

    def _event_date(row):
        # event_date is when the behavior actually happened; created_at is
        # only when it was logged, which can differ for retrospective entries.
        raw = row.get("event_date") or row.get("created_at") or row.get("date") or row.get("logged_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            return None

    def _normalized_value(row, *keys):
        for key in keys:
            if row.get(key) is not None:
                return str(row[key]).strip().lower().replace("_", " ").replace("-", " ")
        return ""

    behavior_rows = _sb_get("compulsive_behavior_tracking", "order=created_at.desc")
    today_date = date.today()
    avoid_behaviors = {
        "Weed": ("weed", "cannabis", "marijuana"),
        "Nail biting": ("nail biting", "nailbiting", "nails"),
        "Porn": ("porn", "pornography"),
    }

    st.markdown('<div class="dojo-section">01 // AVOID · ZERO TARGET</div>', unsafe_allow_html=True)
    avoid_columns = st.columns(3)
    for column, (label, aliases) in zip(avoid_columns, avoid_behaviors.items()):
        incidents = []
        for event in behavior_rows:
            behavior = _normalized_value(event, "behavior", "behavior_name", "behavior_type", "name")
            event_kind = _normalized_value(event, "event_type", "event", "status", "type", "action")
            if any(alias in behavior for alias in aliases) and event_kind in {"relapse", "incident"}:
                event_day = _event_date(event)
                if event_day:
                    incidents.append(event_day)
        last_incident = max(incidents) if incidents else None
        streak = max(0, (today_date - last_incident).days) if last_incident else None
        value = f"{streak}" if streak is not None else "—"
        detail = (f"Last incident · {last_incident.isoformat()}" if last_incident
                  else "No relapse or incident is logged yet")
        belt_width = min(100, 8 + (streak or 0) * 4) if streak is not None else 0
        column.markdown(
            f'<div class="dojo-card avoid"><div class="dojo-label">{html.escape(label)}</div>'
            f'<div class="dojo-value">{value} <span>clean days</span></div>'
            f'<div class="dojo-detail">{html.escape(detail)}</div>'
            f'<div class="dojo-belt avoid"><i style="width:{belt_width}%"></i></div></div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="dojo-section">02 // BUILD · COMPOUND PRACTICE</div>', unsafe_allow_html=True)
    recent_days = 14
    recent_cutoff = today_date - timedelta(days=recent_days - 1)
    practices = []
    for event in behavior_rows:
        behavior = _normalized_value(event, "behavior", "behavior_name", "behavior_type", "name")
        event_kind = _normalized_value(event, "event_type", "event", "status", "type", "action")
        event_day = _event_date(event)
        if (event_day and event_kind == "positive practice"
                and any(token in behavior for token in ("wim hof", "breathing", "breathwork"))):
            practices.append(event_day)
    practice_days = {day for day in practices if day >= recent_cutoff}
    last_practice = max(practices) if practices else None
    days_since_practice = ((today_date - last_practice).days if last_practice else None)

    food_rows = _sb_get(
        "food_entries",
        f"date=gte.{recent_cutoff.isoformat()}&order=date.asc&select=date,calories",
    )
    calories_by_day = {}
    for entry in food_rows:
        day = str(entry.get("date") or "")
        if day:
            calories_by_day[day] = calories_by_day.get(day, 0) + float(entry.get("calories") or 0)
    target_days = sum(1800 <= kcal <= 1900 for kcal in calories_by_day.values())
    acceptable_days = sum(1900 < kcal <= 2000 for kcal in calories_by_day.values())
    steady_days = target_days + acceptable_days

    workout_cutoff = today_date - timedelta(days=7)
    workout_rows = _sb_get(
        "fitness_log",
        f"date=gte.{workout_cutoff.isoformat()}&order=date.desc&select="
        "date,pushups_sets,pullups_sets,pistol_sets",
    )
    workout_sets = sum(
        int(row.get("pushups_sets") or 0)
        + int(row.get("pullups_sets") or 0)
        + int(row.get("pistol_sets") or 0)
        for row in workout_rows
    )

    build_columns = st.columns(3)
    practice_value = str(days_since_practice) if days_since_practice is not None else "—"
    practice_detail = (f"{len(practice_days)} of the last {recent_days} days logged"
                       if last_practice else "No positive practice is logged yet")
    build_columns[0].markdown(
        f'<div class="dojo-card build"><div class="dojo-label">Wim Hof breathing</div>'
        f'<div class="dojo-value">{practice_value} <span>days since</span></div>'
        f'<div class="dojo-detail">{html.escape(practice_detail)}</div>'
        f'<div class="dojo-belt"><i style="width:{min(100, len(practice_days) / recent_days * 100):.0f}%"></i></div></div>',
        unsafe_allow_html=True,
    )
    build_columns[1].markdown(
        f'<div class="dojo-card build"><div class="dojo-label">Calorie steadiness</div>'
        f'<div class="dojo-value">{steady_days}<span> / {len(calories_by_day)} logged days</span></div>'
        f'<div class="dojo-detail">{target_days} at 1800–1900 · {acceptable_days} acceptable to 2000</div>'
        f'<div class="dojo-belt"><i style="width:{min(100, steady_days / max(1, len(calories_by_day)) * 100):.0f}%"></i></div></div>',
        unsafe_allow_html=True,
    )
    build_columns[2].markdown(
        f'<div class="dojo-card build"><div class="dojo-label">Workout volume · 7 days</div>'
        f'<div class="dojo-value">{workout_sets}<span> / 60 sets</span></div>'
        f'<div class="dojo-detail">Push-ups · pull-ups · pistol sets</div>'
        f'<div class="dojo-belt"><i style="width:{min(100, workout_sets / 60 * 100):.0f}%"></i></div></div>',
        unsafe_allow_html=True,
    )

    if calories_by_day:
        chart_days = sorted(calories_by_day)
        chart_values = [calories_by_day[day] for day in chart_days]
        chart_colors = [
            "#4dd4a7" if 1800 <= value <= 1900 else "#64d8ec" if 1900 < value <= 2000 else "#ff5fc8"
            for value in chart_values
        ]
        fig = go.Figure(go.Bar(x=chart_days, y=chart_values, marker_color=chart_colors))
        fig.add_hrect(y0=1800, y1=1900, fillcolor="#4dd4a7", opacity=.08, line_width=0)
        fig.add_hline(y=2000, line_color="#64d8ec", line_dash="dot", opacity=.7)
        fig.update_layout(
            title="Calorie discipline · recent logged days", height=280,
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font_color="#aebdca", margin=dict(t=45, b=25, l=20, r=15),
            xaxis=dict(gridcolor="rgba(255,255,255,.04)"),
            yaxis=dict(title="kcal", gridcolor="rgba(255,255,255,.06)"),
        )
        st.plotly_chart(fig, use_container_width=True)

    st.markdown('<div class="dojo-section">03 // ACTOR READOUT</div>', unsafe_allow_html=True)
    actor_rows = _sb_get(
        "actor_state",
        "actor_type=eq.anton-state-tracker&order=last_advanced_at.desc.nullslast&limit=1"
        "&select=actor_id,actor_type,disposition,revision,state,directory_projection,last_advanced_at",
    )
    if actor_rows:
        actor = actor_rows[0]
        projection = actor.get("directory_projection") or {}
        summary = projection.get("summary") or (actor.get("state") or {}).get("summary") or "No summary yet."
        st.markdown(
            '<div class="memory-block dojo-actor">'
            f'<div class="memory-id">{html.escape(str(actor.get("actor_id", "anton-state-tracker")))}</div>'
            f'<div style="margin:8px 0;color:#e9f5fa;font-size:16px;">{html.escape(str(summary))}</div>'
            f'<div class="memory-meta">{html.escape(str(actor.get("disposition", "—")))} · '
            f'revision {html.escape(str(actor.get("revision", 0)))} · last advanced '
            f'{html.escape(str(actor.get("last_advanced_at") or "never"))}</div></div>',
            unsafe_allow_html=True,
        )
        with st.expander("Inspect full anton-state-tracker state"):
            st.json(actor.get("state") or {})
    else:
        st.info("anton-state-tracker actor state is not available.")

    if st.button("🔄 Refresh Anton state"):
        st.rerun()

# ── Tab: Actors ───────────────────────────────────────────────────────────────
with tab_actors:
    st.subheader("Actor Memory Blocks")
    st.caption("Each block is one persistent actor thread, global/shared across instances. Open it to inspect current memory and its complete assigned event stream.")
    actors = _sb_get(
        "actor_state",
        "order=actor_id.asc"
        "&select=actor_id,actor_type,disposition,revision,state,directory_projection,last_advanced_at,nice",
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
            nice = actor.get("nice", 0)
            summary = (actor.get("directory_projection") or {}).get("summary", "")
            label = f"▦  {actor_type}  ·  {actor_id}  ·  {disposition}  ·  rev {revision}  ·  nice {nice}"
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
        st.info("No actor rows. Apply the migration and run the backfill first.")

    if st.button("🔄 Refresh actors"):
        st.rerun()

# ── Tab: Ongoing Projects ─────────────────────────────────────────────────────
with tab_projects:
    st.subheader("Ongoing Projects")
    st.caption("Anton's current work areas as cognitive threads. Open a project to inspect its recent accumulated insight history.")
    projects = _sb_get(
        "projects",
        "order=status.asc,name.asc&select=id,name,status,domain,description,started_at,metadata",
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

                stages = (project.get("metadata") or {}).get("stages")
                if isinstance(stages, list):
                    stage_blocks = []
                    valid_stages = []
                    for index, stage in enumerate(stages):
                        if not isinstance(stage, dict):
                            continue
                        stage_status = stage.get("status", "planned")
                        status_class = "in-progress" if stage_status == "in_progress" else (
                            "done" if stage_status == "done" else "planned"
                        )
                        marker = "✓" if stage_status == "done" else str(index + 1)
                        stage_name = str(stage.get("name") or f"Stage {index + 1}")
                        valid_stages.append((stage_name, stage))
                        stage_blocks.append(
                            f'<div class="project-stage {status_class}">'
                            f'<div class="project-stage-node">{html.escape(marker)}</div>'
                            f'<div class="project-stage-name">{html.escape(stage_name)}</div>'
                            '</div>'
                        )
                    if stage_blocks:
                        st.markdown(
                            '<div class="project-stages">' + "".join(stage_blocks) + '</div>',
                            unsafe_allow_html=True,
                        )
                        selected_stage_index = st.selectbox(
                            "Inspect stage",
                            range(len(valid_stages)),
                            format_func=lambda position: valid_stages[position][0],
                            key=f"project_stage_{project_id}",
                        )
                        selected_stage_name, selected_stage = valid_stages[selected_stage_index]
                        with st.container(border=True):
                            st.markdown(
                                '<div class="stage-detail-heading">'
                                '<span class="stage-detail-dot"></span>'
                                f'{html.escape(selected_stage_name)}</div>',
                                unsafe_allow_html=True,
                            )
                            detail = selected_stage.get("detail")
                            if detail:
                                st.markdown(str(detail))
                            else:
                                st.caption("No stage detail recorded yet.")

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
                        with st.container(border=True):
                            labels = (
                                f'<div class="insight-label-row">'
                                f'<span class="insight-label">{html.escape(str(insight_type))}</span>'
                                f'<span class="insight-label secondary">'
                                f'{html.escape(str(insight.get("created_at") or "unknown date"))}</span>'
                            )
                            if confidence is not None:
                                labels += (
                                    f'<span class="insight-label secondary">confidence '
                                    f'{html.escape(str(confidence))}</span>'
                                )
                            labels += '</div>'
                            st.markdown(labels, unsafe_allow_html=True)
                            st.markdown(insight.get("content") or "")
                            if insight.get("context"):
                                st.caption(f"Context: {insight['context']}")
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
    st.caption("Deployed relay views plus the corrected prompt-embedded actor design. No actor timers, polling, background process, or separate actor LLM calls.")

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
        st.markdown("#### Corrected actor design · prompt-embedded, turn-bound")
        st.caption(
            "Hard security boundary: actors can advance only inside a real prompt sent by Anton. "
            "They are blocks in the normal conversation—not processes, timers, polling loops, or separate model calls."
        )
        st.graphviz_chart(r'''
digraph corrected_actor_boundary {
  graph [rankdir=LR, bgcolor="transparent", pad=0.25, nodesep=0.65, ranksep=0.5,
         fontname="Arial", label="ACTORS EXIST ONLY INSIDE ANTON'S CONVERSATIONAL TURN", labelloc=t,
         fontsize=18, fontcolor="#A9AAFF"];
  node [shape=plain, fontname="Arial"];
  edge [color="#8B8CFF", penwidth=3, arrowsize=1.05, fontname="Courier New", fontsize=10, fontcolor="#DADAFF"];

  anton [label=<
    <TABLE BORDER="2" CELLBORDER="0" CELLSPACING="0" CELLPADDING="11" COLOR="#64D8EC" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#28758A"><FONT COLOR="white"><B>ACTUAL PROMPT FROM ANTON</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#DDE7F4">the only activation trigger</FONT></TD></TR>
    </TABLE>
  >];

  turn [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="9" COLOR="#8B8CFF" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#5557B8"><FONT COLOR="white"><B>ONE CONVERSATIONAL TURN</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">normal context + user message</FONT></TD></TR>
      <TR><TD><FONT COLOR="#DADAFF"><B>variable actor blocks 1..N</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">one shared LLM call</FONT></TD></TR>
    </TABLE>
  >];

  invariant [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="9" COLOR="#D9778B" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#8C4252"><FONT COLOR="white"><B>HARD SECURITY INVARIANT</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">zero timers · zero polling</FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">zero background actor processes</FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">zero separate actor LLM calls</FONT></TD></TR>
    </TABLE>
  >];

  anton -> turn [label="build + send"];
  turn -> invariant [style=invis];
}
''', use_container_width=True)

    with view_prompt:
        st.markdown("#### Prompt-embedded actors · input and output contract")
        st.caption(
            "One normal conversational call carries every selected actor block in and every updated actor block out. "
            "Capacity (MAX_ACTOR_SLOTS) is filled by lowest `nice` first, Linux-style. Below the cap, a dirty-bit gate "
            "skips including a low-priority actor's block at all when nothing relevant changed since its last pass "
            "(e.g. no new food_entries/fitness_log rows) — this is a real prompt-token omission, not a lighter pass. "
            "Static role/instruction text is hashed per actor; a hash already sent earlier in the same session is "
            "replaced with a compact reference instead of resending the full text."
        )
        st.graphviz_chart(r'''
digraph prompt_actor_contract {
  graph [rankdir=LR, bgcolor="transparent", pad=0.22, nodesep=0.48, ranksep=0.4,
         fontname="Arial", label="ONE PROMPT · ONE LLM CALL · N ACTOR BLOCKS", labelloc=t,
         fontsize=18, fontcolor="#A9AAFF"];
  node [shape=plain, fontname="Arial"];
  edge [color="#8B8CFF", penwidth=3, arrowsize=1.05, fontname="Courier New", fontsize=9, fontcolor="#DADAFF"];

  input [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="9" COLOR="#64D8EC" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#28758A"><FONT COLOR="white"><B>INPUT · BUILT FOR THIS PROMPT</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT"><FONT COLOR="#E8ECF5">normal conversational context</FONT></TD></TR>
      <TR><TD ALIGN="LEFT"><FONT COLOR="#E8ECF5">Anton's current message</FONT></TD></TR>
      <TR><TD BGCOLOR="#1B2038" ALIGN="LEFT"><FONT COLOR="#DADAFF"><B>actor block 1</B> · current stored state</FONT></TD></TR>
      <TR><TD BGCOLOR="#1B2038" ALIGN="LEFT"><FONT COLOR="#A9AAFF">actor block 2 … actor block N</FONT></TD></TR>
      <TR><TD ALIGN="LEFT"><FONT COLOR="#9AA6B7">N = active · not finished · not errored · within capacity</FONT></TD></TR>
    </TABLE>
  >];

  model [label=<
    <TABLE BORDER="2" CELLBORDER="0" CELLSPACING="0" CELLPADDING="13" COLOR="#8B8CFF" BGCOLOR="#171B31">
      <TR><TD><FONT COLOR="white"><B>ONE SHARED<BR/>LLM CALL</B></FONT></TD></TR>
    </TABLE>
  >];

  output [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="9" COLOR="#4DD4A7" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#217A63"><FONT COLOR="white"><B>OUTPUT · ONE RESPONSE</B></FONT></TD></TR>
      <TR><TD ALIGN="LEFT"><FONT COLOR="#E8ECF5">normal conversational reply</FONT></TD></TR>
      <TR><TD BGCOLOR="#15251F" ALIGN="LEFT"><FONT COLOR="#B5E9D7"><B>updated actor block 1</B> · running | finished | error</FONT></TD></TR>
      <TR><TD BGCOLOR="#15251F" ALIGN="LEFT"><FONT COLOR="#8ED9C0">updated actor block 2 … actor block N</FONT></TD></TR>
    </TABLE>
  >];

  store [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="9" COLOR="#7779E8" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#5557B8"><FONT COLOR="white"><B>SUPABASE</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">parse blocks</FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">append to each actor's row / history</FONT></TD></TR>
    </TABLE>
  >];

  input -> model [label="same prompt"];
  model -> output [label="same response"];
  output -> store [label="updated actor blocks"];
}
''', use_container_width=True)

    with view_affect:
        st.markdown("#### Legacy affective loop · superseded reference")
        st.caption(
            "Historical deployed behavior retained as a reference for the filter math. Its reflection timer is not active; "
            "under the corrected design, any future affect actors can advance only as blocks inside Anton's real prompt."
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
        st.markdown("#### Corrected target: affect as prompt blocks")
        st.caption(
            "Axon, Anton-model, and reflection state may appear as variable actor blocks in the same conversational prompt. "
            "They do not tick independently and do not invoke separate model calls."
        )
        st.graphviz_chart(r'''
digraph affective_prompt_blocks {
  graph [rankdir=LR, bgcolor="transparent", pad=0.2, nodesep=0.55,
         fontname="Arial", label="SAME TURN · SAME PROMPT · SAME LLM CALL", labelloc=t,
         fontsize=17, fontcolor="#A9AAFF"];
  node [shape=plain, fontname="Arial"];
  edge [color="#8B8CFF", penwidth=3, arrowsize=1.0];
  prompt [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="9" COLOR="#8B8CFF" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#5557B8"><FONT COLOR="white"><B>CONVERSATIONAL PROMPT</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">normal context + Anton's message</FONT></TD></TR>
      <TR><TD><FONT COLOR="#DADAFF">Axon affect block · if active</FONT></TD></TR>
      <TR><TD><FONT COLOR="#DADAFF">Anton-model block · if active</FONT></TD></TR>
      <TR><TD><FONT COLOR="#DADAFF">reflection block · if active</FONT></TD></TR>
    </TABLE>
  >];
  output [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="9" COLOR="#4DD4A7" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#217A63"><FONT COLOR="white"><B>ONE RESPONSE</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">reply + updated block states</FONT></TD></TR>
    </TABLE>
  >];
  prompt -> output;
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

# Split View tab disabled 2026-08-12 at Anton's request (interfered with CLI reading).

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

# ── Tab: Database / Schema Overview ──────────────────────────────────────────
with tab_database:
    st.subheader("Database / Schema Overview")
    st.caption(
        "Manually refreshed Supabase schema snapshot · 2026-08-12. "
        "Only real foreign-key constraints are drawn; unconnected tables are intentionally standalone."
    )
    schema_core, schema_life, schema_knowledge = st.tabs(
        ["Core + actors + work", "Personal life", "Study + research"]
    )

    with schema_core:
        st.graphviz_chart(r'''
digraph schema_core {
  graph [rankdir=LR, bgcolor="transparent", pad=0.3, nodesep=0.65, ranksep=0.8,
         fontname="Arial", label="CORE, ACTORS & WORK", labelloc=t, fontsize=20, fontcolor="#A9AAFF"];
  node [shape=plain, fontname="Arial"];
  edge [color="#64D8EC", penwidth=2.2, arrowsize=0.85, fontname="Courier New", fontsize=9, fontcolor="#A8DCE7"];

  core [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#7779E8" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#5557B8"><FONT COLOR="white"><B>AXON CORE / AFFECT</B></FONT></TD></TR>
      <TR><TD PORT="alive_state"><FONT COLOR="#E8ECF5">alive_state</FONT></TD></TR>
      <TR><TD PORT="anton_model"><FONT COLOR="#E8ECF5">anton_model</FONT></TD></TR>
      <TR><TD PORT="anton_state_log"><FONT COLOR="#E8ECF5">anton_state_log</FONT></TD></TR>
      <TR><TD PORT="memory"><FONT COLOR="#E8ECF5">memory</FONT></TD></TR>
      <TR><TD PORT="messages"><FONT COLOR="#E8ECF5">messages</FONT></TD></TR>
      <TR><TD PORT="dreams"><FONT COLOR="#E8ECF5">dreams</FONT></TD></TR>
      <TR><TD PORT="insights"><FONT COLOR="#E8ECF5">insights</FONT></TD></TR>
      <TR><TD PORT="rules"><FONT COLOR="#E8ECF5">rules</FONT></TD></TR>
      <TR><TD PORT="agent_identity"><FONT COLOR="#E8ECF5">agent_identity</FONT></TD></TR>
      <TR><TD PORT="agent_instructions"><FONT COLOR="#E8ECF5">agent_instructions</FONT></TD></TR>
      <TR><TD PORT="architecture"><FONT COLOR="#E8ECF5">architecture</FONT></TD></TR>
      <TR><TD PORT="summaries"><FONT COLOR="#E8ECF5">summaries</FONT></TD></TR>
      <TR><TD PORT="logs"><FONT COLOR="#E8ECF5">logs</FONT></TD></TR>
      <TR><TD PORT="operation_policies"><FONT COLOR="#E8ECF5">operation_policies</FONT></TD></TR>
    </TABLE>
  >];

  actors [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#9A7BE8" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#6B4FA4"><FONT COLOR="white"><B>ACTOR MODEL</B></FONT></TD></TR>
      <TR><TD PORT="actor_state"><FONT COLOR="#E8ECF5">actor_state</FONT></TD></TR>
      <TR><TD PORT="events"><FONT COLOR="#E8ECF5">events</FONT></TD></TR>
      <TR><TD PORT="leases"><FONT COLOR="#E8ECF5">leases</FONT></TD></TR>
      <TR><TD PORT="obligations"><FONT COLOR="#E8ECF5">obligations</FONT></TD></TR>
    </TABLE>
  >];

  work [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#4DD4A7" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#217A63"><FONT COLOR="white"><B>PROJECTS / TASKS</B></FONT></TD></TR>
      <TR><TD PORT="projects"><FONT COLOR="#E8ECF5">projects</FONT></TD></TR>
      <TR><TD PORT="personal_tasks"><FONT COLOR="#E8ECF5">personal_tasks</FONT></TD></TR>
      <TR><TD PORT="task_actions"><FONT COLOR="#E8ECF5">task_actions</FONT></TD></TR>
      <TR><TD PORT="roadmap"><FONT COLOR="#E8ECF5">roadmap</FONT></TD></TR>
      <TR><TD PORT="personal_goals"><FONT COLOR="#E8ECF5">personal_goals</FONT></TD></TR>
    </TABLE>
  >];

  standalone [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#56657A" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#344052"><FONT COLOR="white"><B>STANDALONE</B></FONT></TD></TR>
      <TR><TD PORT="job_search"><FONT COLOR="#E8ECF5">job_search</FONT></TD></TR>
      <TR><TD PORT="documents"><FONT COLOR="#E8ECF5">documents</FONT></TD></TR>
    </TABLE>
  >];

  actors:leases:w -> actors:actor_state:w [label=" actor_id → actor_id", constraint=false];
  actors:obligations:w -> actors:actor_state:w [label=" owner_actor_id → actor_id", constraint=false];
  core:insights:e -> work:projects:w [label=" project_id → id"];
  work:personal_tasks:e -> work:personal_tasks:e [label=" parent_id → id", constraint=false];
  work:task_actions:w -> work:personal_tasks:w [label=" task_id → id", constraint=false];
  actors -> work [style=invis, weight=2];
  work -> standalone [style=invis, weight=2];
}
''', use_container_width=True)

    with schema_life:
        st.graphviz_chart(r'''
digraph schema_life {
  graph [rankdir=LR, bgcolor="transparent", pad=0.3, nodesep=0.65, ranksep=0.8,
         fontname="Arial", label="PERSONAL LIFE & TRACKING", labelloc=t, fontsize=20, fontcolor="#64D8EC"];
  node [shape=plain, fontname="Arial"];
  edge [color="#64D8EC", penwidth=2.2, arrowsize=0.85, fontname="Courier New", fontsize=9, fontcolor="#A8DCE7"];

  fitness [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#4DD4A7" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#217A63"><FONT COLOR="white"><B>FITNESS / FOOD</B></FONT></TD></TR>
      <TR><TD PORT="fitness_log"><FONT COLOR="#E8ECF5">fitness_log</FONT></TD></TR>
      <TR><TD PORT="food_entries"><FONT COLOR="#E8ECF5">food_entries</FONT></TD></TR>
      <TR><TD PORT="frequent_foods"><FONT COLOR="#E8ECF5">frequent_foods</FONT></TD></TR>
      <TR><TD PORT="meal_presets"><FONT COLOR="#E8ECF5">meal_presets</FONT></TD></TR>
      <TR><TD PORT="grocery_list"><FONT COLOR="#E8ECF5">grocery_list</FONT></TD></TR>
    </TABLE>
  >];

  personal [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#64D8EC" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#28758A"><FONT COLOR="white"><B>PERSONAL / HOME</B></FONT></TD></TR>
      <TR><TD PORT="personal"><FONT COLOR="#E8ECF5">personal</FONT></TD></TR>
      <TR><TD PORT="building_events"><FONT COLOR="#E8ECF5">building_events</FONT></TD></TR>
      <TR><TD PORT="building_residents"><FONT COLOR="#E8ECF5">building_residents</FONT></TD></TR>
      <TR><TD PORT="car"><FONT COLOR="#E8ECF5">car</FONT></TD></TR>
      <TR><TD PORT="car_events"><FONT COLOR="#E8ECF5">car_events</FONT></TD></TR>
      <TR><TD PORT="sewage_payments"><FONT COLOR="#E8ECF5">sewage_payments</FONT></TD></TR>
      <TR><TD PORT="barbecue_attendees"><FONT COLOR="#E8ECF5">barbecue_attendees</FONT></TD></TR>
      <TR><TD PORT="barbecue_items"><FONT COLOR="#E8ECF5">barbecue_items</FONT></TD></TR>
      <TR><TD PORT="property_requests"><FONT COLOR="#E8ECF5">property_requests</FONT></TD></TR>
      <TR><TD PORT="reimbursements"><FONT COLOR="#E8ECF5">reimbursements</FONT></TD></TR>
      <TR><TD PORT="finances"><FONT COLOR="#E8ECF5">finances</FONT></TD></TR>
      <TR><TD PORT="group_expenses"><FONT COLOR="#E8ECF5">group_expenses</FONT></TD></TR>
    </TABLE>
  >];

  ailin [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#D59B57" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#8A5E2B"><FONT COLOR="white"><B>AILIN</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">ailin_roadmap</FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">irina</FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">irina_phone_setup</FONT></TD></TR>
    </TABLE>
  >];

  russia [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#D9778B" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#8C4252"><FONT COLOR="white"><B>RUSSIA TRACKER</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">russia_fuel_crisis</FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">russia_fuel_regions</FONT></TD></TR>
    </TABLE>
  >];

  behavior [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#9A7BE8" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#6B4FA4"><FONT COLOR="white"><B>BEHAVIOR TRACKING</B></FONT></TD></TR>
      <TR><TD><FONT COLOR="#E8ECF5">compulsive_behavior_tracking</FONT></TD></TR>
    </TABLE>
  >];

  fitness:food_entries:e -> fitness:fitness_log:e [label=" date → date", constraint=false];
  personal:building_events:e -> personal:building_residents:e [label=" resident_id → id", constraint=false];
  personal:car_events:e -> personal:car:e [label=" car_id → id", constraint=false];
  fitness -> personal [style=invis, weight=3];
  personal -> ailin [style=invis, weight=3];
  ailin -> russia [style=invis, weight=2];
  russia -> behavior [style=invis, weight=2];
}
''', use_container_width=True)

    with schema_knowledge:
        st.graphviz_chart(r'''
digraph schema_knowledge {
  graph [rankdir=LR, bgcolor="transparent", pad=0.3, nodesep=0.8, ranksep=0.9,
         fontname="Arial", label="STUDY, RESEARCH & HARDWARE", labelloc=t, fontsize=20, fontcolor="#4DD4A7"];
  node [shape=plain, fontname="Arial"];
  edge [color="#64D8EC", penwidth=2.2, arrowsize=0.85, fontname="Courier New", fontsize=9, fontcolor="#A8DCE7"];

  study [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#4DD4A7" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#217A63"><FONT COLOR="white"><B>STUDY</B></FONT></TD></TR>
      <TR><TD PORT="study_areas"><FONT COLOR="#E8ECF5">study_areas</FONT></TD></TR>
      <TR><TD PORT="study_books"><FONT COLOR="#E8ECF5">study_books</FONT></TD></TR>
      <TR><TD PORT="study_book_chunks"><FONT COLOR="#E8ECF5">study_book_chunks</FONT></TD></TR>
      <TR><TD PORT="study_topics"><FONT COLOR="#E8ECF5">study_topics</FONT></TD></TR>
      <TR><TD PORT="study_exercises"><FONT COLOR="#E8ECF5">study_exercises</FONT></TD></TR>
      <TR><TD PORT="study_attempts"><FONT COLOR="#E8ECF5">study_attempts</FONT></TD></TR>
    </TABLE>
  >];

  research [label=<
    <TABLE BORDER="2" CELLBORDER="1" CELLSPACING="0" CELLPADDING="7" COLOR="#7779E8" BGCOLOR="#121720">
      <TR><TD BGCOLOR="#5557B8"><FONT COLOR="white"><B>ANPLOS / RESEARCH / HARDWARE</B></FONT></TD></TR>
      <TR><TD PORT="anpl_research"><FONT COLOR="#E8ECF5">anpl_research</FONT></TD></TR>
      <TR><TD PORT="hardware_eval"><FONT COLOR="#E8ECF5">hardware_eval</FONT></TD></TR>
      <TR><TD PORT="machines"><FONT COLOR="#E8ECF5">machines</FONT></TD></TR>
      <TR><TD PORT="autocad_docs"><FONT COLOR="#E8ECF5">autocad_docs</FONT></TD></TR>
      <TR><TD PORT="kb_items"><FONT COLOR="#E8ECF5">kb_items</FONT></TD></TR>
      <TR><TD PORT="kb_chunks"><FONT COLOR="#E8ECF5">kb_chunks</FONT></TD></TR>
    </TABLE>
  >];

  study:study_books:e -> study:study_areas:e [label=" area_id → id", constraint=false];
  study:study_book_chunks:e -> study:study_books:e [label=" book_id → id", constraint=false];
  study:study_topics:e -> study:study_books:e [label=" book_id → id", constraint=false];
  study:study_topics:w -> study:study_areas:w [label=" area_id → id", constraint=false];
  study:study_exercises:e -> study:study_books:e [label=" book_id → id", constraint=false];
  study:study_exercises:w -> study:study_topics:w [label=" topic_id → id", constraint=false];
  study:study_attempts:e -> study:study_topics:e [label=" topic_id → id", constraint=false];
  study:study_attempts:w -> study:study_exercises:w [label=" exercise_id → id", constraint=false];
  research:kb_chunks:e -> research:kb_items:e [label=" item_id → id", constraint=false];
  study -> research [style=invis, weight=3];
}
''', use_container_width=True)
