import random
from datetime import date, timedelta

from storage import Member, Store


def choose(members: list[Member], yesterday_winner: int | None, rng: random.Random) -> Member:
    """Random member; yesterday's winner sits out unless they are the only one."""
    pool = [m for m in members if m.user_id != yesterday_winner] or members
    return rng.choice(pool)


def pick_today(store: Store, chat_id: int, today: date, rng: random.Random) -> tuple[int | None, bool]:
    """Returns (winner user_id, is_new). (None, False) when nobody is registered."""
    day = today.isoformat()
    existing = store.pick_for(chat_id, day)
    if existing is not None:
        return existing, False
    members = store.active_members(chat_id)
    if not members:
        return None, False
    yesterday = store.pick_for(chat_id, (today - timedelta(days=1)).isoformat())
    winner = choose(members, yesterday, rng)
    if not store.save_pick(chat_id, day, winner.user_id):
        return store.pick_for(chat_id, day), False
    return winner.user_id, True
