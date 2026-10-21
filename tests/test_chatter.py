import asyncio
from types import SimpleNamespace

import pytest

import core
import games
import handlers
from fakes import FakeBot
from messages import raw, t
from storage import Store

CHAT = -100321
BOT_ID = 999


def test_classify_chatter():
    kw = raw("chatter_keywords")
    assert games.classify_chatter("ты лох", kw) == "insult"
    assert games.classify_chatter("Иди НАХУЙ, бот", kw) == "insult"
    assert games.classify_chatter("спасибо!", kw) == "thanks"
    assert games.classify_chatter("Привет всем", kw) == "hello"
    assert games.classify_chatter("а ты кто?", kw) == "question"
    assert games.classify_chatter("просто так", kw) == "generic"


class ChatterBot(FakeBot):
    async def me(self):
        return SimpleNamespace(id=BOT_ID, username="ChatFunBot")


def make_message(text, reply_to_bot=False, chat_type="supergroup"):
    replies = []

    async def reply(answer, **kw):
        replies.append(answer)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID)) if reply_to_bot else None
    msg = SimpleNamespace(
        text=text, caption=None, entities=None, caption_entities=None,
        chat=SimpleNamespace(id=CHAT, type=chat_type),
        from_user=SimpleNamespace(id=1, is_bot=False, full_name="Ann", username="ann_k"),
        reply_to_message=reply_to,
        message_id=10,
        reply=reply,
    )
    return msg, replies


@pytest.fixture
def env(monkeypatch):
    store = Store(":memory:")
    for mod in (core, handlers):
        monkeypatch.setattr(mod, "store", store)
    monkeypatch.setattr(handlers, "_last_chatter", {})
    store.touch(CHAT, 1, "Ann", username="ann_k")
    return store, ChatterBot()


def run(msg, bot):
    asyncio.run(handlers.on_links(msg, bot))


def test_replies_only_when_addressed(env):
    _, bot = env
    msg, replies = make_message("просто болтаю")
    run(msg, bot)
    assert replies == []

    msg, replies = make_message("ты лох", reply_to_bot=True)
    run(msg, bot)
    assert len(replies) == 1 and "<b>Ann</b>" in replies[0] and "@ann_k" not in replies[0]


def test_mention_question_gets_ball_answer(env, monkeypatch):
    _, bot = env
    msg, replies = make_message("@ChatFunBot будет дождь?")
    run(msg, bot)
    answers = {a.format(name="x") for a in raw("ball_answers")}
    assert replies and replies[0] in answers


def test_cooldown_and_toggle(env, monkeypatch):
    store, bot = env
    msg, replies = make_message("привет", reply_to_bot=True)
    run(msg, bot)
    run(msg, bot)
    assert len(replies) == 1  # second one inside the cooldown

    monkeypatch.setattr(handlers, "_last_chatter", {})
    store.update_settings(CHAT, {"chatter": False})
    msg, replies = make_message("привет", reply_to_bot=True)
    run(msg, bot)
    assert replies == []


def test_commands_and_tone(env):
    _store, bot = env
    msg, replies = make_message("/unknown@ChatFunBot", reply_to_bot=True)
    run(msg, bot)
    assert replies == []

    core.set_tone(CHAT, 1)
    msg, replies = make_message("отвали", reply_to_bot=True)
    run(msg, bot)
    toxic = {v.format(name="<b>Ann</b>") for v in raw("chatter_insult", 1)}
    assert replies and replies[0] in toxic
    assert t("chatter_generic", name="x")
