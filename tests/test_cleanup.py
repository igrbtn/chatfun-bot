import asyncio
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Message

import core
import handlers
from storage import Store

CHAT = -100321654


@pytest.fixture
def store(monkeypatch):
    s = Store(":memory:")
    for mod in (core, handlers):
        monkeypatch.setattr(mod, "store", s)
    monkeypatch.setattr(handlers, "_command_times", {})
    s.upsert_chat(CHAT, "Chat")
    return s


def message(chat_id, message_id):
    return Message(message_id=message_id, date=0, chat=Chat(id=chat_id, type="supergroup"))


class SendMessage:  # names matter: the recorder looks at the method class name
    pass


class EditMessageText:
    pass


def run_recorder(method, result):
    async def make_request(bot, m):
        return SimpleNamespace(result=result)

    return asyncio.run(core.RecordSent()(make_request, None, method))


def test_recorder_tracks_sends_not_edits(store):
    run_recorder(SendMessage(), message(CHAT, 10))
    run_recorder(EditMessageText(), message(CHAT, 11))
    run_recorder(SendMessage(), message(42, 12))  # private chats are not tracked
    assert store.messages(CHAT) == [(10, "out")]


def test_recorder_keeps_existing_schedule(store):
    store.remember(CHAT, 10, "service", 0.0, delete_at=60.0)
    run_recorder(SendMessage(), message(CHAT, 10))
    assert store.due_for_delete(100.0) == [(CHAT, 10)]


def test_old_messages_expire_by_chosen_hours(store):
    store.remember_new(CHAT, 1, "out", 1000.0)
    store.remember_new(CHAT, 2, "out", 1000.0 + 3 * 3600)
    assert core.cleanup_hours(CHAT) == 0
    core.set_cleanup_hours(CHAT, 1)
    assert core.cleanup_hours(CHAT) == 1
    assert store.expire_older_than(CHAT, 1000.0 + 3 * 3600 - 3600, 99999.0) == 1
    assert store.due_for_delete(99999.0) == [(CHAT, 1)]
    core.set_cleanup_hours(CHAT, 5)  # not an allowed choice -> off
    assert core.cleanup_hours(CHAT) == 0


def test_commands_scheduled_only_when_enabled(store):
    async def handler(event, data):
        pass

    msg = SimpleNamespace(text="/hero", caption=None, message_id=77,
                          chat=SimpleNamespace(id=CHAT, type="supergroup"),
                          from_user=SimpleNamespace(id=1, is_bot=False))
    asyncio.run(handlers.AntiFlood()(handler, msg, {}))
    assert store.messages(CHAT) == []
    handlers._command_times.clear()
    store.update_settings(CHAT, {"cleanup_commands": True})
    asyncio.run(handlers.AntiFlood()(handler, msg, {}))
    assert store.messages(CHAT) == [(77, "command")]
    assert store.due_for_delete(10**12) == [(CHAT, 77)]
