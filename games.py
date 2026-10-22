"""Pure game logic for chat entertainment commands (no Telegram, easy to test)."""
import random
from datetime import date, timedelta

from storage import Member

ROLL_DEFAULT = 100
ROLL_MAX = 1_000_000

# /stats argument -> period key; Russian aliases so people can type what they mean.
PERIODS = {
    "day": "day", "today": "day", "den": "day",
    "week": "week", "nedelya": "week",
    "all": "all", "vse": "all",
}
RU_PERIODS = {"день": "day", "сегодня": "day", "неделя": "week", "все": "all", "всё": "all"}


def choose_pair(members: list[Member], yesterday: tuple[int, int] | None, rng: random.Random) -> tuple[int, int] | None:
    """Two different members; yesterday's exact pair is avoided when anyone else is available."""
    if len(members) < 2:
        return None
    ids = [m.user_id for m in members]
    for _ in range(20):
        a, b = rng.sample(ids, 2)
        if yesterday is None or tuple(sorted((a, b))) != tuple(sorted(yesterday)) or len(ids) == 2:
            return a, b
    return a, b


def parse_roll(arg: str | None) -> int:
    """'/roll 6' -> 6; anything odd falls back to 1..100."""
    try:
        top = int((arg or "").split()[0])
    except (ValueError, IndexError):
        return ROLL_DEFAULT
    return top if 2 <= top <= ROLL_MAX else ROLL_DEFAULT


def parse_period(arg: str | None) -> str:
    word = (arg or "").strip().lower()
    return PERIODS.get(word) or RU_PERIODS.get(word) or "week"


def period_since(period: str, today: date) -> str | None:
    """First ISO day included in the period; None = all time. Week = last 7 days incl. today."""
    if period == "day":
        return today.isoformat()
    if period == "week":
        return (today - timedelta(days=6)).isoformat()
    return None


def classify_chatter(text: str, keywords: dict[str, list[str]]) -> str:
    """Kind of a message addressed to the bot: insult / thanks / hello / question / generic.
    Keywords are lowercase substrings from texts.yaml (chatter_keywords)."""
    low = text.lower()
    for kind in ("insult", "thanks", "hello"):
        if any(word in low for word in keywords.get(kind, [])):
            return kind
    return "question" if "?" in text else "generic"


EDGE_CHANCE = 0.04  # a coin landing on its edge: rare, but it has to be able to happen


def coin_tournament(players: list[int], rng: random.Random, edge: float = EDGE_CHANCE) -> tuple[list[tuple], int | None]:
    """Knockout of coin flips: the winner of each flip meets the next player. Returns
    ([(a, b, side, winner)], champion); side is "heads" (a wins), "tails" (b wins) or "edge".
    An edge stops everything: champion is None and every tied player gets a point."""
    rounds = []
    holder = players[0]
    for challenger in players[1:]:
        roll = rng.random()
        if roll < edge:
            rounds.append((holder, challenger, "edge", None))
            return rounds, None
        side = "heads" if roll < edge + (1 - edge) / 2 else "tails"
        winner = holder if side == "heads" else challenger
        rounds.append((holder, challenger, side, winner))
        holder = winner
    return rounds, holder
