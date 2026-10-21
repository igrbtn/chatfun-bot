import asyncio
import random

import pytest

import core
import handlers
import hero
from fakes import FakeBot
from storage import Member, Store

CHAT = -100777999


@pytest.fixture
def env(monkeypatch):
    store = Store(":memory:")
    for mod in (core, handlers):
        monkeypatch.setattr(mod, "store", store)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(core.asyncio, "sleep", lambda *_: real_sleep(0))
    for uid, name in ((1, "Ann"), (2, "Bob"), (3, "Cid")):
        store.touch(CHAT, uid, name)
    return store, FakeBot()


def test_activity_weight_shape():
    assert hero.activity_weight(0) == 1.0
    assert hero.activity_weight(25) == 6.0 and hero.activity_weight(100) == 11.0
    assert hero.push_success_chance(1.0, 3.0) == 0.75


def test_weighted_choice_prefers_chatty_and_reports_chance():
    members = [Member(1, "quiet"), Member(2, "chatty")]
    rng = random.Random(3)
    wins = [hero.choose(members, None, rng, {2: 9.0})[0].user_id for _ in range(2000)]
    assert 0.85 < wins.count(2) / len(wins) < 0.95  # weight 9 vs 1 -> 90%
    _, chance = hero.choose(members, None, random.Random(1), {2: 9.0})
    assert chance in (0.1, 0.9)
    _, flat = hero.choose(members, None, random.Random(1))
    assert flat == 0.5


def test_reveal_has_chance_and_first_time_line(env):
    store, bot = env
    store.update_settings(CHAT, {"drumroll": False})
    asyncio.run(core.roll_hero(bot, CHAT))
    reveal = [s for s in bot.sent if s[0] == "message"][-1][2]
    assert "%" in reveal and len(reveal.split("\n")) == 3  # announcement + chance + "first time"


def test_push_success_moves_title(env, monkeypatch):
    store, bot = env
    day = core.today().isoformat()
    store.save_pick(CHAT, day, 1)
    monkeypatch.setattr(core.rng, "random", lambda: 0.0)  # always succeeds
    assert asyncio.run(core.push_title(bot, CHAT, 1, 2)) is True
    assert store.pick_for(CHAT, day) == 2
    assert store.top(CHAT) == [(2, "Bob", 1)]
    with pytest.raises(core.PushError) as err:
        asyncio.run(core.push_title(bot, CHAT, 2, 3))
    assert err.value.code == "used"  # one attempt per chat per day
    edits = [e[2] for e in bot.edits]
    assert edits and edits[-1].count("\n") == 2  # call, fight, result in one message


def test_push_failure_is_a_double(env, monkeypatch):
    store, bot = env
    day = core.today().isoformat()
    store.save_pick(CHAT, day, 1)
    monkeypatch.setattr(core.rng, "random", lambda: 0.999)  # always fails
    assert asyncio.run(core.push_title(bot, CHAT, 1, 2)) is False
    assert store.pick_for(CHAT, day) == 1
    assert store.top(CHAT) == [(1, "Ann", 2)]
    assert store.leaders(CHAT) == (2, [1])


@pytest.mark.parametrize("hero_id, victim, code", [(2, 3, "not_hero"), (1, 1, "self"), (1, 42, "unknown")])
def test_push_rules(env, hero_id, victim, code):
    store, bot = env
    store.save_pick(CHAT, core.today().isoformat(), 1)
    with pytest.raises(core.PushError) as err:
        asyncio.run(core.push_title(bot, CHAT, hero_id, victim))
    assert err.value.code == code


def test_push_switched_off(env):
    store, bot = env
    store.save_pick(CHAT, core.today().isoformat(), 1)
    store.update_settings(CHAT, {"push": False})
    with pytest.raises(core.PushError) as err:
        asyncio.run(core.push_title(bot, CHAT, 1, 2))
    assert err.value.code == "off"


def test_milestones(env):
    store, _ = env
    day = core.today()
    store.save_pick(CHAT, day.isoformat(), 1)
    assert core.milestone_line(CHAT, 1) is not None  # first win
    for i in range(1, 5):
        store.save_pick(CHAT, (day - core.timedelta(days=40 + i)).isoformat(), 2)
    store.save_pick(CHAT, (day - core.timedelta(days=1)).isoformat(), 3)
    store.clear_pick(CHAT, day.isoformat())
    store.save_pick(CHAT, day.isoformat(), 2)
    line = core.milestone_line(CHAT, 2)  # 5th win -> count milestone
    assert line and "5" in line
