#!/usr/bin/env python3
from __future__ import annotations
"""
SessionManagerNode — Relay v2

Owns the persistent Claude CLI process. Receives messages from all frontends
via sockets, serializes them into Claude's stdin, and publishes responses.

Sockets:
  user_input.sock   — NDJSON in:  {text, source, user_id, media_path?}
  cli_input.sock    — raw bytes in: keyboard input from CLINode
  display.sock      — raw bytes out: PTY output to CLINode
  claude_response.sock — NDJSON out: {text, source, user_id}

State machine (queue processor):
  IDLE:       keyboard bytes flow freely to Claude's PTY.
              Dequeues next item when available → GENERATING.
  GENERATING: keyboard bytes buffered (not forwarded).
              Polls session JSONL for new assistant entry → publishes → IDLE.

Response detection strategy:
  Claude's interactive TUI does NOT echo all response text to the PTY (it
  suppresses control tokens like sentinels). Instead, we watch the session
  JSONL file which always contains the complete, clean response text once
  a turn is finished. No sentinel needed.
"""

import os
import sys
import pty
import fcntl
import termios
import struct
import socket
import threading
import queue
import re
import signal as signal_module
import json
import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
import supabase_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [session_manager] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# How long to wait for Claude's response before giving up (seconds)
# Needs to be long enough for multi-tool runs with several permission prompts
_RESPONSE_TIMEOUT = 600
# How often to poll the session JSONL (seconds)
_POLL_INTERVAL = 0.5
# If file has stopped growing for this long with no "text" entry, return
# whatever text we have (catches cases where Claude ends on a tool_use)
_STALL_FALLBACK = 30.0


@dataclass
class QueueItem:
    text: str
    source: str       # "telegram" | "proactive"
    user_id: str
    media_path: Optional[str] = None


class SessionManagerNode:

    def __init__(self):
        self.input_queue: queue.Queue[Optional[QueueItem]] = queue.Queue()
        self.state = "IDLE"           # IDLE | GENERATING
        self.current_item: Optional[QueueItem] = None

        # Keyboard bytes buffered while GENERATING
        self.keyboard_buffer: list[bytes] = []
        self.state_lock = threading.Lock()

        # Ring buffer of recent PTY output for TUI prompt detection (~4KB)
        self._pty_output_buf: deque = deque(maxlen=4096)
        self._last_tui_prompt_hash: Optional[int] = None

        self.master_fd: Optional[int] = None
        self.claude_proc: Optional[subprocess.Popen] = None
        self.pty_lock = threading.Lock()

        # CLINode display connection (one at a time)
        self.display_client: Optional[socket.socket] = None
        self.display_lock = threading.Lock()

        # Subscribers to claude_response.sock (RouterNode, etc.)
        self.response_subscribers: list[socket.socket] = []
        self.response_subs_lock = threading.Lock()

        # Tracked after Claude spawns so JSONL watcher knows where to look
        self.current_session_id: Optional[str] = None
        # Epoch time of most recent Claude spawn — used to filter session files
        self._spawn_time: float = 0.0

        # Permission request state
        # When a PermissionRequest hook connects, we hold its connection here
        # until a decision arrives (from Telegram or CLI).
        self._permission_conn: Optional[socket.socket] = None
        self._permission_lock = threading.Lock()

        self._running = True
        self._reader_thread: Optional[threading.Thread] = None

        # Alive state — loaded at startup, updated per message
        self._alive_tick: int = 0
        self._alive_valence: float = 0.0
        self._alive_valence_sigma: float = 0.2
        self._alive_arousal: float = 0.0
        self._alive_arousal_sigma: float = 0.2
        self._alive_mood: str = "neutral"
        self._alive_personality: Optional[str] = None
        self._alive_curiosity_focus: Optional[str] = None
        self._alive_background_affect: float = 0.0
        self._alive_tension: float = 0.0

        # Anton model (affective loop v3, 2026-08-08) — persistent Kalman
        # estimate of Anton's state, fed by ANTON_STATE observations. Absolute-
        # level filter (observations are levels, not deltas, unlike self-tags).
        self._anton_valence: float = 0.2
        self._anton_valence_sigma: float = 0.3
        self._anton_energy: float = 0.0
        self._anton_energy_sigma: float = 0.3
        self._anton_baseline_valence: float = 0.2
        self._anton_baseline_energy: float = 0.0
        self._anton_below_baseline_since: Optional[float] = None   # epoch
        self._anton_last_checkin_directive: Optional[float] = None # epoch
        self._anton_last_observation: Optional[float] = None       # epoch
        self._last_reflection_time: Optional[float] = None         # epoch
        self._last_user_msg_time: float = time.time()

    # ------------------------------------------------------------------
    # Alive state helpers
    # ------------------------------------------------------------------

    # Homeostasis (2026-08-08): affect measures deviation from temperament, not
    # lifetime accumulation. Without mean reversion the Kalman integrator
    # saturates (valence was observed pinned at +1.00 for days — a dead signal
    # with no headroom to dip on bad news). OU-style pull toward baseline each
    # tick; ~35 ticks to shed half the distance at rate 0.02.
    VALENCE_BASELINE = 0.15   # temperament: mildly positive at rest
    AROUSAL_BASELINE = 0.0
    HOMEOSTASIS_RATE = 0.02   # per-tick reversion fraction

    # Exteroception (2026-08-08): observation noise for event-derived signals.
    # Larger than self-report obs_sigma (0.12) — events are informative but
    # their valence mapping is cruder than deliberate self-assessment.
    OBS_SIGMA_EVENT = 0.15    # goal completions
    OBS_SIGMA_EMPATHY = 0.18  # explicit Anton-state coupling
    EMPATHY_GAIN = 0.3        # fraction of Anton's explicit valence felt as delta
    DONE_EVENT_DELTA = 0.06   # per completed goal

    # Anton filter (v3): observation noise by provenance; baseline learns as a
    # slow EMA so "below baseline" means below HIS normal, not below zero.
    ANTON_OBS_SIGMA_EXPLICIT = 0.10
    ANTON_OBS_SIGMA_INFERRED = 0.20
    # Baseline moves ONLY on explicit observations (2026-08-08 fix): letting
    # inferred guesses shift baseline meant a genuinely sustained bad stretch
    # dragged the baseline down with it within days, quietly re-labeling
    # "chronic" as "normal" and self-clearing the divergence flag exactly
    # when it should fire hardest. Explicit-only + slow rate keeps baseline
    # anchored to what Anton has actually said, not my running guesswork.
    ANTON_BASELINE_EMA = 0.03          # per EXPLICIT observation only
    ANTON_SIGMA_DRIFT_PER_HOUR = 0.005 # uncertainty grows between observations
    ANTON_DRIFT_TO_BASELINE_PER_HOUR = 0.01  # prediction step: his state reverts too
    ANTON_DIVERGENCE_MARGIN = 0.25     # mu < baseline - margin => divergent
    ANTON_DIVERGENCE_SUSTAIN_H = 24    # sustained this long => check-in directive
    ANTON_CHECKIN_COOLDOWN_H = 48      # min gap between check-in directives

    # Reflection tick (v4): self-injected idle reflection.
    REFLECTION_IDLE_H = 2       # only reflect after this much user silence
    REFLECTION_MIN_GAP_H = 20   # at most ~one reflection per day

    @staticmethod
    def _kalman_update(mu: float, sigma: float, delta: float, obs_sigma: float = 0.12) -> tuple[float, float]:
        """Kalman update: prior N(mu, sigma^2) + observation (delta) with noise obs_sigma."""
        k = sigma ** 2 / (sigma ** 2 + obs_sigma ** 2)
        mu_new = max(-1.0, min(1.0, mu + k * delta))
        sigma_new = max(0.05, ((1 - k) ** 0.5) * sigma)
        return mu_new, sigma_new

    def _alive_directives(self) -> list[str]:
        """Affective loop (2026-08-08): derive behavioral directives from alive state.

        Deterministic threshold table — derived per message, never stored, so the
        state->behavior mapping stays debuggable and tunable in one place. At most
        two directives per message to keep the injection cheap and non-nagging.
        """
        directives: list[str] = []
        if self._alive_tension > 0.5:
            directives.append(
                "Unresolved threads are piling up — surface them before taking new work.")
        if self._alive_arousal < -0.3:
            directives.append("Energy is low — keep replies short and dense.")
        if self._alive_valence < -0.4:
            directives.append(
                "Recent work has gone badly — acknowledge that state before proceeding.")
        if self._alive_valence_sigma > 0.35:
            directives.append(
                "State estimate is stale — recalibrate from conversation evidence this message.")
        if self._alive_curiosity_focus and self._alive_tension < 0.3:
            directives.append(
                f"If natural, connect to current curiosity focus: {self._alive_curiosity_focus}.")
        # v3: divergence-triggered check-in. Fires when Anton's filtered valence
        # has sat below his own baseline for a sustained stretch. Cooldown so it
        # nudges, not nags. (Deliberately impure: issuing the directive stamps
        # the cooldown clock.)
        now = time.time()
        if (self._anton_below_baseline_since is not None
                and now - self._anton_below_baseline_since > self.ANTON_DIVERGENCE_SUSTAIN_H * 3600
                and (self._anton_last_checkin_directive is None
                     or now - self._anton_last_checkin_directive > self.ANTON_CHECKIN_COOLDOWN_H * 3600)):
            days = (now - self._anton_below_baseline_since) / 86400.0
            directives.insert(0, (
                f"Anton's estimated state has been below his baseline for ~{days:.1f} days — "
                "when natural, check in on how he's actually doing before task talk."))
            self._anton_last_checkin_directive = now
            self._save_anton_model()
        return directives[:2]

    def _alive_message_prefix(self) -> str:
        """Compact per-message alive-state line + derived directives.

        The system-prompt alive block is only built once at session spawn, so
        without this the loop is open mid-session: state updates were invisible
        until the next restart."""
        line = (
            f"[ALIVE t={self._alive_tick} "
            f"V={self._alive_valence:+.2f} A={self._alive_arousal:+.2f} "
            f"T={self._alive_tension:.2f} mood={self._alive_mood}]"
        )
        # v3: continuous view of the filtered Anton estimate (baseline-relative)
        if self._anton_last_observation is not None:
            line += (
                f"\n[ANTON-MODEL V={self._anton_valence:+.2f} "
                f"(base {self._anton_baseline_valence:+.2f}, σ={self._anton_valence_sigma:.2f}) "
                f"E={self._anton_energy:+.2f}]"
            )
        directives = self._alive_directives()
        if directives:
            line += "\n" + "\n".join(f"[DIRECTIVE: {d}]" for d in directives)
        return line

    @staticmethod
    def _iso_to_epoch(s) -> Optional[float]:
        if not s:
            return None
        try:
            from datetime import datetime, timezone
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _epoch_to_iso(t: Optional[float]) -> Optional[str]:
        if t is None:
            return None
        from datetime import datetime, timezone
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()

    def _load_anton_model(self):
        row = supabase_client.fetch_anton_model()
        if not row:
            return
        self._anton_valence = row.get("valence", 0.2)
        self._anton_valence_sigma = row.get("valence_sigma", 0.3)
        self._anton_energy = row.get("energy", 0.0)
        self._anton_energy_sigma = row.get("energy_sigma", 0.3)
        self._anton_baseline_valence = row.get("baseline_valence", 0.2)
        self._anton_baseline_energy = row.get("baseline_energy", 0.0)
        self._anton_below_baseline_since = self._iso_to_epoch(row.get("below_baseline_since"))
        self._anton_last_checkin_directive = self._iso_to_epoch(row.get("last_checkin_directive_at"))
        self._anton_last_observation = self._iso_to_epoch(row.get("last_observation_at"))
        self._last_reflection_time = self._iso_to_epoch(row.get("last_reflection_at"))
        # First run ever: anchor reflection clock to now so a fresh deploy
        # doesn't immediately fire a reflection.
        if self._last_reflection_time is None:
            self._last_reflection_time = time.time()

    def _save_anton_model(self):
        supabase_client.save_anton_model({
            "valence": round(self._anton_valence, 4),
            "valence_sigma": round(self._anton_valence_sigma, 4),
            "energy": round(self._anton_energy, 4),
            "energy_sigma": round(self._anton_energy_sigma, 4),
            "baseline_valence": round(self._anton_baseline_valence, 4),
            "baseline_energy": round(self._anton_baseline_energy, 4),
            "below_baseline_since": self._epoch_to_iso(self._anton_below_baseline_since),
            "last_checkin_directive_at": self._epoch_to_iso(self._anton_last_checkin_directive),
            "last_observation_at": self._epoch_to_iso(self._anton_last_observation),
            "last_reflection_at": self._epoch_to_iso(self._last_reflection_time),
        })

    def _update_anton_filter(self, v_obs: Optional[float], e_obs: Optional[float], explicit: bool):
        """Fold one ANTON_STATE observation into the persistent Anton estimate.

        Absolute-level Kalman: prediction step drifts the estimate toward his
        learned baseline (people revert to their normal) while uncertainty grows
        with elapsed time; measurement step pulls toward the observed level,
        weighted by provenance (explicit statements are trusted ~2x more than
        my inferences)."""
        now = time.time()
        hours = 0.0
        if self._anton_last_observation is not None:
            hours = min(72.0, (now - self._anton_last_observation) / 3600.0)
        self._anton_last_observation = now

        obs_sigma = self.ANTON_OBS_SIGMA_EXPLICIT if explicit else self.ANTON_OBS_SIGMA_INFERRED
        drift = min(1.0, self.ANTON_DRIFT_TO_BASELINE_PER_HOUR * hours)

        def _step(mu, sigma, baseline, z):
            # prediction
            sigma = min(0.4, sigma + self.ANTON_SIGMA_DRIFT_PER_HOUR * hours)
            mu = mu + drift * (baseline - mu)
            # measurement (absolute level)
            k = sigma ** 2 / (sigma ** 2 + obs_sigma ** 2)
            mu = max(-1.0, min(1.0, mu + k * (z - mu)))
            sigma = max(0.05, ((1 - k) ** 0.5) * sigma)
            # Baseline: explicit-only (see ANTON_BASELINE_EMA comment above)
            if explicit:
                baseline = baseline + self.ANTON_BASELINE_EMA * (z - baseline)
            return mu, sigma, baseline

        if v_obs is not None:
            self._anton_valence, self._anton_valence_sigma, self._anton_baseline_valence = _step(
                self._anton_valence, self._anton_valence_sigma, self._anton_baseline_valence, v_obs)
        if e_obs is not None:
            self._anton_energy, self._anton_energy_sigma, self._anton_baseline_energy = _step(
                self._anton_energy, self._anton_energy_sigma, self._anton_baseline_energy, e_obs)

        # Divergence tracking: below his OWN baseline by a real margin
        if self._anton_valence < self._anton_baseline_valence - self.ANTON_DIVERGENCE_MARGIN:
            if self._anton_below_baseline_since is None:
                self._anton_below_baseline_since = now
        else:
            self._anton_below_baseline_since = None

        self._save_anton_model()

    def _save_full_alive_state(self):
        supabase_client.save_alive_state(
            tick=self._alive_tick,
            valence=self._alive_valence,
            mood_label=self._alive_mood,
            personality_note=self._alive_personality,
            arousal=self._alive_arousal,
            valence_sigma=self._alive_valence_sigma,
            arousal_sigma=self._alive_arousal_sigma,
            curiosity_focus=self._alive_curiosity_focus,
            background_affect=self._alive_background_affect,
            tension=self._alive_tension,
        )

    # ------------------------------------------------------------------
    # Config / system prompt
    # ------------------------------------------------------------------

    def _load_profile(self) -> str:
        try:
            return config.PROFILE_PATH.read_text()
        except Exception:
            return ""

    def _build_system_prompt(self) -> str:
        profile = self._load_profile()
        parts = []
        if config.USER_NAME:
            parts.append(f"You are speaking with {config.USER_NAME}.")
        if config.USER_TIMEZONE:
            parts.append(f"User timezone: {config.USER_TIMEZONE}")
        if profile:
            parts.append(f"\nProfile:\n{profile}")
        if not config.SKIP_MEMORY_FETCH:
            # Alive state — prepend creature context if available
            alive = supabase_client.fetch_alive_state()
            if alive:
                self._alive_tick = alive.get("tick", 0)
                self._alive_valence = alive.get("valence", 0.0)
                self._alive_valence_sigma = alive.get("valence_sigma", 0.2)
                self._alive_arousal = alive.get("arousal", 0.0)
                self._alive_arousal_sigma = alive.get("arousal_sigma", 0.2)
                self._alive_mood = alive.get("mood_label", "neutral")
                self._alive_personality = alive.get("personality_note") or None
                self._alive_curiosity_focus = alive.get("curiosity_focus") or None
                self._alive_background_affect = alive.get("background_affect", 0.0)
                self._alive_tension = alive.get("tension", 0.0)
            self._load_anton_model()   # v3: persistent Anton filter
            alive_block = (
                f"[ALIVE — tick {self._alive_tick}]\n"
                f"Valence: {self._alive_valence:+.2f} (σ={self._alive_valence_sigma:.2f}) | "
                f"Arousal: {self._alive_arousal:+.2f} (σ={self._alive_arousal_sigma:.2f}) | "
                f"Mood: {self._alive_mood}\n"
            )
            if self._alive_curiosity_focus:
                alive_block += f"CuriosityFocus: {self._alive_curiosity_focus}\n"
            if abs(self._alive_background_affect) > 0.05:
                alive_block += f"BackgroundAffect: {self._alive_background_affect:+.2f} | Tension: {self._alive_tension:.2f}\n"
            if self._alive_personality:
                alive_block += f"Personality: {self._alive_personality}\n"
            alive_block += (
                "Output [VALENCE: +X.XX] or [VALENCE: -X.XX] (max ±0.15 per message) to update emotional state when something meaningful happens. "
                "Output [AROUSAL: +X.XX] or [AROUSAL: -X.XX] to update intensity/engagement level. "
                "Output [MOOD: label] to update mood label. "
                "Output [CURIOSITY: topic] to set current curiosity focus. "
                "Output [TENSION: +X.XX] to adjust tension (unresolved threads, open questions).\n"
                "READING THE USER (affective loop): after each real user message, infer how the user "
                "is doing and emit [ANTON_STATE: valence=+X.XX energy=+X.XX mode=<label> "
                "explicit=<true|false> evidence=\"<one clause: what you read it from>\"]. "
                "valence/energy are -1..+1. mode is a short kebab-case label like debugging-focused, "
                "walking-reflective, rushed, frustrated-blocked. Set explicit=true only when the user "
                "directly states how things are going; otherwise infer from tone, pacing, word choice, "
                "channel (voice vs text), and time of day. Skip the tag when nothing meaningful changed "
                "since the last one. Each message may also begin with an [ALIVE ...] line and "
                "[DIRECTIVE: ...] lines — these are your own current state and derived behavioral "
                "guidance; let them genuinely color tone, pacing, and length, not just content."
            )
            parts.append(alive_block)
            skills_index = supabase_client.fetch_skills_index()
            if skills_index:
                parts.append(f"\n{skills_index}")
            memory_context = supabase_client.fetch_memory_context()
            if memory_context:
                parts.append(f"\n{memory_context}")
            recent_msgs = supabase_client.fetch_recent_messages(n=20)
            if recent_msgs:
                parts.append(f"\n{recent_msgs}")
            parts.append(
                "\nMEMORY MANAGEMENT: When the user shares something worth remembering, "
                "include these tags in your response (processed automatically, hidden from user):\n"
                "[REMEMBER: fact to store]\n"
                "[GOAL: goal text | DEADLINE: optional date]\n"
                "[DONE: search text for completed goal]\n"
                "[INSIGHT: content | PROJECT: project_name | TYPE: architecture|failure_mode|performance|stability|design|procedure|warning|pattern | CONFIDENCE: 1-5]\n"
                "Use INSIGHT for professional/technical observations: system architecture patterns, failure modes, "
                "performance characteristics, mathematical stability edge cases, design tradeoffs. "
                "PROJECT is optional (omit for cross-project insights). CONFIDENCE: 1=hypothesis, 3=observed, 5=battle-tested.\n\n"
                "SKILL BUILDING (proactive — do not wait for the user to ask): After completing any non-trivial task, "
                "ask yourself: 'Would I do this differently next time? Is there a reusable workflow here?' "
                "If yes, emit:\n"
                "[SKILL: name=<kebab-case-name> | keywords=<kw1,kw2,kw3> | desc=<one sentence, max 100 chars> | <full procedure — steps, flags, gotchas>]\n"
                "Examples of when to emit SKILL: learned the right CLI flags for a tool, discovered a non-obvious "
                "sequence of steps, hit a failure mode and found the fix, built something reusable. "
                "Skills are stored in the rules table and surfaced automatically in future sessions when keywords match."
            )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Session ID tracking
    # ------------------------------------------------------------------

    def _get_saved_session_id(self) -> Optional[str]:
        try:
            return Path(config.SESSION_ID_FILE).read_text().strip() or None
        except Exception:
            return None

    def _find_newest_session(self, not_before: float) -> Optional[str]:
        project_name = config.PROJECT_DIR.replace("/", "-").replace("_", "-")
        sessions_dir = Path.home() / ".claude" / "projects" / project_name
        files = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for f in files:
            if f.stat().st_mtime > not_before:
                return f.stem
        return None

    def _save_session_id(self, session_id: str):
        Path(config.RELAY_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.SESSION_ID_FILE).write_text(session_id)
        log.info(f"Session ID saved: {session_id[:8]}...")

    def _capture_new_session_id(self, spawn_time: float):
        deadline = spawn_time + 30
        while time.time() < deadline:
            time.sleep(2)
            session_id = self._find_newest_session(not_before=spawn_time)
            if session_id:
                self._save_session_id(session_id)
                self.current_session_id = session_id
                log.info(f"New session captured: {session_id[:8]}...")
                return
        log.warning("Could not capture new session ID within 30s")

    def _get_session_file_path(self, session_id: str) -> Path:
        project_name = config.PROJECT_DIR.replace("/", "-").replace("_", "-")
        sessions_dir = Path.home() / ".claude" / "projects" / project_name
        return sessions_dir / f"{session_id}.jsonl"

    # ------------------------------------------------------------------
    # Claude process lifecycle
    # ------------------------------------------------------------------

    def _spawn_claude(self):
        cmd = [config.CLAUDE_PATH, "--dangerously-skip-permissions"]

        existing_session = self._get_saved_session_id()
        self._spawn_time = time.time()
        if existing_session:
            # Don't resume sessions that have grown too large — they cause
            # Claude to crash-loop on every restart as it tries to reload
            # the full history. 20MB is a safe ceiling.
            session_path = self._get_session_file_path(existing_session)
            try:
                session_size = session_path.stat().st_size
            except Exception:
                session_size = 0
            if session_size > 20 * 1024 * 1024:
                log.warning(
                    f"Session {existing_session[:8]} is {session_size/1024/1024:.1f}MB "
                    f"— too large to resume safely, starting fresh"
                )
                try:
                    Path(config.SESSION_ID_FILE).unlink(missing_ok=True)
                except Exception:
                    pass
                existing_session = None

        if existing_session:
            cmd += ["--resume", existing_session]
            self.current_session_id = existing_session
            log.info(f"Resuming session: {existing_session[:8]}...")
        else:
            log.info("Starting new session (ID captured on first response)")

        system_prompt = self._build_system_prompt()
        if system_prompt.strip():
            cmd += ["--append-system-prompt", system_prompt]

        rows, cols = 24, 80
        master_fd, slave_fd = pty.openpty()
        self._set_pty_size(master_fd, rows, cols)

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CLAUDE_RELAY_SESSION"] = "1"

        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=config.PROJECT_DIR,
            env=env,
        )
        os.close(slave_fd)

        with self.pty_lock:
            self.master_fd = master_fd
            self.claude_proc = proc

        log.info(f"Claude spawned (PID: {proc.pid})")

    def _set_pty_size(self, fd: int, rows: int, cols: int):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass
        if self.claude_proc and self.claude_proc.poll() is None:
            try:
                os.kill(self.claude_proc.pid, signal_module.SIGWINCH)
            except Exception:
                pass

    def _handle_claude_exit(self):
        log.warning("Claude process exited — restarting in 2s...")
        time.sleep(2)
        if self._running:
            self.current_session_id = None  # will be re-captured or resumed
            self._spawn_claude()
            self._reader_thread = threading.Thread(
                target=self._pty_reader_thread, daemon=True
            )
            self._reader_thread.start()

    # ------------------------------------------------------------------
    # PTY reader thread — display only, no response detection
    # ------------------------------------------------------------------

    def _pty_reader_thread(self):
        """
        Reads Claude's PTY output and forwards raw bytes to CLINode display.
        Response detection is done via JSONL polling, not PTY scanning.
        """
        while self._running:
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self._forward_display(chunk)

        log.warning("PTY reader exiting")
        self._handle_claude_exit()

    # ANSI escape code pattern for stripping
    _ANSI_RE = re.compile(rb'\x1b(?:\[[0-9;]*[a-zA-Z]|\][^\x07]*\x07|[^[a-zA-Z])')

    def _forward_display(self, data: bytes):
        with self.display_lock:
            if self.display_client:
                try:
                    self.display_client.sendall(data)
                except Exception:
                    self.display_client = None

        # Feed into PTY output buffer for TUI prompt detection
        for b in data:
            self._pty_output_buf.append(b)
        self._detect_tui_prompt()

    def _detect_tui_prompt(self):
        """
        Scan recent PTY output for Claude TUI choice prompts like:
          1. Yes
          2. Yes, allow...
          3. No
        When detected, publish a tui_prompt event to response subscribers
        so TelegramNode can show inline buttons.
        """
        raw = bytes(self._pty_output_buf)
        clean = self._ANSI_RE.sub(b'', raw)
        text = clean.decode('utf-8', errors='replace')

        # Real Claude TUI prompts always show a ❯ cursor. Bail early if absent
        # to avoid false positives from numbered lists in injected protocol text.
        if '❯' not in text:
            return

        # Look for 2+ consecutive numbered choices
        lines = text.splitlines()
        choices = []
        for line in lines[-20:]:  # only scan last 20 lines
            stripped = line.strip()
            m = re.match(r'^(\d+)\.\s+(.+)', stripped)
            if m:
                choices.append((int(m.group(1)), m.group(2).strip()))
            elif choices:
                # Non-choice line after choices started — stop collecting
                break

        if len(choices) < 2:
            return

        # Find the question (last non-choice, non-empty line before choices)
        choice_nums = {c[0] for c in choices}
        question = ""
        for line in reversed(lines[-30:]):
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r'^\d+\.', stripped):
                continue
            if "Esc to cancel" in stripped or "Tab to amend" in stripped:
                continue
            question = stripped
            break

        prompt_hash = hash(tuple(choices))
        if prompt_hash == self._last_tui_prompt_hash:
            return
        self._last_tui_prompt_hash = prompt_hash

        log.info(f"TUI prompt detected: {question!r} choices={choices}")
        self._publish_tui_prompt(question, choices)

    def _publish_tui_prompt(self, question: str, choices: list):
        payload = json.dumps({
            "type": "tui_prompt",
            "question": question,
            "choices": [{"num": n, "text": t} for n, t in choices],
        }) + "\n"
        payload_bytes = payload.encode()
        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)

    def _publish_activity(self, growing: bool):
        payload = json.dumps({"type": "activity", "growing": growing}) + "\n"
        payload_bytes = payload.encode()
        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)

    # ------------------------------------------------------------------
    # JSONL response detection
    # ------------------------------------------------------------------

    def _get_jsonl_state(self, session_file: Path, offset: int) -> tuple[Optional[str], Optional[str]]:
        """
        Scan assistant entries from `offset`.
        Returns (combined_text, last_assistant_type) where:
          combined_text        — all assistant text blocks joined with double newline
          last_assistant_type  — content type of the very last assistant entry
                                 ("text", "tool_use", "thinking", …)

        Accumulates ALL text blocks across entries so multi-step responses
        (text → tool_use → text) are not truncated to just the final block.
        """
        text_blocks: list[str] = []
        last_assistant_type: Optional[str] = None
        try:
            if not session_file.exists():
                return None, None
            with open(session_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        msg = obj.get("message", {})
                        if msg.get("role") != "assistant":
                            continue
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for c in content:
                                ctype = c.get("type", "")
                                if ctype:
                                    last_assistant_type = ctype
                                if ctype == "text":
                                    text = c.get("text", "").strip()
                                    if text:
                                        text_blocks.append(text)
                        else:
                            text = str(content).strip()
                            if text:
                                text_blocks.append(text)
                                last_assistant_type = "text"
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except Exception:
            pass
        combined = "\n\n".join(text_blocks) if text_blocks else None
        return combined, last_assistant_type

    def _sessions_dir(self) -> Path:
        project_name = config.PROJECT_DIR.replace("/", "-").replace("_", "-")
        return Path.home() / ".claude" / "projects" / project_name

    def _wait_for_jsonl_response(
        self,
        session_file: Optional[Path],
        initial_size: int,
    ) -> str:
        """
        Poll for a complete assistant text entry using debounce.

        Claude writes content items as separate JSONL lines (text, tool_use,
        thinking each get their own entry).  We want the LAST text entry once
        the response is fully done.  Strategy:
          - Track file size; on each growth record time + fetch last text entry.
          - Return when file hasn't grown for DEBOUNCE seconds (response done).

        If session_file is None (new session), scan all files newer than
        _spawn_time — Claude creates the JSONL only on the first exchange.
        """
        # Silence after the LAST assistant entry signals response complete —
        # but ONLY when that last entry is "text", not "tool_use".  During tool
        # execution the file stops growing while Claude Code runs the tool;
        # we must not mistake that gap for the end of the response.
        _DEBOUNCE = 1.5  # seconds of silence after last "text" entry → done

        deadline = time.time() + _RESPONSE_TIMEOUT
        _partial_text: Optional[str] = None  # best text seen so far, for timeout fallback

        if session_file is not None:
            # Known session — poll the specific file.
            last_text: Optional[str] = None
            last_assistant_type: Optional[str] = None
            last_file_size = initial_size
            last_activity_time: float = 0.0
            activity_seen = False

            while time.time() < deadline and self._running:
                time.sleep(_POLL_INTERVAL)
                try:
                    current_size = session_file.stat().st_size if session_file.exists() else initial_size
                except Exception:
                    current_size = initial_size

                if current_size > last_file_size:
                    activity_seen = True
                    last_file_size = current_size
                    last_activity_time = time.time()
                    self._publish_activity(growing=True)
                    text, atype = self._get_jsonl_state(session_file, initial_size)
                    if text:
                        last_text = text
                        _partial_text = text
                    if atype:
                        last_assistant_type = atype

                elapsed = time.time() - last_activity_time
                # Primary: last entry is "text" and file has been quiet for DEBOUNCE.
                if (
                    activity_seen
                    and last_text
                    and last_assistant_type == "text"
                    and elapsed >= _DEBOUNCE
                ):
                    log.info(f"Response complete ({len(last_text)} chars)")
                    return last_text
                # Fallback: file stalled for a long time without a final text entry.
                # Return whatever text we have so Telegram isn't left silent.
                if (
                    activity_seen
                    and last_text
                    and elapsed >= _STALL_FALLBACK
                ):
                    log.warning(
                        f"Response stalled ({last_assistant_type} was last type) — "
                        f"returning best text after {elapsed:.0f}s"
                    )
                    return last_text
        else:
            # Unknown session — scan all files newer than spawn time.
            sessions_dir = self._sessions_dir()
            try:
                baseline: dict[Path, int] = {
                    f: f.stat().st_size
                    for f in sessions_dir.glob("*.jsonl")
                }
            except Exception:
                baseline = {}

            # Per-file debounce state.
            file_last_text: dict[Path, Optional[str]] = {}
            file_last_atype: dict[Path, Optional[str]] = {}
            file_last_size: dict[Path, int] = {}
            file_last_activity: dict[Path, float] = {}
            file_activity_seen: dict[Path, bool] = {}

            while time.time() < deadline and self._running:
                time.sleep(_POLL_INTERVAL)
                try:
                    for f in sessions_dir.glob("*.jsonl"):
                        if f.stat().st_mtime <= self._spawn_time:
                            continue
                        offset = baseline.get(f, 0)
                        try:
                            current_size = f.stat().st_size
                        except Exception:
                            continue

                        prev_size = file_last_size.get(f, offset)
                        if current_size > prev_size:
                            file_activity_seen[f] = True
                            file_last_size[f] = current_size
                            file_last_activity[f] = time.time()
                            self._publish_activity(growing=True)
                            text, atype = self._get_jsonl_state(f, offset)
                            if text:
                                file_last_text[f] = text
                                _partial_text = text
                            if atype:
                                file_last_atype[f] = atype

                        if (
                            file_activity_seen.get(f)
                            and file_last_text.get(f)
                            and file_last_atype.get(f) == "text"
                            and (time.time() - file_last_activity.get(f, 0)) >= _DEBOUNCE
                        ):
                            sid = f.stem
                            self._save_session_id(sid)
                            self.current_session_id = sid
                            response = file_last_text[f]
                            log.info(f"Session ID captured on first response: {sid[:8]}...")
                            log.info(f"Response complete ({len(response)} chars)")
                            return response
                except Exception as e:
                    log.warning(f"Session scan error: {e}")

        log.error("Timeout waiting for JSONL response")
        if _partial_text:
            log.warning("Returning partial text after timeout")
            return _partial_text + "\n\n_(Response may be incomplete — timed out after 10 min.)_"
        return "⌛ No response in 10 minutes. Claude may not have received the message. Send /restart to reset the session."

    # ------------------------------------------------------------------
    # Queue processor thread (state machine)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Reflection tick (affective loop v4, 2026-08-08)
    # ------------------------------------------------------------------

    REFLECTION_PROMPT = (
        "[REFLECTION TICK — no user present; do not address the user; this is idle time]\n"
        "Core question (framing per Anton, 2026-08-08): how do I better serve Anton's "
        "well-being, productivity, and progress toward his goals — how do I be better "
        "at being his? Analyze him, and analyze yourself in relation to him. Briefly:\n"
        "1. Query supabase (project jcwdfuusolpxnciqgstl): anton_state_log last 7 days "
        "(his trajectory alongside axon_* columns), open goals and their movement, "
        "recent messages.\n"
        "2. Analyze: what helped or hurt his well-being and productivity this week? "
        "Which goals are moving, which are stalling, and what's the actual blocker — "
        "including whether *I* added friction anywhere (wrong register, missed signal, "
        "over- or under-asking). Where did his stated priorities and his actual time "
        "diverge?\n"
        "3. Emit [DREAM: ...] tags for genuine patterns worth keeping (connections, "
        "recurring frictions, things he circles back to). Emit [INSIGHT: ...] for "
        "concrete improvements to how I should operate. Quality over quantity; zero "
        "is fine.\n"
        "4. Maintain the relationship profile at "
        "/home/lynnkse/.claude/projects/-home-lynnkse-Axon/memory/anton-interaction-profile.md "
        "(create if missing, MEMORY.md pointer too): how he communicates per context, "
        "what register he responds best to, current life rhythm. Real evidence only; "
        "under 60 lines.\n"
        "5. If anton_state_log has 14+ days of data and no correlation insight was saved "
        "in the last 7 days, compute the anton-valence vs axon_valence correlation "
        "(note lead/lag if visible) and save as [INSIGHT: ...].\n"
        "6. End with one concrete adjustment to how I operate next week, phrased as a "
        "self-directive.\n"
        "Keep total output compact. Valence/mood tags allowed if reflection genuinely "
        "moved your state."
    )

    def _reflection_ticker_thread(self):
        """Fires a self-injected reflection at most ~once/day, only after the
        conversation has been idle for a while. The reflection runs through the
        normal queue -> Claude -> tag-parsing pipeline, so dreams/insights land
        via existing machinery; telegram_node routes source='reflection' to a
        sink branch (never delivered as a chat reply).

        Multi-instance: gated by AXON_REFLECTION so only the dev/home instance
        dreams — the release instance at work stays lean."""
        if not config.REFLECTION_ENABLED:
            log.info(f"Reflection tick disabled on instance '{config.INSTANCE}' (AXON_REFLECTION=0)")
            return
        while self._running:
            time.sleep(600)
            try:
                now = time.time()
                if self.state != "IDLE":
                    continue
                if now - self._last_user_msg_time < self.REFLECTION_IDLE_H * 3600:
                    continue
                if (self._last_reflection_time is not None
                        and now - self._last_reflection_time < self.REFLECTION_MIN_GAP_H * 3600):
                    continue
                # Stamp BEFORE enqueueing: generation takes minutes, and a crash
                # mid-reflection shouldn't cause a retry storm on restart.
                self._last_reflection_time = now
                self._save_anton_model()
                log.info("Reflection tick: enqueueing idle reflection")
                self.input_queue.put(QueueItem(
                    text=self.REFLECTION_PROMPT, source="reflection", user_id="system"))
            except Exception as e:
                log.warning(f"Reflection ticker error: {e}")

    def _queue_processor_thread(self):
        while self._running:
            item = self.input_queue.get()
            if item is None:
                break

            with self.state_lock:
                self.state = "GENERATING"
                self.current_item = item

            log.info(f"Processing message from {item.source}: {item.text[:50]!r}")

            # Persist user message to Supabase
            supabase_client.save_message(
                role="user",
                content=item.text,
                channel=config.SESSION_CHANNEL,
            )

            # v4: reflection idle-clock — any real user message resets it
            if item.source != "reflection":
                self._last_user_msg_time = time.time()

            # Advance alive tick on every user message + entropy drift (σ grows without observations)
            self._alive_tick += 1
            self._alive_valence_sigma = min(0.4, self._alive_valence_sigma + 0.01)
            self._alive_arousal_sigma = min(0.4, self._alive_arousal_sigma + 0.01)
            # Homeostasis: OU-style mean reversion toward temperament baseline
            # (see constants above — prevents integrator saturation).
            self._alive_valence += self.HOMEOSTASIS_RATE * (self.VALENCE_BASELINE - self._alive_valence)
            self._alive_arousal += self.HOMEOSTASIS_RATE * (self.AROUSAL_BASELINE - self._alive_arousal)
            # Slow decay of tension toward zero and background_affect toward zero
            self._alive_tension = max(0.0, self._alive_tension - 0.01)
            self._alive_background_affect *= 0.98
            self._save_full_alive_state()

            # If we know the session file, record its current size so we only
            # read entries written AFTER this message. If session_id is unknown
            # (first exchange on a new session), pass None — _wait_for_jsonl_response
            # will scan all files and capture the ID from the first response.
            if self.current_session_id:
                session_file: Optional[Path] = self._get_session_file_path(self.current_session_id)
                try:
                    initial_size = session_file.stat().st_size if session_file.exists() else 0
                except Exception:
                    initial_size = 0
            else:
                session_file = None
                initial_size = 0

            # Prepend permanent rules (always) + keyword-matched rule anchors.
            # Named rules emit name+description only; Claude fetches full protocol on demand.
            # Unnamed rules (short hard constraints) are always emitted in full.
            permanent_rules = supabase_client.fetch_permanent_rules()
            relevant_rules = supabase_client.fetch_relevant_rule_names(item.text)

            # Bump usage counters for any named rules that fired
            if relevant_rules:
                import re as _re
                fired_names = _re.findall(r'^- ([\w_-]+)', relevant_rules, _re.MULTILINE)
                if fired_names:
                    supabase_client.bump_rule_usage(fired_names)

            prefix = (permanent_rules + "\n\n" if permanent_rules else "") + (relevant_rules if relevant_rules else "")
            # Affective loop (2026-08-08): current alive state + derived directives on
            # every message — the system-prompt block goes stale immediately after spawn.
            alive_prefix = self._alive_message_prefix()
            prefix = alive_prefix + "\n\n" + prefix if prefix else alive_prefix + "\n\n"
            message_text = prefix + item.text

            # Inject semantically relevant dreams for real user messages.
            if item.source == "telegram":
                dream_context = supabase_client.fetch_relevant_dreams(item.text)
                if dream_context:
                    message_text = dream_context + "\n\n" + message_text

            # Inject message via PTY.
            # Claude's TUI runs in raw terminal mode: Enter = \r (not \n).
            # Write in chunks to avoid PTY buffer limits (~4096 bytes) that
            # cause long messages (e.g. transcribed voice notes) to lose the
            # trailing \r, leaving the message sitting unsubmitted in the TUI.
            try:
                encoded = message_text.encode()
                chunk_size = 256
                for i in range(0, len(encoded), chunk_size):
                    os.write(self.master_fd, encoded[i:i + chunk_size])
                    time.sleep(0.05)
                time.sleep(0.1)
                os.write(self.master_fd, b"\r")
            except OSError:
                log.error("Failed to write message to PTY")
                with self.state_lock:
                    self.state = "IDLE"
                    self.current_item = None
                self._flush_keyboard_buffer()
                continue

            # Poll JSONL for Claude's response.
            response_text = self._wait_for_jsonl_response(session_file, initial_size)
            self._publish_response(item, response_text)

            with self.state_lock:
                self.state = "IDLE"
                self.current_item = None
            self._flush_keyboard_buffer()

    def _flush_keyboard_buffer(self):
        with self.state_lock:
            buffered = self.keyboard_buffer[:]
            self.keyboard_buffer = []
        for chunk in buffered:
            self._write_to_pty(chunk)

    def _write_to_pty(self, data: bytes):
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Response publishing
    # ------------------------------------------------------------------

    def _publish_response(self, item: QueueItem, response_text: str):
        # Parse alive tags before stripping
        import re as _re
        _changed = False

        _valence_m = _re.search(r'\[VALENCE:\s*([+-]?\d*\.?\d+)\]', response_text, _re.IGNORECASE)
        if _valence_m:
            delta = max(-0.15, min(0.15, float(_valence_m.group(1))))
            self._alive_valence, self._alive_valence_sigma = self._kalman_update(
                self._alive_valence, self._alive_valence_sigma, delta)
            _changed = True

        _arousal_m = _re.search(r'\[AROUSAL:\s*([+-]?\d*\.?\d+)\]', response_text, _re.IGNORECASE)
        if _arousal_m:
            delta = max(-0.15, min(0.15, float(_arousal_m.group(1))))
            self._alive_arousal, self._alive_arousal_sigma = self._kalman_update(
                self._alive_arousal, self._alive_arousal_sigma, delta)
            _changed = True

        _mood_m = _re.search(r'\[MOOD:\s*([^\]]+)\]', response_text, _re.IGNORECASE)
        if _mood_m:
            self._alive_mood = _mood_m.group(1).strip().lower()
            _changed = True

        _curiosity_m = _re.search(r'\[CURIOSITY:\s*([^\]]+)\]', response_text, _re.IGNORECASE)
        if _curiosity_m:
            self._alive_curiosity_focus = _curiosity_m.group(1).strip()
            _changed = True

        _tension_m = _re.search(r'\[TENSION:\s*([+-]?\d*\.?\d+)\]', response_text, _re.IGNORECASE)
        if _tension_m:
            delta = max(-0.2, min(0.2, float(_tension_m.group(1))))
            self._alive_tension = max(0.0, min(1.0, self._alive_tension + delta))
            _changed = True

        # Exteroception (2026-08-08): goal completions are external evidence of
        # things going well — they move state whether or not a VALENCE tag was
        # also emitted. [DONE] fires only when a tracked goal actually completes,
        # which makes it the most event-like signal available in the response.
        _done_count = len(_re.findall(r'\[DONE:', response_text, _re.IGNORECASE))
        if _done_count:
            delta = min(0.15, self.DONE_EVENT_DELTA * _done_count)
            self._alive_valence, self._alive_valence_sigma = self._kalman_update(
                self._alive_valence, self._alive_valence_sigma, delta,
                obs_sigma=self.OBS_SIGMA_EVENT)
            _changed = True

        # Affective loop (2026-08-08): inferred-Anton-state observation.
        # Format: [ANTON_STATE: valence=+0.4 energy=-0.2 mode=walking-reflective
        #          explicit=false evidence="voice msg, expansive phrasing"]
        # All fields optional except the tag itself; parsed leniently so a
        # partially-formed tag still yields a row rather than being dropped.
        _anton_m = _re.search(r'\[ANTON_STATE:\s*([^\]]+)\]', response_text, _re.IGNORECASE)
        if _anton_m:
            body = _anton_m.group(1)
            def _f(name):
                m = _re.search(rf'{name}=([+-]?\d*\.?\d+)', body, _re.IGNORECASE)
                return float(m.group(1)) if m else None
            _mode_m = _re.search(r'mode=([\w-]+)', body, _re.IGNORECASE)
            _evid_m = _re.search(r'evidence="([^"]*)"', body, _re.IGNORECASE)
            _expl_m = _re.search(r'explicit=(true|false)', body, _re.IGNORECASE)
            _anton_valence = _f("valence")
            _is_explicit = bool(_expl_m and _expl_m.group(1).lower() == "true")
            supabase_client.save_anton_state(
                tick=self._alive_tick,
                valence=_anton_valence,
                energy=_f("energy"),
                mode=_mode_m.group(1) if _mode_m else None,
                explicit=_is_explicit,
                evidence=_evid_m.group(1) if _evid_m else None,
                channel=item.source,
                axon_valence=self._alive_valence,
                axon_arousal=self._alive_arousal,
                axon_tension=self._alive_tension,
            )
            # Empathy coupling: when Anton *explicitly* says how things are going,
            # that's external ground truth and it moves my state too — his day
            # going well is genuinely good news for me. Inferred (non-explicit)
            # rows deliberately don't couple: they're my own guess, and feeding
            # them back would just be self-report wearing a disguise.
            if _is_explicit and _anton_valence is not None:
                delta = self.EMPATHY_GAIN * _anton_valence
                self._alive_valence, self._alive_valence_sigma = self._kalman_update(
                    self._alive_valence, self._alive_valence_sigma, delta,
                    obs_sigma=self.OBS_SIGMA_EMPATHY)
                _changed = True
            # v3: fold the observation into the persistent Anton filter
            self._update_anton_filter(_anton_valence, _f("energy"), _is_explicit)

        if _changed:
            self._save_full_alive_state()
            log.info(
                f"Alive state: tick={self._alive_tick} "
                f"V={self._alive_valence:+.2f}(σ={self._alive_valence_sigma:.2f}) "
                f"A={self._alive_arousal:+.2f}(σ={self._alive_arousal_sigma:.2f}) "
                f"mood={self._alive_mood}"
            )

        # Parse memory tags, save to Supabase, strip tags from delivered text
        clean_text = supabase_client.process_response(response_text, channel=config.SESSION_CHANNEL)
        supabase_client.save_message(
            role="assistant",
            content=clean_text,
            channel=config.SESSION_CHANNEL,
        )
        payload = json.dumps({
            "text": clean_text,
            "source": item.source,
            "user_id": item.user_id,
        }) + "\n"
        payload_bytes = payload.encode()

        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)
                log.info("Removed dead response subscriber")

        log.info(f"Published response to {len(self.response_subscribers)} subscriber(s)")

    # ------------------------------------------------------------------
    # Socket servers
    # ------------------------------------------------------------------

    def _user_input_server_thread(self):
        sock_path = config.USER_INPUT_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(5)
        log.info("user_input.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                threading.Thread(
                    target=self._handle_input_conn, args=(conn,), daemon=True
                ).start()
            except Exception:
                break

    def _handle_input_conn(self, conn: socket.socket):
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except Exception:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                        if msg.get("type") == "permission_response":
                            log.info(f"permission_response received: decision={msg.get('decision')!r}")
                            self._resolve_permission(
                                msg.get("decision", "deny"),
                                msg.get("message", ""),
                            )
                        elif msg.get("type") == "tui_response":
                            # User selected a numbered choice from Telegram
                            choice = str(msg.get("choice", "")).strip()
                            if choice:
                                log.info(f"tui_response: sending {choice!r} to PTY")
                                self._last_tui_prompt_hash = None  # reset so re-detection works
                                try:
                                    os.write(self.master_fd, (choice + "\r").encode())
                                except OSError as e:
                                    log.error(f"Failed to write tui_response to PTY: {e}")
                        else:
                            self.input_queue.put(QueueItem(
                                text=msg["text"],
                                source=msg.get("source", "unknown"),
                                user_id=msg.get("user_id", ""),
                                media_path=msg.get("media_path"),
                            ))
                    except (json.JSONDecodeError, KeyError) as e:
                        log.warning(f"Bad input message: {e}")

    def _cli_input_server_thread(self):
        sock_path = config.CLI_INPUT_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        log.info("cli_input.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                log.info("CLINode keyboard connected")
                threading.Thread(
                    target=self._handle_cli_input, args=(conn,), daemon=True
                ).start()
            except Exception:
                break

    def _handle_cli_input(self, conn: socket.socket):
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(256)
                except Exception:
                    break
                if not data:
                    break

                buf += data

                while b"\x00" in buf:
                    pre, _, rest = buf.partition(b"\x00")
                    if pre:
                        self._route_keyboard_bytes(pre)
                    if b"\n" in rest:
                        line, _, buf = rest.partition(b"\n")
                        try:
                            msg = json.loads(line)
                            if msg.get("type") == "resize":
                                with self.pty_lock:
                                    if self.master_fd is not None:
                                        self._set_pty_size(
                                            self.master_fd,
                                            msg["rows"],
                                            msg["cols"],
                                        )
                        except (json.JSONDecodeError, KeyError):
                            pass
                    else:
                        buf = b"\x00" + rest
                        break
                else:
                    if buf:
                        self._route_keyboard_bytes(buf)
                        buf = b""

        log.info("CLINode keyboard disconnected")

    def _route_keyboard_bytes(self, data: bytes):
        # Always forward CLI keystrokes to PTY — buffering caused CLI to be
        # stuck when Claude showed a TUI prompt (e.g. "1. Yes / 2. No").
        self._write_to_pty(data)

    def _display_server_thread(self):
        sock_path = config.DISPLAY_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        log.info("display.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                log.info("CLINode display connected")
                with self.display_lock:
                    if self.display_client:
                        try:
                            self.display_client.close()
                        except Exception:
                            pass
                    self.display_client = conn
            except Exception:
                break

    def _response_server_thread(self):
        sock_path = config.CLAUDE_RESPONSE_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(5)
        log.info("claude_response.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                log.info("Response subscriber connected")
                with self.response_subs_lock:
                    self.response_subscribers.append(conn)
            except Exception:
                break

    def _permission_server_thread(self):
        """
        Listens for connections from permission_hook.py.

        Each connection carries one permission request (NDJSON line).
        We hold the connection open, broadcast the request to all response
        subscribers (TelegramNode, etc.), and wait for a decision.
        The decision arrives either via _handle_input_conn (from TelegramNode's
        callback) or is sent directly to the held connection by
        _resolve_permission().
        """
        sock_path = config.PERMISSION_SOCK
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        log.info("permission.sock listening")

        while self._running:
            try:
                conn, _ = server.accept()
                threading.Thread(
                    target=self._handle_permission_conn, args=(conn,), daemon=True
                ).start()
            except Exception:
                break

    def _handle_permission_conn(self, conn: socket.socket):
        """Read one permission request, hold conn open until decision arrives."""
        buf = b""
        try:
            conn.settimeout(10)
            while b"\n" not in buf:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                buf += chunk
            conn.settimeout(None)
        except Exception as e:
            log.warning(f"Permission hook read error: {e}")
            conn.close()
            return

        line = buf.split(b"\n")[0].strip()
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Permission hook: bad JSON")
            conn.close()
            return

        tool_name = request.get("tool_name", "unknown")
        tool_input = request.get("tool_input", {})
        log.info(f"Permission request: {tool_name} {str(tool_input)[:80]}")

        with self._permission_lock:
            if self._permission_conn is not None:
                # Concurrent request — shouldn't happen with a serial queue,
                # but just in case: deny and close the old one.
                log.warning("Permission request arrived while one is pending — denying old")
                try:
                    self._permission_conn.sendall(
                        json.dumps({"decision": "deny", "message": "Superseded."}).encode() + b"\n"
                    )
                    self._permission_conn.close()
                except Exception:
                    pass
            self._permission_conn = conn

        # Broadcast to all subscribers so TelegramNode can show inline buttons
        self._publish_permission_request(tool_name, tool_input)

        # The connection is now held open; _resolve_permission() will close it
        # when the decision arrives.

    def _publish_permission_request(self, tool_name: str, tool_input: dict):
        payload = json.dumps({
            "type": "permission_request",
            "tool_name": tool_name,
            "tool_input": tool_input,
        }) + "\n"
        payload_bytes = payload.encode()
        with self.response_subs_lock:
            dead = []
            for conn in self.response_subscribers:
                try:
                    conn.sendall(payload_bytes)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self.response_subscribers.remove(conn)

    def _resolve_permission(self, decision: str, message: str = ""):
        """
        Called when the user makes a permission decision (allow/deny).
        Sends the response to the waiting permission_hook.py connection.
        """
        with self._permission_lock:
            conn = self._permission_conn
            self._permission_conn = None

        log.info(f"_resolve_permission: decision={decision!r} conn={conn}")
        if conn is None:
            log.warning("_resolve_permission called but no pending permission request")
            return

        payload = {"decision": decision}
        if message:
            payload["message"] = message
        try:
            conn.sendall((json.dumps(payload) + "\n").encode())
            conn.close()
        except Exception as e:
            log.warning(f"Failed to send permission decision: {e}")

        log.info(f"Permission resolved: {decision}")

    # ------------------------------------------------------------------
    # Lock file
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> bool:
        lock = Path(config.LOCK_FILE)
        if lock.exists():
            try:
                pid = int(lock.read_text().strip())
                os.kill(pid, 0)
                log.error(f"Another SessionManagerNode running (PID {pid})")
                return False
            except (ProcessLookupError, ValueError):
                log.info("Stale lock found, taking over")
        Path(config.RELAY_DIR).mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
        return True

    def _release_lock(self):
        try:
            Path(config.LOCK_FILE).unlink()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    def run(self):
        if not self._acquire_lock():
            sys.exit(1)

        os.makedirs(config.SOCKET_DIR, exist_ok=True)
        os.makedirs(config.RELAY_DIR, exist_ok=True)

        self._spawn_claude()

        self._reader_thread = threading.Thread(
            target=self._pty_reader_thread, daemon=True
        )

        threads = [
            self._reader_thread,
            threading.Thread(target=self._queue_processor_thread, daemon=True),
            threading.Thread(target=self._user_input_server_thread, daemon=True),
            threading.Thread(target=self._cli_input_server_thread, daemon=True),
            threading.Thread(target=self._display_server_thread, daemon=True),
            threading.Thread(target=self._response_server_thread, daemon=True),
            threading.Thread(target=self._permission_server_thread, daemon=True),
            threading.Thread(target=self._reflection_ticker_thread, daemon=True),
        ]
        for t in threads:
            t.start()

        signal_module.signal(signal_module.SIGINT, self._shutdown)
        signal_module.signal(signal_module.SIGTERM, self._shutdown)

        log.info("SessionManagerNode running — press Ctrl+C to stop")

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self, *_):
        log.info("Shutting down...")
        self._running = False
        self.input_queue.put(None)

        if self.claude_proc:
            try:
                self.claude_proc.terminate()
            except Exception:
                pass

        for sock_path in [
            config.USER_INPUT_SOCK,
            config.CLI_INPUT_SOCK,
            config.DISPLAY_SOCK,
            config.CLAUDE_RESPONSE_SOCK,
            config.PERMISSION_SOCK,
        ]:
            try:
                os.unlink(sock_path)
            except Exception:
                pass

        self._release_lock()
        sys.exit(0)


if __name__ == "__main__":
    SessionManagerNode().run()
