import asyncio
import random
import string
from types import SimpleNamespace

import pytest

import core
import handlers
from fakes import FakeBot
from messages import DEFAULT_TONE, TONES, _texts, _tones, raw, t
from storage import Store

CHAT = -100555


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def variants(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    flat = []
    for item in value:
        flat.extend(variants(item))
    return flat


def test_every_tone_override_uses_only_known_placeholders():
    base = _texts()
    for tone, overrides in _tones().items():
        assert tone in TONES and tone != DEFAULT_TONE
        for key, value in overrides.items():
            assert key in base, f"tone {tone}: {key} has no base text"
            allowed = set().union(*(fields(v) for v in variants(base[key])))
            if key in ("hero_reveal", "hero_already", "tie_winner", "tie_winner_draw", "pair_reveal", "pair_already"):
                allowed |= {"title", "title_lc"}
            for text in variants(value):
                assert fields(text) <= allowed, f"tone {tone}: {key}: {text}"


def test_every_tone_renders_announcements():
    kw = {"mention": "M", "title": "T", "title_lc": "t", "a": "A", "b": "B", "winner": "W", "loser": "L",
          "question": "Q", "score": 2}
    for tone in TONES:
        for key in ("hero_reveal", "pair_reveal", "who_answer", "duel_result", "duel_self_shot", "tie_winner",
                    "dl_phrases", "ball_answers", "hero_already", "pair_already", "duel_call", "duel_fight"):
            for text in variants(raw(key, tone)):
                assert text.format(**kw)
        assert all(len(lines) == 3 for lines in raw("hero_drumroll", tone))


def test_tones_really_differ_and_fall_back():
    rng = random.Random(1)
    toxic = set(variants(raw("hero_reveal", 1)))
    sweet = set(variants(raw("hero_reveal", 5)))
    assert toxic and sweet and not toxic & sweet
    assert raw("hero_title_default", 1) == raw("hero_title_default")  # untouched keys fall back
    assert t("coin_heads", rng, tone=1) == t("coin_heads")


@pytest.fixture
def env(monkeypatch):
    store = Store(":memory:")
    for mod in (core, handlers):
        monkeypatch.setattr(mod, "store", store)
    store.touch(CHAT, 1, "Ann")
    return store, FakeBot()


def test_chat_tone_setting(env):
    store, _ = env
    assert core.chat_tone(CHAT) == DEFAULT_TONE
    core.set_tone(CHAT, 1)
    assert core.chat_tone(CHAT) == 1
    assert core.tt(CHAT, "hero_reveal", random.Random(0), mention="x", title="T", title_lc="t") in [
        v.format(mention="x", title="T", title_lc="t") for v in raw("hero_reveal", 1)
    ]
    core.set_tone(CHAT, 3)
    assert store.custom_text(CHAT, "tone") is None
    core.set_tone(CHAT, 9)
    assert core.chat_tone(CHAT) == DEFAULT_TONE


def test_self_duel_is_a_loss(env):
    store, bot = env
    replies = []

    async def reply(text, **kw):
        replies.append(text)
        return SimpleNamespace(message_id=1)

    me = SimpleNamespace(id=1, is_bot=False, full_name="Ann", username=None)
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup"),
        from_user=me,
        reply_to_message=SimpleNamespace(from_user=me),
        reply=reply,
    )
    asyncio.run(handlers.cmd_duel(msg, bot))
    assert len(replies) == 1 and "Ann" in replies[0] and "tg://" not in replies[0]
    assert store.duel_stats(CHAT) == [(1, "Ann", 0, 1)]
    assert store.pending_duel(CHAT, 1) is None


def test_dockerfile_ships_every_text_file():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    copy_lines = [ln for ln in (root / "Dockerfile").read_text().splitlines() if ln.startswith("COPY")]
    for yaml_file in root.glob("*.yaml"):
        assert any(yaml_file.name in ln for ln in copy_lines), f"{yaml_file.name} is not copied into the image"


def test_no_gendered_suffixes():
    """Texts are masculine or neutral: no "(a)"-style feminine endings glued to words."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"(?<=[а-яёА-ЯЁ])\([а-яё]+\)")
    for name in ("texts.yaml", "tones.yaml"):
        found = pattern.findall((root / name).read_text(encoding="utf-8"))
        assert not found, f"{name}: {found}"
