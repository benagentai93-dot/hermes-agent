"""Tests for the Telegram final-reply feedback ("report error") button."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.platforms.base import SendResult
from gateway.platforms.telegram import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_event(chat_id="123", thread_id=None):
    return SimpleNamespace(source=SimpleNamespace(chat_id=chat_id, thread_id=thread_id))


def _make_send_result(message_ids=("10", "11")):
    return SendResult(
        success=True,
        message_id=message_ids[0] if message_ids else None,
        raw_response={"message_ids": list(message_ids)},
    )


def _make_query(data="vf:1", user_id=42, chat_id=123, chat_type="supergroup"):
    chat = SimpleNamespace(id=chat_id, type=chat_type, title="Group")
    message = SimpleNamespace(
        chat_id=chat_id,
        chat=chat,
        message_thread_id=None,
        message_id=10,
    )
    query = MagicMock()
    query.data = data
    query.message = message
    query.from_user = SimpleNamespace(id=user_id, first_name="Ben", full_name="Ben L")
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return query


# ===========================================================================
# attach_feedback_control
# ===========================================================================

@pytest.mark.asyncio
async def test_attach_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_FEEDBACK_BUTTON", raising=False)
    adapter = _make_adapter()

    await adapter.attach_feedback_control(_make_event(), _make_send_result(), "answer")

    adapter._bot.edit_message_reply_markup.assert_not_called()
    assert adapter._feedback_state == {}


@pytest.mark.asyncio
async def test_attach_adds_button_to_last_chunk(monkeypatch):
    monkeypatch.setenv("TELEGRAM_FEEDBACK_BUTTON", "true")
    adapter = _make_adapter()

    await adapter.attach_feedback_control(
        _make_event(chat_id="123"), _make_send_result(("10", "11")), "answer text",
    )

    adapter._bot.edit_message_reply_markup.assert_called_once()
    kwargs = adapter._bot.edit_message_reply_markup.call_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["message_id"] == 11  # last chunk, not first
    assert len(adapter._feedback_state) == 1
    state = next(iter(adapter._feedback_state.values()))
    assert state["chat_id"] == "123"
    assert state["message_id"] == "11"
    assert state["excerpt"] == "answer text"


@pytest.mark.asyncio
async def test_attach_failure_drops_state(monkeypatch):
    monkeypatch.setenv("TELEGRAM_FEEDBACK_BUTTON", "true")
    adapter = _make_adapter()
    adapter._bot.edit_message_reply_markup = AsyncMock(side_effect=RuntimeError("boom"))

    await adapter.attach_feedback_control(_make_event(), _make_send_result(), "x")

    assert adapter._feedback_state == {}


@pytest.mark.asyncio
async def test_attach_state_bounded(monkeypatch):
    monkeypatch.setenv("TELEGRAM_FEEDBACK_BUTTON", "true")
    adapter = _make_adapter()
    for i in range(adapter._FEEDBACK_STATE_MAX + 20):
        await adapter.attach_feedback_control(
            _make_event(), _make_send_result((str(i),)), "x",
        )
    assert len(adapter._feedback_state) <= adapter._FEEDBACK_STATE_MAX


# ===========================================================================
# vf: callback handling
# ===========================================================================

@pytest.mark.asyncio
async def test_callback_injects_feedback_event(monkeypatch):
    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    adapter.handle_message = AsyncMock()
    adapter._feedback_state[1] = {
        "chat_id": "123",
        "message_id": "10",
        "thread_id": None,
        "excerpt": "original verdict text",
    }

    query = _make_query(data="vf:1")
    update = SimpleNamespace(callback_query=query)
    await adapter._handle_callback_query(update, None)

    assert 1 not in adapter._feedback_state
    query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)
    adapter.handle_message.assert_called_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.reply_to_text == "original verdict text"
    assert event.reply_to_message_id == "10"
    assert "回報錯誤" in event.text
    assert event.source.chat_id == "123"
    assert event.source.user_id == "42"


@pytest.mark.asyncio
async def test_callback_unauthorized_no_injection():
    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=False)
    adapter.handle_message = AsyncMock()
    adapter._feedback_state[1] = {
        "chat_id": "123", "message_id": "10", "thread_id": None, "excerpt": "x",
    }

    query = _make_query(data="vf:1")
    update = SimpleNamespace(callback_query=query)
    await adapter._handle_callback_query(update, None)

    assert 1 in adapter._feedback_state  # untouched
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_callback_already_handled():
    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    adapter.handle_message = AsyncMock()

    query = _make_query(data="vf:99")
    update = SimpleNamespace(callback_query=query)
    await adapter._handle_callback_query(update, None)

    adapter.handle_message.assert_not_called()
    query.answer.assert_called_once()


@pytest.mark.asyncio
async def test_callback_invalid_id():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()

    query = _make_query(data="vf:not-a-number")
    update = SimpleNamespace(callback_query=query)
    await adapter._handle_callback_query(update, None)

    adapter.handle_message.assert_not_called()


# ===========================================================================
# config.py bridging
# ===========================================================================

def test_config_bridges_feedback_button(monkeypatch, tmp_path):
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "feedback_button": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_FEEDBACK_BUTTON", "")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_FEEDBACK_BUTTON") == "true"


def test_config_feedback_button_env_precedence(monkeypatch, tmp_path):
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "feedback_button": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_FEEDBACK_BUTTON", "false")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_FEEDBACK_BUTTON") == "false"
