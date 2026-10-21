import asyncio
from types import SimpleNamespace

import pytest

import core
import handlers
from storage import Store

CHAT = -100444


@pytest.fixture
def env(monkeypatch):
    store = Store(":memory:")
    for mod in (core, handlers):
        monkeypatch.setattr(mod, "store", store)
    monkeypatch.setattr(handlers, "_command_times", {})
    monkeypatch.setattr(handlers, "_active_downloads", {})
    return store


def test_command_window():
    handlers._command_times.clear()
    assert handlers.command_allowed(CHAT, 1, 100.0)
    assert not handlers.command_allowed(CHAT, 1, 101.0)        # within the 3 s gap
    assert handlers.command_allowed(CHAT, 2, 101.0)            # other person is independent
    allowed = [handlers.command_allowed(CHAT, 1, 100.0 + 4 * i) for i in range(1, 8)]
    assert allowed.count(True) == handlers.COMMAND_BURST - 1   # burst cap per minute
    assert handlers.command_allowed(CHAT, 1, 100.0 + 200)      # window slides
    handlers._command_times.clear()


def group_message(text, message_id=50, user_id=1):
    return SimpleNamespace(
        text=text, caption=None, message_id=message_id,
        chat=SimpleNamespace(id=CHAT, type="supergroup"),
        from_user=SimpleNamespace(id=user_id, is_bot=False),
    )


def test_middleware_drops_and_schedules_spam_deletion(env):
    store = env
    calls = []

    async def handler(event, data):
        calls.append(event.message_id)

    mw = handlers.AntiFlood()
    asyncio.run(mw(handler, group_message("/duel", 1), {}))
    asyncio.run(mw(handler, group_message("/duel", 2), {}))
    asyncio.run(mw(handler, group_message("hello", 3), {}))  # plain text is never limited here
    assert calls == [1, 3]
    assert store.due_for_delete(10**12) == [(CHAT, 2)]


def test_service_reply_vanishes(env):
    store = env

    async def reply(text, **kw):
        return SimpleNamespace(message_id=77)

    msg = group_message("/hero", 60)
    msg.reply = reply
    asyncio.run(handlers.service_reply(msg, "already"))
    assert sorted(store.due_for_delete(10**12)) == [(CHAT, 60), (CHAT, 77)]
    assert store.due_for_delete(0) == []


def test_download_cap(env, monkeypatch):
    started = []
    gate = asyncio.Event()

    async def slow_delivery(bot, chat_id, url, reply_to=None, quiet_fail=False):
        started.append(url)
        await gate.wait()
        return True

    monkeypatch.setattr(handlers, "deliver_video", slow_delivery)

    def link_message(n):
        msg = group_message(f"https://youtu.be/v{n}", 100 + n)
        msg.entities = msg.caption_entities = None
        msg.reply_to_message = None
        return msg

    async def scenario():
        first = [asyncio.create_task(handlers.on_links(link_message(n), None)) for n in range(2)]
        await asyncio.sleep(0)
        await handlers.on_links(link_message(9), None)  # third parallel download from the same person
        gate.set()
        await asyncio.gather(*first)

    asyncio.run(scenario())
    assert started == ["https://youtu.be/v0", "https://youtu.be/v1"]
    assert handlers._active_downloads[(CHAT, 1)] == 0


def test_toxic_tone_snaps_back_once(env, monkeypatch):
    store = env
    monkeypatch.setattr(handlers, "_scolded", {})
    replies = []

    async def reply(text, **kw):
        replies.append(text)
        return SimpleNamespace(message_id=900 + len(replies))

    async def handler(event, data):
        pass

    def spam(n):
        msg = group_message("/duel", n)
        msg.reply = reply
        return msg

    mw = handlers.AntiFlood()
    store.touch(CHAT, 1, "Oleg")
    for n in range(4):  # normal tone: silent
        asyncio.run(mw(handler, spam(n), {}))
    assert replies == []

    handlers._command_times.clear()
    core.set_tone(CHAT, 1)
    for n in range(10, 15):
        asyncio.run(mw(handler, spam(n), {}))
    assert len(replies) == 1 and "<b>Oleg</b>" in replies[0]
    toxic = {v.format(name="<b>Oleg</b>") for v in core.raw("flood_scold", 1)}
    assert replies[0] in toxic
