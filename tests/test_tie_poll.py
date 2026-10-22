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


class FixedRng:
    """Feeds coin_tournament fixed rolls: < 0.04 edge, < 0.52 heads, else tails."""

    def __init__(self, rolls):
        self.rolls = list(rolls)

    def random(self):
        return self.rolls.pop(0)


def test_coin_tournament_rules():
    import games

    rounds, champ = games.coin_tournament([1, 2, 3], FixedRng([0.3, 0.9]))
    assert rounds == [(1, 2, "heads", 1), (1, 3, "tails", 3)] and champ == 3
    rounds, champ = games.coin_tournament([1, 2, 3], FixedRng([0.3, 0.01]))
    assert rounds[-1][2] == "edge" and champ is None and len(rounds) == 2


@pytest.fixture
def coin_env(env, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(core.asyncio, "sleep", lambda *_: real_sleep(0))
    monkeypatch.setattr(core.rng, "shuffle", lambda seq: None)  # keep leader order for predictable rounds
    return env


def test_tie_settled_by_coin_once(coin_env, monkeypatch):
    store, bot = coin_env
    store.save_pick(CHAT, "2026-10-01", 1)
    store.save_pick(CHAT, "2026-10-02", 2)
    monkeypatch.setattr(core.rng, "random", lambda: 0.9)  # tails: second player wins
    assert asyncio.run(core.settle_tie(bot, CHAT))
    assert store.leaders(CHAT) == (2, [2])
    messages = [s for s in bot.sent if s[0] == "message"]
    assert len(messages) == 2  # growing toss message + separate winner announcement that pings
    assert "@bob_xx" in messages[-1][2]
    assert bot.edits and bot.edits[-1][2].count("\n") == 1  # start line + one round
    assert not asyncio.run(core.settle_tie(bot, CHAT))  # nobody shares first place any more
    assert not [s for s in bot.sent if s[0] == "poll"]


def test_edge_gives_everyone_a_point_and_stops(coin_env, monkeypatch):
    store, bot = coin_env
    for day, uid in (("2026-10-01", 1), ("2026-10-02", 2), ("2026-10-03", 3)):
        store.save_pick(CHAT, day, uid)
    monkeypatch.setattr(core.rng, "random", lambda: 0.01)  # first flip lands on its edge
    assert asyncio.run(core.settle_tie(bot, CHAT))
    assert store.leaders(CHAT) == (2, [1, 2, 3])
    assert bot.edits[-1][2].count("\n") == 1  # stopped after the first flip


def test_tie_coin_switched_off(coin_env):
    store, bot = coin_env
    store.save_pick(CHAT, "2026-10-01", 1)
    store.save_pick(CHAT, "2026-10-02", 2)
    store.update_settings(CHAT, {"tie_poll": False})
    assert not asyncio.run(core.settle_tie(bot, CHAT))
