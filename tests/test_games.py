import asyncio
import random
from datetime import date
from types import SimpleNamespace

import pytest

import core
import games
import handlers
from fakes import FakeBot
from storage import Member, Store

CHAT = -100900


@pytest.fixture
def env(monkeypatch):
    store = Store(":memory:")
    for mod in (core, handlers):
        monkeypatch.setattr(mod, "store", store)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(handlers.asyncio, "sleep", lambda *_: real_sleep(0))
    for uid, name in ((1, "Ann"), (2, "Bob"), (3, "Cid")):
        store.touch(CHAT, uid, name)
    return store, FakeBot()


def test_choose_pair_avoids_yesterday():
    members = [Member(1, "A"), Member(2, "B"), Member(3, "C")]
    for seed in range(30):
        a, b = games.choose_pair(members, (1, 2), random.Random(seed))
        assert a != b and sorted((a, b)) != [1, 2]
    assert games.choose_pair(members[:1], None, random.Random()) is None
    assert sorted(games.choose_pair(members[:2], (1, 2), random.Random(0))) == [1, 2]


def test_parsers():
    assert games.parse_roll("6") == 6
    assert games.parse_roll("abc") == games.parse_roll(None) == games.parse_roll("1") == 100
    assert games.parse_period("день") == games.parse_period("today") == "day"
    assert games.parse_period("все") == "all" and games.parse_period("") == "week"
    d = date(2026, 10, 17)
    assert games.period_since("day", d) == "2026-10-17"
    assert games.period_since("week", d) == "2026-10-11"
    assert games.period_since("all", d) is None


def test_activity_counts_by_period(env):
    store, _ = env
    for day, uid, n in (("2026-10-01", 1, 5), ("2026-10-17", 2, 3), ("2026-10-17", 1, 1)):
        for _ in range(n):
            store.count_message(CHAT, uid, day)
    assert store.activity_top(CHAT, "2026-10-17") == ([(2, "Bob", 3), (1, "Ann", 1)], 4)
    assert store.activity_top(CHAT, None) == ([(1, "Ann", 6), (2, "Bob", 3)], 9)


def test_pair_roll_once_a_day_and_top(env):
    store, bot = env
    pair, new = asyncio.run(core.roll_pair(bot, CHAT))
    assert new and len(set(pair)) == 2
    again, new2 = asyncio.run(core.roll_pair(bot, CHAT))
    assert (sorted(again), new2) == (sorted(pair), False)
    assert len([s for s in bot.sent if s[0] == "message"]) == 1
    assert store.pair_top(CHAT) == [(*sorted(pair), 1)]


def test_custom_pair_title(env):
    store, _ = env
    store.set_custom_text(CHAT, core.PAIR_TITLE_KEY, "Sweet <couple>")
    assert core.pair_vars(CHAT)["title"] == "Sweet &lt;couple&gt;"
    store.set_custom_text(CHAT, core.PAIR_TITLE_KEY, None)
    assert core.pair_title(CHAT) == core.t("pair_title_default")


def test_duel_storage_rules(env):
    store, _ = env
    d = store.create_duel(CHAT, 1, 2, now=100.0)
    assert store.pending_duel(CHAT, 1) == d
    assert store.finish_duel(d, "done", opponent=2, winner=2)
    assert not store.finish_duel(d, "done", opponent=3, winner=3)  # double tap
    store.finish_duel(store.create_duel(CHAT, 3, None, now=100.0), "done", opponent=1, winner=1)
    assert sorted(store.duel_stats(CHAT)) == [(1, "Ann", 1, 1), (2, "Bob", 1, 0), (3, "Cid", 0, 1)]
    stale = store.create_duel(CHAT, 2, None, now=1.0)
    assert [s[0] for s in store.stale_duels(older_than=50.0)] == [stale]


class FakeCallback:
    def __init__(self, data: str, user_id: int, name: str):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, full_name=name, username=None, is_bot=False)
        self.answers: list[str | None] = []
        self.edits: list[str] = []
        self.message = SimpleNamespace(edit_text=self._edit)

    async def _edit(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text=None, **kw):
        self.answers.append(text)


def test_duel_buttons(env):
    store, bot = env
    targeted = store.create_duel(CHAT, 1, 2, now=1e9)

    stranger = FakeCallback(f"duel:{targeted}:accept", 3, "Cid")
    asyncio.run(handlers.on_duel_button(stranger, bot))
    assert stranger.answers == [core.t("duel_not_you")] and store.duel(targeted)["status"] == "pending"

    own = FakeCallback(f"duel:{targeted}:accept", 1, "Ann")
    asyncio.run(handlers.on_duel_button(own, bot))
    assert own.answers == [core.t("duel_self")]

    accept = FakeCallback(f"duel:{targeted}:accept", 2, "Bob")
    asyncio.run(handlers.on_duel_button(accept, bot))
    duel = store.duel(targeted)
    assert duel["status"] == "done" and duel["winner"] in (1, 2) and len(accept.edits) == 2
    assert all("tg://" not in e and "@" not in e for e in accept.edits)  # follow-ups do not ping

    late = FakeCallback(f"duel:{targeted}:accept", 2, "Bob")
    asyncio.run(handlers.on_duel_button(late, bot))
    assert late.answers == [core.t("duel_over")]

    open_duel = store.create_duel(CHAT, 3, None, now=1e9)
    decline = FakeCallback(f"duel:{open_duel}:decline", 1, "Ann")
    asyncio.run(handlers.on_duel_button(decline, bot))
    assert decline.answers == [core.t("duel_not_you")]
    cancel = FakeCallback(f"duel:{open_duel}:decline", 3, "Cid")
    asyncio.run(handlers.on_duel_button(cancel, bot))
    assert store.duel(open_duel)["status"] == "cancelled"

    bogus = FakeCallback("duel:x:accept", 1, "Ann")
    asyncio.run(handlers.on_duel_button(bogus, bot))
    assert bogus.answers == [core.t("duel_over")]


def test_who_answer(env):
    _store, _ = env
    text = core.who_answer(CHAT, "  кто проставляется? ")
    assert text.startswith("кто проставляется?") or "кто проставляется" in text
    assert core.who_answer(-1, "x") is None
