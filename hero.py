import math
import random
from datetime import date, timedelta

from storage import Member, Store


def activity_weight(messages: int) -> float:
    """Chatty people risk more, but not hopelessly: 0 msgs -> 1, 25 -> 6, 100 -> 11."""
    return 1.0 + math.sqrt(max(messages, 0))


def choose(
    members: list[Member], yesterday_winner: int | None, rng: random.Random, weights: dict[int, float] | None = None
) -> tuple[Member, float]:
    """(winner, their chance). Yesterday's winner sits out unless they are the only one.
    Without weights everyone has the same odds."""
    pool = [m for m in members if m.user_id != yesterday_winner] or members
    w = [weights.get(m.user_id, 1.0) if weights else 1.0 for m in pool]
    winner = rng.choices(pool, weights=w, k=1)[0]
    return winner, w[pool.index(winner)] / sum(w)


def push_success_chance(hero_weight: float, victim_weight: float) -> float:
    """Odds that today's hero manages to push the title onto the victim: the chattier the victim, the easier."""
    return victim_weight / (hero_weight + victim_weight)


def pick_today(
    store: Store, chat_id: int, today: date, rng: random.Random, weights: dict[int, float] | None = None
) -> tuple[int | None, bool, float | None]:
    """(winner user_id, is_new, chance of a fresh pick). (None, False, None) when nobody is registered."""
    day = today.isoformat()
    existing = store.pick_for(chat_id, day)
    if existing is not None:
        return existing, False, None
    members = store.active_members(chat_id)
    if not members:
        return None, False, None
    yesterday = store.pick_for(chat_id, (today - timedelta(days=1)).isoformat())
    winner, chance = choose(members, yesterday, rng, weights)
    if not store.save_pick(chat_id, day, winner.user_id):
        return store.pick_for(chat_id, day), False, None
    return winner.user_id, True, chance
