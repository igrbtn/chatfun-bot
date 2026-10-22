import random
from datetime import date

import hero
from messages import raw
from storage import Member, Store


def make_store(*names):
    s = Store(":memory:")
    for i, n in enumerate(names, 1):
        s.touch(-100, i, n)
    return s


def test_pick_is_stable_within_a_day():
    s = make_store("Ann", "Bob", "Cid")
    d = date(2026, 10, 16)
    first, new, _ = hero.pick_today(s, -100, d, random.Random(1))
    assert new
    again, new2, _ = hero.pick_today(s, -100, d, random.Random(2))
    assert (again, new2) == (first, False)


def test_yesterday_winner_sits_out():
    members = [Member(1, "A"), Member(2, "B")]
    for seed in range(20):
        assert hero.choose(members, 1, random.Random(seed))[0].user_id == 2
    assert hero.choose([Member(1, "A")], 1, random.Random(0))[0].user_id == 1


def test_empty_chat():
    assert hero.pick_today(Store(":memory:"), -100, date(2026, 1, 1), random.Random()) == (None, False, None)


def test_opt_out_survives_touch():
    s = make_store("Ann", "Bob")
    assert s.set_active(-100, 1, "Ann", False)
    assert not s.set_active(-100, 1, "Ann", False)
    s.touch(-100, 1, "Ann K")
    assert [m.user_id for m in s.active_members(-100)] == [2]
    assert s.set_active(-100, 1, "Ann K", True)
    assert not s.set_active(-100, 3, "New", False)


def test_chats_are_isolated_and_top_counts():
    s = make_store("Ann", "Bob")
    s.touch(-200, 9, "Zed")
    s.save_pick(-100, "2026-10-01", 1)
    s.save_pick(-100, "2026-10-02", 2)
    s.save_pick(-100, "2026-10-03", 1)
    assert not s.save_pick(-100, "2026-10-03", 2)
    assert s.top(-100) == [(1, "Ann", 2), (2, "Bob", 1)]
    assert s.top(-200) == []
    assert [m.user_id for m in s.active_members(-200)] == [9]


def test_texts_render():
    for variant in raw("hero_reveal"):
        text = variant.format(mention="Ann", title="Toxic of the day", title_lc="toxic of the day")
        assert "Ann" in text and "oxic of the day" in text
    assert all(len(v) >= 2 for v in raw("hero_drumroll"))
    assert {"hero", "top", "reg", "unreg", "title", "add", "app", "help", "pair", "stats", "who", "duel"} <= set(raw("commands"))
    assert "{title}" in raw("help") and "{title}" in raw("top_title")


def test_title_survives_settings_updates():
    s = make_store("Ann")
    s.set_hero_title(-100, "Toxic of the day")
    s.update_settings(-100, {"hero": False})
    assert s.hero_title(-100) == "Toxic of the day"
    assert s.settings(-100)["hero"] is False
    s.set_hero_title(-100, None)
    assert s.hero_title(-100) is None


def test_normalize_title():
    from core import TITLE_MAX, normalize_title

    assert normalize_title("  Hero \n of   the\tday ") == "Hero of the day"
    assert normalize_title("   ") is None
    assert normalize_title(None) is None
    assert len(normalize_title("x" * 100)) == TITLE_MAX


def test_seen_without_auto_register_is_listed_but_not_playing():
    s = Store(":memory:")
    s.touch(-100, 7, "Admin", register=False)
    assert s.user_chats(7) == [(-100, "-100")]
    assert s.active_members(-100) == []
    assert s.set_active(-100, 7, "Admin", True)
    s.touch(-100, 7, "Admin", register=False)
    assert [m.user_id for m in s.active_members(-100)] == [7]


def test_username_mention_and_migration(tmp_path):
    import sqlite3

    import core

    old = tmp_path / "old.db"
    db = sqlite3.connect(old)
    db.execute(
        "CREATE TABLE members (chat_id INTEGER, user_id INTEGER, name TEXT, active INTEGER DEFAULT 1, "
        "PRIMARY KEY (chat_id, user_id))"
    )
    db.execute("INSERT INTO members VALUES (-1, 1, 'Ann', 1)")
    db.commit()
    db.close()
    s = Store(old)
    assert s.member(-1, 1).username is None
    s.touch(-1, 1, "Ann", username="ann_k")
    s.touch(-1, 2, "Bob")
    assert s.member(-1, 1).username == "ann_k"

    real = core.store
    core.store = s
    try:
        assert core.winner_mention(-1, 1) == "@ann_k"
        assert core.winner_mention(-1, 2).startswith('<a href="tg://user?id=2">')
    finally:
        core.store = real


def test_parse_players():
    from core import parse_players

    usernames, names = parse_players("@ann_k @Bob_22, @ann_k Sanya Pakha @abcd bad@ Ronin")
    assert usernames == ["ann_k", "Bob_22"]
    assert names == ["Sanya", "Pakha", "bad@", "Ronin"]


def test_manual_players_merge_into_real_account():
    s = Store(":memory:")
    s.touch(-1, 5, "Real Ann", username="Ann_K")
    added, skipped = s.add_manual(-1, ["ann_k", "bob"], ["Sanya"])
    assert added == ["@bob", "Sanya"] and skipped == ["@ann_k"]
    assert s.add_manual(-1, ["bob"], ["Sanya"]) == ([], ["@bob", "Sanya"])

    bob = next(m for m in s.all_members(-1) if m.username == "bob")
    assert bob.user_id < 0 and bob.active
    s.save_pick(-1, "2026-10-01", bob.user_id)
    s.touch(-1, 77, "Bob Real", username="Bob")
    assert s.member(-1, bob.user_id) is None
    assert s.member(-1, 77).name == "Bob Real"
    assert s.top(-1) == [(77, "Bob Real", 1)]

    sanya = next(m for m in s.all_members(-1) if m.name == "Sanya")
    assert not s.remove_member(-1, 77)
    assert s.remove_member(-1, sanya.user_id)


def test_manual_mention_without_username():
    import core

    s = Store(":memory:")
    s.add_manual(-1, ["bob"], ["Sanya", "<i>x"])
    real = core.store
    core.store = s
    try:
        ids = {m.name: m.user_id for m in s.all_members(-1)}
        assert core.winner_mention(-1, ids["@bob"]) == "@bob"
        assert core.winner_mention(-1, ids["Sanya"]) == "<b>Sanya</b>"
        assert core.winner_mention(-1, ids["<i>x"]) == "<b>&lt;i&gt;x</b>"
    finally:
        core.store = real


def test_quiet_name_never_pings():
    import core

    s = Store(":memory:")
    s.touch(-1, 5, "Ann K", username="ann_k")
    s.add_manual(-1, ["bobby"], ["Sanya"])
    real = core.store
    core.store = s
    try:
        ids = {m.name: m.user_id for m in s.all_members(-1)}
        for uid in ids.values():
            quiet = core.quiet_name(-1, uid)
            assert "@" not in quiet and "tg://" not in quiet and quiet.startswith("<b>")
        assert core.quiet_name(-1, ids["@bobby"]) == "<b>bobby</b>"
        assert core.winner_mention(-1, 5) == "@ann_k"  # first announcements still ping
    finally:
        core.store = real


def test_merging_placeholder_moves_pairs_and_duels():
    s = Store(":memory:")
    s.add_manual(-1, ["bobby"], [])
    placeholder = s.all_members(-1)[0].user_id
    s.touch(-1, 5, "Ann")
    s.save_pair(-1, "2026-10-01", placeholder, 5)
    d = s.create_duel(-1, placeholder, 5, 0.0)
    s.finish_duel(d, "done", opponent=5, winner=placeholder)
    s.touch(-1, 99, "Bobby Real", username="bobby")
    assert s.pair_top(-1) == [(5, 99, 1)]
    assert s.duel(d)["challenger"] == 99 and s.duel(d)["winner"] == 99


def test_orphans_from_old_versions_are_repaired(tmp_path):
    from storage import manual_id

    path = tmp_path / "old.db"
    s = Store(path)
    s.touch(-1, 5, "Ann")
    s.touch(-1, 99, "Bobby", username="Bobby")
    orphan = manual_id("u:bobby")
    s.save_pair(-1, "2026-10-01", orphan, 5)  # what an old merge left behind
    s.db.close()
    repaired = Store(path)
    assert repaired.pair_top(-1) == [(5, 99, 1)]


def test_list_names_do_not_ping():
    import handlers

    assert handlers.list_name("@Shtnrs") == "Shtnrs"
    assert handlers.list_name("Ann") == "Ann"
