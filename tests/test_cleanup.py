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


def test_only_own_commands_are_touched(store):
    assert handlers.is_own_command("/hero", "ChatFunBot")
    assert handlers.is_own_command("/duel@ChatFunBot", "ChatFunBot")
    assert handlers.is_own_command("/whatever@chatfunbot", "ChatFunBot")
    assert not handlers.is_own_command("/future@OtherBot", "ChatFunBot")
    assert not handlers.is_own_command("/pidor@ThirdBot", "ChatFunBot")
    assert not handlers.is_own_command("/future", "ChatFunBot")  # bare but not ours

    async def handler(event, data):
        pass

    class Bot:
        async def me(self):
            return SimpleNamespace(username="ChatFunBot")

    store.update_settings(CHAT, {"cleanup_commands": True})
    for n in range(5):  # someone spams another bot: no rate limit, no deletion
        msg = SimpleNamespace(text="/future@OtherBot", caption=None, message_id=200 + n,
                              chat=SimpleNamespace(id=CHAT, type="supergroup"),
                              from_user=SimpleNamespace(id=1, is_bot=False))
        asyncio.run(handlers.AntiFlood()(handler, msg, {"bot": Bot()}))
    assert store.messages(CHAT) == []


def test_deletions_are_counted(store):
    class Bot:
        def __init__(self):
            self.fail = set()

        async def delete_message(self, chat_id, message_id):
            from aiogram.exceptions import TelegramBadRequest

            if message_id in self.fail:
                raise TelegramBadRequest(method=None, message="message can't be deleted")

    bot = Bot()
    bot.fail = {3}
    store.remember(CHAT, 1, "command", 0.0, delete_at=0.0)
    store.remember(CHAT, 2, "push:42", 0.0, delete_at=0.0)
    store.remember(CHAT, 3, "out", 0.0, delete_at=0.0)
    store.remember(42, 4, "status", 0.0, delete_at=0.0)  # private chat: not counted
    results = [asyncio.run(core._safe_delete(bot, c, m)) for c, m in store.due_for_delete(10**12)]
    assert sorted(results) == [("command", True), ("out", False), ("push", True), ("status", True)]
    stats = store.deletion_stats(CHAT, core.today().isoformat())
    assert stats["today"] == {"deleted": 2, "failed": 1, "by_kind": {"command": 1, "push": 1}}
    assert stats["total"] == {"deleted": 2, "failed": 1}
    assert store.messages(CHAT) == []  # forgotten either way
