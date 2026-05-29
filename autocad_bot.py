#!/usr/bin/env python3
"""
AutoCAD 2025 Assistant Bot

RAG bot that answers AutoCAD 2025 questions using official Russian documentation
stored in Supabase. Uses Claude CLI for answering.

Handles any question: specific functions, buttons, tasks, workflows.
Responds in Russian with step-by-step instructions when needed.
"""

import os
import json
import logging
import subprocess
import urllib.request
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN     = os.environ["AUTOCAD_BOT_TOKEN"]
ALLOWED_USERS = {int(os.environ["AUTOCAD_BOT_USER_ID"]), int(os.environ["TELEGRAM_USER_ID"])}
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_ANON_KEY"]
CLAUDE_PATH   = os.environ.get("CLAUDE_PATH", "claude").replace("claude_relay_wrapper.sh", "").rstrip("/") + "/claude" if "wrapper" in os.environ.get("CLAUDE_PATH", "") else "claude"


def search_docs(query: str, match_count: int = 8) -> list[dict]:
    data = json.dumps({
        "query": query,
        "table": "autocad_docs",
        "match_count": match_count,
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/functions/v1/search",
        data=data,
        headers={"Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def multi_search(question: str) -> list[dict]:
    """Search with the original question plus English command name variant."""
    queries = [question]

    # Also try common English AutoCAD terms if question is in Russian
    en_terms = {
        "штриховк": "HATCH command fill pattern", "размер": "DIMSTYLE dimension",
        "видовой": "VIEWPORT layout paper space", "слой": "LAYER properties",
        "блок": "BLOCK INSERT definition", "текст": "TEXT MTEXT annotation",
        "линия": "LINE command draw", "полилиния": "POLYLINE PLINE",
        "окружность": "CIRCLE command", "дуга": "ARC command",
        "прямоугольник": "RECTANGLE RECTANG", "обрезать": "TRIM command",
        "зеркало": "MIRROR command", "массив": "ARRAY command",
        "привязк": "SNAP OSNAP object snap", "координат": "UCS coordinate system",
        "чертеж": "drawing template setup", "печать": "PLOT PRINT layout",
        "конус": "3D solid cone CONE command", "цилиндр": "CYLINDER 3D solid",
        "экструзи": "EXTRUDE 3D solid", "объект": "object properties selection",
    }
    q_lower = question.lower()
    for ru, en in en_terms.items():
        if ru in q_lower:
            queries.append(en)
            break

    seen = set()
    all_chunks = []
    for q in queries:
        try:
            for c in search_docs(q, match_count=8):
                key = c.get("content", "")[:80]
                if key not in seen:
                    seen.add(key)
                    all_chunks.append(c)
        except Exception as e:
            log.warning(f"Search failed for '{q}': {e}")

    all_chunks.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return all_chunks[:15]


def ask_claude(question: str, chunks: list[dict]) -> str:
    if chunks:
        context = "\n\n---\n\n".join(
            f"[{c.get('title', '')}]\n{c.get('content', '')}"
            for c in chunks
        )
    else:
        context = "Документация не найдена."

    prompt = f"""Ты — эксперт по AutoCAD 2025. Пользователь задал вопрос об AutoCAD.

Официальная документация AutoCAD 2025 (русская версия):
{context[:7000]}

Вопрос пользователя: {question}

Инструкции:
- Отвечай на основе документации выше. Используй её как источник точной информации о командах, меню, кнопках.
- Если вопрос — задача (как сделать X), дай пошаговый алгоритм с точными названиями команд и меню.
- Указывай: команду командной строки, путь в меню/ленте, горячую клавишу — если есть в документации.
- Если документация не содержит нужной информации, скажи об этом честно и дай общий ответ на основе знаний об AutoCAD.
- Пиши чётко, по делу, структурированно. Отвечай на русском языке."""

    result = subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions", prompt],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:200])
    return result.stdout.strip()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("Извините, этот бот приватный.")
        return

    question = update.message.text
    if not question:
        return

    log.info(f"[{user_id}] {question[:80]}")
    thinking_msg = await update.message.reply_text("🔍 Ищу в документации AutoCAD 2025...")

    try:
        chunks = multi_search(question)
        log.info(f"Found {len(chunks)} chunks, top similarity: {chunks[0].get('similarity', 0):.3f}" if chunks else "No chunks")

        answer = ask_claude(question, chunks)

        # Top 3 source titles
        titles = list(dict.fromkeys(c.get("title", "") for c in chunks[:5] if c.get("title")))
        sources = "\n".join(f"• {t}" for t in titles[:3])
        full_reply = f"{answer}\n\n📖 Источники: {sources}" if sources else answer

        # Telegram message limit is 4096 chars
        if len(full_reply) > 4000:
            full_reply = full_reply[:3990] + "…"

        await thinking_msg.delete()
        await update.message.reply_text(full_reply)

    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        await thinking_msg.delete()
        await update.message.reply_text(f"Ошибка: {e}")


def main():
    log.info("Starting AutoCAD bot (Claude-powered)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info(f"Allowed users: {ALLOWED_USERS}")
    app.run_polling()


if __name__ == "__main__":
    main()
