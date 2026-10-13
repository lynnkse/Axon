"""Exercise photo prompt construction without Telegram, sockets, or inference."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import telegram_node as telegram


AILIN_FALLBACK = (
    "Look at the image Lynn shared and reply to him naturally as Ailin, "
    "in your established first-person conversational voice. "
    "Ground your response in what you can see and the current conversation."
)


@pytest.fixture
def photo_handler(monkeypatch, tmp_path):
    tg_file = SimpleNamespace(download_to_drive=AsyncMock())
    photo = SimpleNamespace(get_file=AsyncMock(return_value=tg_file))
    low_resolution = SimpleNamespace(get_file=AsyncMock())
    message = SimpleNamespace(
        photo=[low_resolution, photo], caption=None,
        reply_chat_action=AsyncMock(), reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=123))
    context, subscriber = object(), object()
    forward = AsyncMock()
    ingest = AsyncMock()
    authorized = Mock(return_value=True)
    monkeypatch.setattr(telegram, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(telegram, "_is_authorized", authorized)
    monkeypatch.setattr(telegram, "_handle_and_reply", forward)
    monkeypatch.setattr(telegram, "_ingest_kb", ingest)
    monkeypatch.setattr(telegram.time, "time", lambda: 1234.5)
    _, _, on_photo, _ = telegram._make_handlers(subscriber)

    def run(instance, caption):
        monkeypatch.setattr(telegram.config, "INSTANCE", instance)
        message.caption = caption
        asyncio.run(on_photo(update, context))

    return SimpleNamespace(
        run=run, update=update, context=context, subscriber=subscriber,
        forward=forward, ingest=ingest, authorized=authorized,
        download=tg_file.download_to_drive, photo=photo, low=low_resolution,
        path=str(tmp_path / "image_1234500.jpg"),
    )


@pytest.mark.parametrize("caption", [None, ""])
def test_uncaptioned_ailin_photo_uses_conversational_voice(photo_handler, caption):
    h = photo_handler
    h.run("ailin", caption)
    h.forward.assert_awaited_once_with(
        h.update, h.context, h.subscriber,
        f"[Image: {h.path}]\n\n{AILIN_FALLBACK}", media_path=h.path,
    )
    h.download.assert_awaited_once_with(h.path)
    h.low.get_file.assert_not_awaited()
    h.ingest.assert_not_awaited()


@pytest.mark.parametrize("instance", ["ailin", "rog"])
@pytest.mark.parametrize("caption", [
    "  Посмотри!\nKeep my spacing.  ", "Analyze this image.", " \n ",
])
def test_caption_is_preserved_verbatim(photo_handler, instance, caption):
    h = photo_handler
    h.run(instance, caption)
    h.forward.assert_awaited_once_with(
        h.update, h.context, h.subscriber,
        f"[Image: {h.path}]\n\n{caption}", media_path=h.path,
    )


@pytest.mark.parametrize("instance", ["rog", "standard", ""])
@pytest.mark.parametrize("caption", [None, ""])
def test_default_photo_fallback_is_unchanged(photo_handler, instance, caption):
    h = photo_handler
    h.run(instance, caption)
    h.forward.assert_awaited_once_with(
        h.update, h.context, h.subscriber,
        f"[Image: {h.path}]\n\nAnalyze this image.", media_path=h.path,
    )


def test_ailin_photo_authorization_precedes_download(photo_handler):
    h = photo_handler
    h.authorized.return_value = False
    h.run("ailin", None)
    h.photo.get_file.assert_not_awaited()
    h.download.assert_not_awaited()
    h.forward.assert_not_awaited()
    h.update.message.reply_text.assert_awaited_once_with("This bot is private.")


def test_ailin_knowledge_base_caption_keeps_existing_route(photo_handler):
    h = photo_handler
    caption = "knowledge base | My image"
    h.run("ailin", caption)
    h.ingest.assert_awaited_once_with(h.update, "image", h.path, caption, "image_1234500")
    h.forward.assert_not_awaited()
