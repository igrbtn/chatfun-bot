import random
from functools import cache
from pathlib import Path

import yaml

TEXTS_PATH = Path(__file__).with_name("texts.yaml")
TONES_PATH = Path(__file__).with_name("tones.yaml")

TONES = (1, 2, 3, 4, 5)  # 1 = ultra toxic ... 5 = sickly sweet
DEFAULT_TONE = 3         # tone 3 is texts.yaml itself


@cache
def _texts() -> dict:
    return yaml.safe_load(TEXTS_PATH.read_text(encoding="utf-8"))


@cache
def _tones() -> dict:
    return yaml.safe_load(TONES_PATH.read_text(encoding="utf-8"))


def raw(key: str, tone: int | None = None):
    """Per-tone override from tones.yaml when the tone has one, otherwise the base text."""
    if tone is not None and tone != DEFAULT_TONE:
        value = _tones().get(tone, {}).get(key)
        if value is not None:
            return value
    return _texts()[key]


def t(key: str, rng: random.Random | None = None, tone: int | None = None, **kw) -> str:
    """Render a text by key; a list value means 'pick one variant at random'."""
    value = raw(key, tone)
    if isinstance(value, list):
        value = (rng or random).choice(value)
    return value.format(**kw)
