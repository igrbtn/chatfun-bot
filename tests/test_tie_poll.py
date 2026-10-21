import asyncio

import pytest

import core
from fakes import FakeBot
from storage import Store

CHAT = -100777


@pytest.fixture
def env(monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(core, "store", store)
    for uid, name in ((1, "Ann"), (2, "Bob"), (3, "Cid")):
        store.touch(CHAT, uid, name, username=None if uid == 3 else name.lower() + "_xx")
    return store, FakeBot()


def test_leaders_order_by_who_reached_first(env):
    store, _ = env
    assert store.leaders(CHAT) == (0, [])
    store.save_pick(CHAT, "2026-10-01", 2)
    store.save_pick(CHAT, "2026-10-02", 1)
    store.save_pick(CHAT, "2026-10-03", 1)
    store.save_pick(CHAT, "2026-10-04", 2)
    assert store.leaders(CHAT) == (2, [1, 2])  # Ann had 2 wins first


def test_poll_winner_rules():
    assert core.poll_winner([1, 2], [3, 5]) == (2, True)
    assert core.poll_winner([1, 2], [4, 4]) == (1, False)
    assert core.poll_winner([1, 2], [0, 0]) == (1, False)
    assert core.poll_winner([1, 2], []) == (1, False)


def test_tie_poll_once_per_tie_then_closed(env, monkeypatch):
    store, bot = env
    store.save_pick(CHAT, "2026-10-01", 1)
    store.save_pick(CHAT, "2026-10-02", 2)
    assert asyncio.run(core.maybe_tie_poll(bot, CHAT))
    assert not asyncio.run(core.maybe_tie_poll(bot, CHAT))
    poll = [s for s in bot.sent if s[0] == "poll"]
    assert len(poll) == 1 and poll[0][3] == ["@ann_xx", "@bob_xx"]

    bot.poll_votes = [1, 3]
    monkeypatch.setattr(core.time, "time", lambda: 10**12)
    (chat, key, mid, options), = store.due_tie_polls(10**12)
    asyncio.run(core.close_tie_poll(bot, chat, key, mid, options))
    assert "@bob_xx" in bot.sent[-1][2]
    assert store.due_tie_polls(10**12) == []


def test_draw_goes_to_previous_leader_and_setting_off(env):
    store, bot = env
    store.save_pick(CHAT, "2026-10-01", 3)
    store.save_pick(CHAT, "2026-10-02", 1)
    store.update_settings(CHAT, {"tie_poll": False})
    assert not asyncio.run(core.maybe_tie_poll(bot, CHAT))
    store.update_settings(CHAT, {"tie_poll": True})
    assert asyncio.run(core.maybe_tie_poll(bot, CHAT))
    bot.poll_votes = [2, 2]
    (chat, key, mid, options), = store.due_tie_polls(10**12)
    asyncio.run(core.close_tie_poll(bot, chat, key, mid, options))
    assert "<b>Cid</b>" in bot.sent[-1][2] or "tg://user?id=3" in bot.sent[-1][2]
