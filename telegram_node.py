#!/usr/bin/env python3
"""
TelegramNode — Relay v2

Bridges Telegram ↔ SessionManagerNode.

Inbound (Telegram → SessionManager):
  - Text messages → send NDJSON to user_input.sock
  - Voice messages → download → Groq transcription → user_input.sock
  - Photos/documents → download to uploads dir → user_input.sock with media_path

Outbound (SessionManager → Telegram):
  - Subscribes to claude_response.sock (persistent connection)
  - Receives NDJSON {text, source, user_id}
  - Strips memory tags ([REMEMBER], [GOAL], [DONE])
  - Splits at 4096-char Telegram limit
  - Sends to the user

Typing keepalive: sends "typing" action every 4s while waiting for response.
Media cleanup: deletes downloaded files after response delivered.
"""

import asyncio
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [telegram_node] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Memory tag stripping ──────────────────────────────────────────────────────

_TAG_RE = re.compile(
    r"\[REMEMBER:[^\]]+\]"
    r"|\[GOAL:[^\]]+\]"
    r"|\[DONE:[^\]]+\]",
    re.IGNORECASE,
)


def _strip_memory_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


# ── Telegram 4096-char splitter ───────────────────────────────────────────────

_MAX_TELEGRAM = 4000


def _split_message(text: str) -> list[str]:
    if len(text) <= _MAX_TELEGRAM:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= _MAX_TELEGRAM:
            chunks.append(remaining)
            break
        split = remaining.rfind("\n\n", 0, _MAX_TELEGRAM)
        if split == -1:
            split = remaining.rfind("\n", 0, _MAX_TELEGRAM)
        if split == -1:
            split = remaining.rfind(" ", 0, _MAX_TELEGRAM)
        if split == -1:
            split = _MAX_TELEGRAM
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip()
    return chunks


# ── Voice transcription (Groq) ────────────────────────────────────────────────

async def _transcribe_voice(audio_bytes: bytes) -> str:
    provider = config.get("VOICE_PROVIDER")
    if not provider:
        return ""
    if provider == "groq":
        return await _transcribe_groq(audio_bytes)
    log.warning(f"Unknown VOICE_PROVIDER: {provider}")
    return ""


async def _transcribe_groq(audio_bytes: bytes) -> str:
    groq_key = config.get("GROQ_API_KEY")
    if not groq_key:
        log.warning("GROQ_API_KEY not set")
        return ""
    try:
        import tempfile, asyncio, os
        # Convert OGA/OGG/Opus to mp3 via ffmpeg for reliable Groq compatibility
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as src_f:
            src_f.write(audio_bytes)
            src_path = src_f.name
        mp3_path = src_path.replace(".oga", ".mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-b:a", "64k", mp3_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(mp3_path):
            log.error(f"ffmpeg failed (rc={proc.returncode}): {stderr.decode()[-300:]}")
            os.unlink(src_path)
            return ""
        with open(mp3_path, "rb") as mp3_f:
            mp3_bytes = mp3_f.read()
        os.unlink(src_path); os.unlink(mp3_path)
        from groq import AsyncGroq
        client = AsyncGroq(api_key=groq_key)
        result = await client.audio.transcriptions.create(
            file=("voice.mp3", mp3_bytes, "audio/mpeg"),
            model="whisper-large-v3-turbo",
        )
        return result.text.strip()
    except Exception as e:
        log.error(f"Groq transcription error: {e}")
        return ""


# ── SessionManager socket helpers ─────────────────────────────────────────────

def _send_to_session_manager(text: str, source: str, user_id: str, media_path: Optional[str] = None):
    """Send one NDJSON message to user_input.sock (fire-and-forget)."""
    msg: dict = {"text": text, "source": source, "user_id": user_id}
    if media_path:
        msg["media_path"] = media_path
    payload = (json.dumps(msg) + "\n").encode()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(config.USER_INPUT_SOCK)
        sock.sendall(payload)
        sock.close()
    except Exception as e:
        log.error(f"Failed to send to session manager: {e}")


# ── Response subscriber ───────────────────────────────────────────────────────

class ResponseSubscriber:
    """
    Maintains a persistent connection to claude_response.sock.
    Routes messages to two queues:
      _response_queue   — normal {text, source, user_id} messages
      _permission_queue — {type:"permission_request", tool_name, tool_input} messages
      _confirm_queue    — {type:"confirm_request", action_id, summary} messages (executor)
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._response_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._permission_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._confirm_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._tui_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._proactive_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._thread = threading.Thread(target=self._reader_thread, daemon=True)
        self._thread.start()

    def _reader_thread(self):
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(config.CLAUDE_RESPONSE_SOCK)
                log.info("Connected to claude_response.sock")
                buf = b""
                while True:
                    try:
                        data = sock.recv(4096)
                    except Exception:
                        break
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            if msg.get("type") == "confirm_request":
                                self._loop.call_soon_threadsafe(
                                    self._confirm_queue.put_nowait, msg
                                )
                            elif msg.get("type") == "permission_request":
                                self._loop.call_soon_threadsafe(
                                    self._permission_queue.put_nowait, msg
                                )
                            elif msg.get("type") == "tui_prompt":
                                self._loop.call_soon_threadsafe(
                                    self._tui_queue.put_nowait, msg
                                )
                            elif msg.get("source") == "proactive":
                                self._loop.call_soon_threadsafe(
                                    self._proactive_queue.put_nowait, msg
                                )
                            else:
                                self._loop.call_soon_threadsafe(
                                    self._response_queue.put_nowait, msg
                                )
                        except json.JSONDecodeError as e:
                            log.warning(f"Bad response JSON: {e}")
            except Exception as e:
                log.warning(f"claude_response.sock error: {e} — retrying in 0.5s")
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
            time.sleep(0.5)

    async def get(self) -> dict:
        return await self._response_queue.get()

    async def get_permission(self) -> dict:
        return await self._permission_queue.get()

    async def get_confirm(self) -> dict:
        return await self._confirm_queue.get()

    async def get_tui_prompt(self) -> dict:
        return await self._tui_queue.get()

    async def get_proactive(self) -> dict:
        return await self._proactive_queue.get()


# ── Permission debug log ──────────────────────────────────────────────────────

_PLOG_FILE = "/tmp/telegram_permission.log"

def _plog(msg: str):
    from datetime import datetime
    line = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} [telegram] {msg}\n"
    log.info(f"[perm] {msg}")
    try:
        with open(_PLOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


# ── Core handler logic ────────────────────────────────────────────────────────

# Serialize text message processing: only one message processed at a time.
# This prevents race conditions where two concurrent _wait_for_response calls
# grab each other's responses from the shared response queue.
# Button callbacks (permissions, TUI) bypass this lock and always pass through.
_message_lock: Optional[asyncio.Lock] = None  # initialized inside the event loop


def _get_message_lock() -> asyncio.Lock:
    global _message_lock
    if _message_lock is None:
        _message_lock = asyncio.Lock()
    return _message_lock


AUTHORIZED_USER_ID = config.get("TELEGRAM_USER_ID")
_extra_ids: set[str] = {
    uid.strip() for uid in config.EXTRA_USER_IDS.split(",") if uid.strip()
}
UPLOADS_DIR = Path(config.RELAY_DIR) / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _is_authorized(update: Update) -> bool:
    if not AUTHORIZED_USER_ID:
        return True
    uid = str(update.effective_user.id)
    return uid == AUTHORIZED_USER_ID or uid in _extra_ids


async def _typing_keepalive(update: Update, context: ContextTypes.DEFAULT_TYPE, stop_event: asyncio.Event):
    """Send typing action every 4s until stop_event is set."""
    while not stop_event.is_set():
        try:
            await update.effective_message.reply_chat_action("typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=4.0)
        except asyncio.TimeoutError:
            pass


_RESPONSE_TIMEOUT = 720.0  # 12 min (session_manager times out at 10 min)


async def _wait_for_response(
    subscriber: ResponseSubscriber,
    source: str,
    activity_event: Optional[asyncio.Event] = None,
) -> str:
    """Wait for next response from SessionManager with matching source."""
    deadline = asyncio.get_event_loop().time() + _RESPONSE_TIMEOUT
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return "⌛ No response in 12 minutes. Claude may be stuck. Send /restart to reset."
        try:
            msg = await asyncio.wait_for(subscriber.get(), timeout=min(remaining, 30.0))
        except asyncio.TimeoutError:
            continue
        if msg.get("type") == "activity":
            if activity_event and msg.get("growing"):
                activity_event.set()
            continue
        return msg.get("text", "")


async def _status_notifier(
    update: Update,
    stop_event: asyncio.Event,
    activity_event: Optional[asyncio.Event] = None,
    initial_delay: float = 15.0,
    interval: float = 60.0,
):
    """
    After initial_delay, send a status message based on actual JSONL activity.
    - No activity seen: warn that Claude may not have received the message.
    - Activity seen: confirm Claude is working.
    Then repeat every interval seconds with current state.
    """
    try:
        await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=initial_delay)
        return
    except asyncio.TimeoutError:
        pass
    if stop_event.is_set():
        return

    elapsed = int(initial_delay)
    if activity_event and activity_event.is_set():
        msg = f"⏳ Claude is working... ({elapsed}s)"
    else:
        msg = "⚠️ Claude hasn't started responding — the message may not have been received."
    try:
        await update.effective_message.reply_text(msg)
    except Exception:
        pass

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=interval)
            return
        except asyncio.TimeoutError:
            elapsed += int(interval)
        if stop_event.is_set():
            return
        if activity_event and activity_event.is_set():
            status = f"⏳ Claude is working... ({elapsed}s)"
        else:
            status = f"⚠️ Still no response from Claude ({elapsed}s) — consider /restart"
        try:
            await update.effective_message.reply_text(status)
        except Exception:
            pass


async def _handle_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    subscriber: ResponseSubscriber,
    text: str,
    source: str = "telegram",
    media_path: Optional[str] = None,
):
    """Common handler: send to SessionManager, keepalive, deliver response."""
    user_id = str(update.effective_user.id)

    # Send typing immediately — before waiting for the lock, so the user always
    # sees the indicator even if a previous message is still being processed.
    try:
        await update.effective_message.reply_chat_action("typing")
    except Exception:
        pass

    async with _get_message_lock():
        stop_typing = asyncio.Event()
        activity_event = asyncio.Event()
        typing_task = asyncio.create_task(
            _typing_keepalive(update, context, stop_typing)
        )
        status_task = asyncio.create_task(
            _status_notifier(update, stop_typing, activity_event)
        )

        _send_to_session_manager(text, source, user_id, media_path)

        try:
            raw = await _wait_for_response(subscriber, source, activity_event)
        finally:
            stop_typing.set()
            for t in (typing_task, status_task):
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    clean = _strip_memory_tags(raw)
    if not clean:
        clean = "(no response)"

    for chunk in _split_message(clean):
        await update.effective_message.reply_text(chunk)

    # Cleanup downloaded media
    if media_path:
        try:
            os.unlink(media_path)
        except Exception:
            pass


# ── Permission request handling ───────────────────────────────────────────────

def _send_permission_response(decision: str):
    """Send allow/deny decision back to SessionManagerNode."""
    msg = {"type": "permission_response", "decision": decision}
    payload = (json.dumps(msg) + "\n").encode()
    _plog(f"sending permission_response decision={decision} to {config.USER_INPUT_SOCK}")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(config.USER_INPUT_SOCK)
        sock.sendall(payload)
        sock.close()
        _plog("permission_response sent OK")
    except Exception as e:
        log.error(f"Failed to send permission response: {e}")
        _plog(f"ERROR sending permission_response: {e}")


def _format_permission_message(tool_name: str, tool_input: dict) -> str:
    lines = [f"Claude wants to use: *{tool_name}*"]
    # Show the most relevant field(s) for common tools
    if "command" in tool_input:
        lines.append(f"`{tool_input['command'][:200]}`")
    elif "file_path" in tool_input:
        lines.append(f"`{tool_input['file_path']}`")
    elif tool_input:
        # Show first field as fallback
        key, val = next(iter(tool_input.items()))
        lines.append(f"`{key}: {str(val)[:200]}`")
    lines.append("\nAllow?")
    return "\n".join(lines)


async def _tui_dispatcher(
    subscriber: ResponseSubscriber,
    bot,
    authorized_user_id: str,
):
    """
    Background task: reads TUI choice prompts from the subscriber and
    sends inline keyboard buttons to the authorized user.
    """
    while True:
        msg = await subscriber.get_tui_prompt()
        question = msg.get("question", "Claude is asking:")
        choices = msg.get("choices", [])
        if not choices:
            continue

        log.info(f"Sending TUI prompt to Telegram: {question!r}")
        buttons = [
            InlineKeyboardButton(f"{c['num']}. {c['text']}", callback_data=f"tui:{c['num']}")
            for c in choices
        ]
        # Two buttons per row
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        keyboard = InlineKeyboardMarkup(rows)
        text = f"*Claude is asking:*\n{question}"
        try:
            await bot.send_message(
                chat_id=authorized_user_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error(f"Failed to send TUI prompt to Telegram: {e}")


_SILENT_TOKEN = "[SILENT]"


async def _proactive_dispatcher(
    subscriber: ResponseSubscriber,
    bot,
    authorized_user_id: str,
):
    """
    Background task: receives proactive responses from SessionManager.
    Discards [SILENT] responses; forwards everything else to the user.
    """
    while True:
        msg = await subscriber.get_proactive()
        raw = msg.get("text", "")
        clean = _strip_memory_tags(raw).strip()

        if not clean or _SILENT_TOKEN.lower() in clean.lower():
            log.info("Proactive check-in: silent (nothing to send)")
            continue

        log.info(f"Proactive message to Telegram: {clean[:80]}")
        for chunk in _split_message(clean):
            try:
                await bot.send_message(chat_id=authorized_user_id, text=chunk)
            except Exception as e:
                log.error(f"Failed to send proactive message: {e}")


async def _confirm_dispatcher(
    subscriber: "ResponseSubscriber",
    bot,
    authorized_user_id: str,
):
    """Background task: sends executor action confirmation requests as Telegram inline buttons."""
    while True:
        msg = await subscriber.get_confirm()
        action_id = msg.get("action_id", "")
        summary = msg.get("summary", "unknown action")
        log.info(f"Sending confirm request to Telegram: action_id={action_id}")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"exec:approve:{action_id}"),
            InlineKeyboardButton("❌ Deny",    callback_data=f"exec:deny:{action_id}"),
        ]])
        try:
            await bot.send_message(
                chat_id=authorized_user_id,
                text=summary,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error(f"Failed to send confirm request: {e}")


async def _permission_dispatcher(
    subscriber: ResponseSubscriber,
    bot,
    authorized_user_id: str,
):
    """
    Background task: reads permission requests from the subscriber and
    sends an inline keyboard to the authorized user.
    """
    while True:
        msg = await subscriber.get_permission()
        tool_name = msg.get("tool_name", "unknown")
        tool_input = msg.get("tool_input", {})
        log.info(f"Sending permission request to Telegram: {tool_name}")

        text = _format_permission_message(tool_name, tool_input)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Allow", callback_data="perm:allow"),
                InlineKeyboardButton("Deny", callback_data="perm:deny"),
            ]
        ])
        try:
            await bot.send_message(
                chat_id=authorized_user_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error(f"Failed to send permission request to Telegram: {e}")
            # Auto-deny so Claude isn't stuck
            _send_permission_response("deny")


# ── Knowledge base ingestion ──────────────────────────────────────────────────

_KB_INGEST_SCRIPT = Path(__file__).parent / "kb_ingest.py"
_KB_TRIGGER_RE = re.compile(r"knowledge\s*base", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")


def _is_kb_request(caption: str) -> bool:
    return bool(_KB_TRIGGER_RE.search(caption or ""))


def _parse_kb_caption(caption: str, default_title: str) -> dict:
    """Caption format: "...knowledge base... | Title | Author | Category" (all after the trigger optional)."""
    cleaned = _KB_TRIGGER_RE.sub("", caption or "").strip(" |").strip()
    parts = [p.strip() for p in cleaned.split("|")] if cleaned else []
    return {
        "title": parts[0] if parts and parts[0] else default_title,
        "author": parts[1] if len(parts) > 1 else "",
        "category": parts[2] if len(parts) > 2 else "general",
    }


async def _ingest_kb(update: Update, kind: str, source: str, caption: str, default_title: str):
    """Run kb_ingest.py for a PDF/PPTX/image/link. kind is one of pdf/pptx/image/link."""
    meta = _parse_kb_caption(caption, default_title)

    await update.message.reply_text(
        f"Adding to knowledge base: *{meta['title']}*...\nThis may take a minute.",
        parse_mode="Markdown",
    )

    env = os.environ.copy()
    env["SUPABASE_URL"] = config.SUPABASE_URL
    env["SUPABASE_ANON_KEY"] = config.SUPABASE_ANON_KEY
    env["CLAUDE_PATH"] = config.CLAUDE_PATH

    cmd = [
        sys.executable, str(_KB_INGEST_SCRIPT),
        f"--{kind}", source,
        "--title", meta["title"],
        "--category", meta["category"],
    ]
    if meta["author"]:
        cmd += ["--author", meta["author"]]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace")
        log.info(f"kb_ingest output:\n{output[-1000:]}")
        if proc.returncode == 0:
            lines = [l for l in output.splitlines() if any(
                kw in l for kw in ["complete", "Chunks", "Errors", "Title"]
            )]
            summary = "\n".join(lines[-6:]) if lines else "Done."
            await update.message.reply_text(f"✅ Added to knowledge base\n{summary}")
        else:
            await update.message.reply_text(f"❌ Knowledge base ingestion failed:\n{output[-500:]}")
    except Exception as e:
        log.error(f"kb_ingest error: {e}")
        await update.message.reply_text(f"❌ Error running knowledge base ingestion: {e}")
    finally:
        if kind in ("pdf", "pptx", "image"):
            try:
                os.unlink(source)
            except Exception:
                pass


# ── PDF study ingestion ───────────────────────────────────────────────────────

_INGEST_SCRIPT = Path(__file__).parent / "study_ingest.py"

async def _ingest_study_pdf(update: Update, file_path: str, caption: str):
    """
    Run study_ingest.py on a PDF sent via Telegram.
    Caption format: "Area Name | Book Title | Author"  (area required, rest optional)
    Example: "Linear Algebra | Introduction to Linear Algebra | Gilbert Strang"
    """
    parts = [p.strip() for p in caption.split("|")]
    area = parts[0] if parts and parts[0] else ""
    title = parts[1] if len(parts) > 1 else Path(file_path).stem
    author = parts[2] if len(parts) > 2 else ""

    if not area:
        await update.message.reply_text(
            f"📚 PDF saved at:\n`{file_path}`\n\n"
            "Tell me: area, title, and author — I'll ingest it.\n"
            "Or resend with caption: `Fitness | Book Title | Author`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"Ingesting *{title}* ({area})...\nThis will take a few minutes.",
        parse_mode="Markdown",
    )

    env = os.environ.copy()
    env["SUPABASE_URL"] = config.SUPABASE_URL
    env["SUPABASE_ANON_KEY"] = config.SUPABASE_ANON_KEY
    env["TELEGRAM_BOT_TOKEN"] = config.get("TELEGRAM_BOT_TOKEN")
    env["TELEGRAM_USER_ID"] = config.get("TELEGRAM_USER_ID")

    cmd = [
        sys.executable, str(_INGEST_SCRIPT),
        file_path,
        "--area", area,
        "--title", title,
    ]
    if author:
        cmd += ["--author", author]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace")
        log.info(f"study_ingest output:\n{output[-1000:]}")
        if proc.returncode == 0:
            # Extract summary lines from output
            lines = [l for l in output.splitlines() if any(
                kw in l for kw in ["complete", "Chunks", "Chapters", "Pages", "Error"]
            )]
            summary = "\n".join(lines[-6:]) if lines else "Done."
            await update.message.reply_text(f"✅ Ingestion complete\n{summary}")
        else:
            await update.message.reply_text(f"❌ Ingestion failed:\n{output[-500:]}")
    except Exception as e:
        log.error(f"PDF ingestion error: {e}")
        await update.message.reply_text(f"❌ Error running ingestion: {e}")
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass


# ── Message handlers ──────────────────────────────────────────────────────────

def _make_handlers(subscriber: ResponseSubscriber):

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            await update.message.reply_text("This bot is private.")
            return
        text = update.message.text
        log.info(f"Text from {update.effective_user.id}: {text[:60]}")

        if _is_kb_request(text):
            url_match = _URL_RE.search(text)
            if url_match:
                await _ingest_kb(update, "link", url_match.group(0), text, url_match.group(0))
                return
            await update.message.reply_text(
                "Got the 'knowledge base' request but no link found in the message — "
                "send a PDF/slides/image as an attachment, or include a URL in the text."
            )
            return

        await _handle_and_reply(update, context, subscriber, text)

    async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            await update.message.reply_text("This bot is private.")
            return
        voice = update.message.voice
        log.info(f"Voice ({voice.duration}s) from {update.effective_user.id}")

        if not config.get("VOICE_PROVIDER"):
            await update.message.reply_text(
                "Voice transcription is not configured (VOICE_PROVIDER not set)."
            )
            return

        await update.message.reply_chat_action("typing")
        try:
            tg_file = await voice.get_file()
            import httpx
            async with httpx.AsyncClient(timeout=30) as dl_client:
                resp = await dl_client.get(tg_file.file_path)
                audio_bytes = resp.content
            log.info(f"Voice download: {len(audio_bytes)} bytes, status={resp.status_code}")
            transcription = await _transcribe_voice(bytes(audio_bytes))
            if not transcription:
                await update.message.reply_text("Could not transcribe voice message.")
                return
            text = f"[Voice message transcribed]: {transcription}"
            log.info(f"Transcribed: {transcription[:80]}")
            await _handle_and_reply(update, context, subscriber, text)
        except Exception as e:
            log.error(f"Voice error: {e}")
            await update.message.reply_text("Could not process voice message.")

    async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            await update.message.reply_text("This bot is private.")
            return
        log.info(f"Photo from {update.effective_user.id}")
        await update.message.reply_chat_action("typing")
        try:
            photo = update.message.photo[-1]  # highest resolution
            tg_file = await photo.get_file()
            ts = int(time.time() * 1000)
            file_path = str(UPLOADS_DIR / f"image_{ts}.jpg")
            await tg_file.download_to_drive(file_path)
            caption = update.message.caption or ""

            if _is_kb_request(caption):
                await _ingest_kb(update, "image", file_path, caption, f"image_{ts}")
                return

            text = f"[Image: {file_path}]\n\n{caption or 'Analyze this image.'}"
            await _handle_and_reply(update, context, subscriber, text, media_path=file_path)
        except Exception as e:
            log.error(f"Photo error: {e}")
            await update.message.reply_text("Could not process image.")

    async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            await update.message.reply_text("This bot is private.")
            return
        doc = update.message.document
        log.info(f"Document ({doc.file_name}) from {update.effective_user.id}")
        await update.message.reply_chat_action("typing")
        try:
            tg_file = await doc.get_file()
            ts = int(time.time() * 1000)
            file_name = doc.file_name or f"file_{ts}"
            file_path = str(UPLOADS_DIR / f"{ts}_{file_name}")
            await tg_file.download_to_drive(file_path)

            # Detect PDF by magic bytes (handles files without .pdf extension)
            is_pdf = False
            try:
                with open(file_path, "rb") as _f:
                    is_pdf = _f.read(4) == b"%PDF"
            except Exception:
                pass
            is_pdf = is_pdf or (doc.file_name or "").lower().endswith(".pdf")
            is_pptx = (doc.file_name or "").lower().endswith((".pptx", ".ppt"))

            caption = update.message.caption or ""

            # Explicit "knowledge base" request takes priority over study ingestion
            if _is_kb_request(caption) and (is_pdf or is_pptx):
                kind = "pdf" if is_pdf else "pptx"
                await _ingest_kb(update, kind, file_path, caption, Path(file_name).stem)
                return

            # PDF: copy to persistent books dir and route to study ingestion
            if is_pdf:
                books_dir = Path(config.RELAY_DIR) / "books"
                books_dir.mkdir(exist_ok=True)
                # Use original filename with .pdf extension
                safe_name = re.sub(r"[^\w\-.]", "_", file_name)
                if not safe_name.lower().endswith(".pdf"):
                    safe_name += ".pdf"
                persistent_path = str(books_dir / f"{ts}_{safe_name}")
                import shutil
                shutil.copy2(file_path, persistent_path)
                log.info(f"PDF saved to persistent path: {persistent_path}")
                caption = update.message.caption or ""
                await _ingest_study_pdf(update, persistent_path, caption)
                return

            caption = update.message.caption or f"Analyze: {doc.file_name}"
            text = f"[File: {file_path}]\n\n{caption}"
            await _handle_and_reply(update, context, subscriber, text, media_path=file_path)
        except Exception as e:
            log.error(f"Document error: {e}")
            await update.message.reply_text("Could not process document.")

    return on_text, on_voice, on_photo, on_document


# ── Slash command handlers ────────────────────────────────────────────────────

def _read_session_jsonl(session_id: str) -> list[dict]:
    project_name = config.PROJECT_DIR.replace("/", "-")
    session_file = Path.home() / ".claude" / "projects" / project_name / f"{session_id}.jsonl"
    entries = []
    if not session_file.exists():
        return entries
    with open(session_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _make_slash_handlers():

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            return
        text = (
            "*Available commands*\n\n"
            "/usage — token usage for current 5h window\n"
            "/model — current Claude model\n"
            "/status — relay health check\n"
            "/clear — start a fresh Claude session (takes effect on next restart)\n"
            "/help — this message"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            return
        lines = ["*Relay Status*\n"]
        lock = Path(config.LOCK_FILE)
        if lock.exists():
            try:
                pid = int(lock.read_text().strip())
                os.kill(pid, 0)
                lines.append(f"✅ SessionManager running (PID {pid})")
            except (ProcessLookupError, ValueError):
                lines.append("❌ SessionManager not running (stale lock)")
        else:
            lines.append("❌ SessionManager not running")
        try:
            sid = Path(config.SESSION_ID_FILE).read_text().strip()
            lines.append(f"Session: `{sid[:8]}...`")
        except Exception:
            lines.append("Session: none")
        for name, path in [
            ("user\\_input.sock", config.USER_INPUT_SOCK),
            ("permission.sock", config.PERMISSION_SOCK),
        ]:
            lines.append(f"{'✅' if Path(path).exists() else '❌'} {name}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            return

        tz = ZoneInfo(config.USER_TIMEZONE or "UTC")
        now = time.time()
        window_cutoff = now - 5 * 3600
        week_cutoff   = now - 7 * 24 * 3600

        project_name  = config.PROJECT_DIR.replace("/", "-")
        sessions_dir  = Path.home() / ".claude" / "projects" / project_name

        def _parse_ts(raw) -> Optional[float]:
            if isinstance(raw, str):
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    return None
            if isinstance(raw, (int, float)):
                return float(raw)
            return None

        w5h  = dict(inp=0, out=0, cr=0, cc=0)
        wk   = dict(inp=0, out=0, cr=0, cc=0)
        earliest_5h: Optional[float] = None

        try:
            all_files = list(sessions_dir.glob("*.jsonl"))
        except Exception:
            all_files = []

        for jfile in all_files:
            try:
                with open(jfile, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj  = json.loads(line)
                            ts   = _parse_ts(obj.get("timestamp"))
                            usage = obj.get("message", {}).get("usage")
                            if not usage or ts is None:
                                continue
                            inp = usage.get("input_tokens", 0)
                            out = usage.get("output_tokens", 0)
                            cr  = usage.get("cache_read_input_tokens", 0)
                            cc  = usage.get("cache_creation_input_tokens", 0)
                            if ts >= week_cutoff:
                                wk["inp"] += inp; wk["out"] += out
                                wk["cr"]  += cr;  wk["cc"]  += cc
                            if ts >= window_cutoff:
                                w5h["inp"] += inp; w5h["out"] += out
                                w5h["cr"]  += cr;  w5h["cc"]  += cc
                                if earliest_5h is None or ts < earliest_5h:
                                    earliest_5h = ts
                        except Exception:
                            continue
            except Exception:
                continue

        # 5h window reset countdown
        if earliest_5h:
            reset_ts  = earliest_5h + 5 * 3600
            reset_dt  = datetime.fromtimestamp(reset_ts, tz=tz)
            mins_left = int((reset_ts - now) / 60)
            if mins_left > 0:
                h, m = divmod(mins_left, 60)
                countdown = f"in {h}h {m}m" if h else f"in {m}m"
            else:
                countdown = "soon"
            win_reset = f"{reset_dt.strftime('%H:%M %Z')} ({countdown})"
        else:
            win_reset = "no activity in window"

        # Weekly reset — next Monday 00:00 local
        now_dt     = datetime.fromtimestamp(now, tz=tz)
        days_to_mon = (7 - now_dt.weekday()) % 7 or 7
        wk_reset_dt = (now_dt + timedelta(days=days_to_mon)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wk_reset = wk_reset_dt.strftime("%a %d/%m %H:%M %Z")

        # Percentage helpers (limits from .env, 0 = not set)
        lim_5h = int(config.get("USAGE_5H_LIMIT", "0") or 0)
        lim_wk = int(config.get("USAGE_WEEK_LIMIT", "0") or 0)

        def pct(used: int, limit: int) -> str:
            return f" — {used * 100 // limit}%" if limit else ""

        lines = [
            f"*5h window* — resets {win_reset}",
            f"Output: {w5h['out']:>9,}{pct(w5h['out'], lim_5h)}",
            f"Input:  {w5h['inp']:>9,}",
            f"Cache:  {w5h['cr']:>9,} read / {w5h['cc']:,} created",
            "",
            f"*7-day total* — resets {wk_reset}",
            f"Output: {wk['out']:>9,}{pct(wk['out'], lim_wk)}",
            f"Input:  {wk['inp']:>9,}",
            f"Cache:  {wk['cr']:>9,} read / {wk['cc']:,} created",
        ]
        if not lim_5h or not lim_wk:
            lines.append(
                "\n_Set USAGE\\_5H\\_LIMIT and USAGE\\_WEEK\\_LIMIT in .env for % display_"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            return
        model = "unknown"
        try:
            session_id = Path(config.SESSION_ID_FILE).read_text().strip()
            for obj in _read_session_jsonl(session_id):
                m = obj.get("model")
                if m:
                    model = m
        except Exception:
            pass
        await update.message.reply_text(f"Current model: `{model}`", parse_mode="Markdown")

    async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            return
        try:
            Path(config.SESSION_ID_FILE).unlink(missing_ok=True)
            await update.message.reply_text(
                "✅ Session cleared. Restart SessionManagerNode to begin a fresh session."
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    return cmd_help, cmd_status, cmd_usage, cmd_model, cmd_clear


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    token = config.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    # post_init runs inside run_polling()'s event loop — safe to get the loop here.
    async def post_init(application):
        loop = asyncio.get_event_loop()
        subscriber = ResponseSubscriber(loop)
        on_text, on_voice, on_photo, on_document = _make_handlers(subscriber)
        cmd_help, cmd_status, cmd_usage, cmd_model, cmd_clear = _make_slash_handlers()

        # Slash commands (intercepted before reaching Claude PTY)
        application.add_handler(CommandHandler("help",   cmd_help))
        application.add_handler(CommandHandler("status", cmd_status))
        application.add_handler(CommandHandler("usage",  cmd_usage))
        application.add_handler(CommandHandler("model",  cmd_model))
        application.add_handler(CommandHandler("clear",  cmd_clear))

        # Message handlers (forwarded to Claude PTY)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        application.add_handler(MessageHandler(filters.VOICE, on_voice))
        application.add_handler(MessageHandler(filters.PHOTO, on_photo))
        application.add_handler(MessageHandler(filters.Document.ALL, on_document))

        # Permission inline keyboard handler
        async def on_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            _plog(f"callback received: data={query.data!r} from_user={query.from_user.id}")

            # Send permission decision FIRST, before any await that could fail
            auth_ok = not AUTHORIZED_USER_ID or str(query.from_user.id) == AUTHORIZED_USER_ID
            _plog(f"auth_ok={auth_ok} AUTHORIZED_USER_ID={AUTHORIZED_USER_ID!r}")
            if auth_ok:
                decision = "allow" if query.data == "perm:allow" else "deny"
                _send_permission_response(decision)
                log.info(f"Permission {decision} via Telegram button")

            # Now handle Telegram UI updates (non-critical)
            try:
                await query.answer()
            except Exception:
                pass
            if auth_ok:
                label = "✓ Allowed" if decision == "allow" else "✗ Denied"
                try:
                    await query.edit_message_text(label)
                except Exception as e:
                    log.warning(f"Could not edit permission message: {e}")

        application.add_handler(CallbackQueryHandler(on_permission_callback, pattern="^perm:"))

        # Executor confirm inline keyboard handler (deepseek_brain actions)
        async def on_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            auth_ok = not AUTHORIZED_USER_ID or str(query.from_user.id) == AUTHORIZED_USER_ID
            if auth_ok:
                parts = query.data.split(":", 2)  # exec:approve:<id>
                decision = parts[1] if len(parts) > 1 else "deny"
                action_id = parts[2] if len(parts) > 2 else ""
                approved = decision == "approve"
                log.info(f"Executor confirm {decision} for action_id={action_id}")
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(config.USER_INPUT_SOCK)
                    payload = json.dumps({"type": "confirm_response", "action_id": action_id, "approved": approved}) + "\n"
                    sock.sendall(payload.encode())
                    sock.close()
                except Exception as e:
                    log.error(f"Failed to send confirm_response: {e}")
            try:
                await query.answer()
                label = "✅ Approved" if (auth_ok and approved) else "❌ Denied"
                await query.edit_message_text(label)
            except Exception:
                pass

        application.add_handler(CallbackQueryHandler(on_confirm_callback, pattern="^exec:"))

        # TUI choice prompt inline keyboard handler
        async def on_tui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            auth_ok = not AUTHORIZED_USER_ID or str(query.from_user.id) == AUTHORIZED_USER_ID
            if auth_ok:
                choice = query.data.split(":", 1)[1]  # "tui:2" → "2"
                log.info(f"TUI choice {choice!r} via Telegram button")
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(config.USER_INPUT_SOCK)
                    sock.sendall((json.dumps({"type": "tui_response", "choice": choice}) + "\n").encode())
                    sock.close()
                except Exception as e:
                    log.error(f"Failed to send tui_response: {e}")
            try:
                await query.answer()
                label = f"✓ {query.data.split(':')[1]} selected"
                await query.edit_message_text(label)
            except Exception:
                pass

        application.add_handler(CallbackQueryHandler(on_tui_callback, pattern="^tui:"))

        # Start permission, TUI, and proactive dispatchers as background asyncio tasks
        if AUTHORIZED_USER_ID:
            loop.create_task(
                _permission_dispatcher(subscriber, application.bot, AUTHORIZED_USER_ID)
            )
            loop.create_task(
                _tui_dispatcher(subscriber, application.bot, AUTHORIZED_USER_ID)
            )
            loop.create_task(
                _proactive_dispatcher(subscriber, application.bot, AUTHORIZED_USER_ID)
            )
            loop.create_task(
                _confirm_dispatcher(subscriber, application.bot, AUTHORIZED_USER_ID)
            )

        log.info(f"TelegramNode starting (authorized user: {AUTHORIZED_USER_ID or 'ANY'})")

    app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).build()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
