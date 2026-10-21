import asyncio
import dataclasses
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

import core
import webapp
from fakes import FakeBot, sign_init_data
from storage import Store

TOKEN = os.environ["BOT_TOKEN"]
CHAT = -1001234
ADMIN, USER, STRANGER = 10, 20, 30
GLOBAL = 424242


@pytest.fixture
def env(monkeypatch):
    store = Store(":memory:")
    for mod in (core, webapp):
        monkeypatch.setattr(mod, "store", store)
    monkeypatch.setattr(core, "_status_cache", {})
    real_sleep = asyncio.sleep
    monkeypatch.setattr(core.asyncio, "sleep", lambda *_: real_sleep(0))
    store.upsert_chat(CHAT, "Test chat")
    store.touch(CHAT, ADMIN, "Admin")
    store.touch(CHAT, USER, "User")
    bot = FakeBot({(CHAT, ADMIN): "administrator", (CHAT, USER): "member"})
    return store, bot


def call(bot, user_id, method, path, body=None, init=None):
    async def run():
        async with TestClient(TestServer(webapp.build_app(bot))) as client:
            headers = {"X-Init-Data": init if init is not None else sign_init_data(
                TOKEN, {"id": user_id, "first_name": f"u{user_id}"}, start_param="c1234")}
            resp = await client.request(method, path, json=body, headers=headers)
            return resp.status, await resp.json()
    return asyncio.run(run())


def test_rejects_bad_signature(env):
    _, bot = env
    forged = sign_init_data("999:OTHER", {"id": ADMIN, "first_name": "x"})
    assert call(bot, ADMIN, "GET", "/api/me", init=forged)[0] == 401
    assert call(bot, ADMIN, "GET", "/api/me", init="")[0] == 401


def test_rejects_stale_init_data(env):
    _, bot = env
    old = sign_init_data(TOKEN, {"id": ADMIN, "first_name": "x"}, auth_date=1_000_000)
    assert call(bot, ADMIN, "GET", "/api/me", init=old)[0] == 401


def test_me_lists_chats_and_start_param(env):
    _, bot = env
    status, data = call(bot, USER, "GET", "/api/me")
    assert status == 200
    assert data["chats"] == [{"id": CHAT, "title": "Test chat", "admin": False}]
    assert data["start_chat"] == -1234


def test_stranger_cannot_read_chat(env):
    _, bot = env
    assert call(bot, STRANGER, "GET", f"/api/chats/{CHAT}")[0] == 403


def test_roll_announces_once(env):
    _store, bot = env
    status, data = call(bot, USER, "POST", f"/api/chats/{CHAT}/roll")
    assert status == 200 and data["new"]
    first = data["winner"]["id"]
    status, data = call(bot, USER, "POST", f"/api/chats/{CHAT}/roll")
    assert (data["winner"]["id"], data["new"]) == (first, False)
    reveals = [s for s in bot.sent if s[0] == "message" and str(first) in s[2]]
    assert len(reveals) == 1


def test_admin_only_endpoints(env):
    _store, bot = env
    assert call(bot, USER, "GET", f"/api/chats/{CHAT}/admin")[0] == 403
    status, data = call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/settings", {"hero": False, "bogus": 1})
    assert status == 200 and data["settings"]["hero"] is False and "bogus" not in data["settings"]
    assert call(bot, USER, "POST", f"/api/chats/{CHAT}/roll")[0] == 409


def test_admin_toggles_member_and_reroll(env):
    store, bot = env
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/members/{USER}", {"active": False})[0] == 200
    assert [m.user_id for m in store.active_members(CHAT)] == [ADMIN]
    status, data = call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/reroll")
    assert status == 200 and data["winner"]["id"] == ADMIN and data["new"]
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/reset")[1] == {"removed": 1}


def test_download_validates_url(env):
    _, bot = env
    assert call(bot, USER, "POST", "/api/download", {"url": "https://example.com/x"})[1] == {"error": "unsupported_url"}


def test_admin_sets_title_and_members_see_it(env):
    _store, bot = env
    assert call(bot, USER, "POST", f"/api/chats/{CHAT}/admin/title", {"title": "X"})[0] == 403
    status, data = call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/title", {"title": "  Toxic   of the day "})
    assert status == 200 and data["hero_title"] == "Toxic of the day"
    assert call(bot, USER, "GET", f"/api/chats/{CHAT}")[1]["game_title"] == "Toxic of the day"
    status, data = call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/title", {"title": ""})
    assert data["hero_title"] == "" and data["game_title"] != "Toxic of the day"
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/title", {"title": 5})[0] == 400


def test_title_used_in_reveal(env):
    store, bot = env
    store.set_hero_title(CHAT, "Toxic <b>of</b> the day")
    call(bot, USER, "POST", f"/api/chats/{CHAT}/roll")
    reveal = bot.sent[-1][2]
    assert "of</b>" not in reveal and "&lt;b&gt;" in reveal


def test_global_admin_sees_all_chats(env, monkeypatch):
    store, bot = env
    cfg = dataclasses.replace(core.cfg, admin_ids=frozenset({GLOBAL}))
    for mod in (core, webapp):
        monkeypatch.setattr(mod, "cfg", cfg)
    store.upsert_chat(-1009, "Other chat")
    _status, data = call(bot, GLOBAL, "GET", "/api/me")
    assert data["global_admin"] is True
    assert {c["id"] for c in data["chats"]} == {CHAT, -1009}
    assert call(bot, GLOBAL, "POST", "/api/chats/-1009/admin/title", {"title": "Hero"})[0] == 200
    assert call(bot, USER, "GET", "/api/me")[1]["global_admin"] is False


def test_static_urls_are_versioned(env):
    _, bot = env

    async def run():
        async with TestClient(TestServer(webapp.build_app(bot))) as client:
            page = await (await client.get("/")).text()
            js = await client.get(page.split('src="')[-1].split('"')[0])
            return page, js.status
    page, js_status = asyncio.run(run())
    assert all(f"/static/{n}?v=" in page for n in ("app.js", "style.css", "ru.json"))
    assert js_status == 200


def test_admin_adds_and_removes_manual_players(env):
    _store, bot = env
    assert call(bot, USER, "POST", f"/api/chats/{CHAT}/admin/players", {"text": "@bobby"})[0] == 403
    status, data = call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/players", {"text": "@bobby Sanya"})
    assert status == 200 and data == {"added": ["@bobby", "Sanya"], "skipped": []}
    members = call(bot, ADMIN, "GET", f"/api/chats/{CHAT}/admin")[1]["members"]
    manual = [m for m in members if m["manual"]]
    assert {m["name"] for m in manual} == {"@bobby", "Sanya"}
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/members/{manual[0]['id']}/remove")[0] == 200
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/members/{USER}/remove")[0] == 409


def test_pair_stats_who_endpoints(env):
    store, bot = env
    status, data = call(bot, USER, "POST", f"/api/chats/{CHAT}/pair/roll")
    assert status == 200 and data["new"] and len(data["pair"]) == 2
    view = call(bot, USER, "GET", f"/api/chats/{CHAT}")[1]
    assert view["pair"]["today"] == data["pair"] and view["features"]["duels"] is True

    store.count_message(CHAT, USER, core.today().isoformat())
    stats = call(bot, USER, "GET", f"/api/chats/{CHAT}/stats?period=day")[1]
    assert stats["total"] == 1 and stats["rows"][0]["id"] == USER and stats["period"] == "day"

    status, data = call(bot, USER, "POST", f"/api/chats/{CHAT}/who", {"question": "who pays <b>?"})
    assert status == 200 and "who pays <b>" in data["text"]  # plain text for the app
    assert bot.sent[-1][1] == CHAT and "who pays &lt;b&gt;" in bot.sent[-1][2]  # escaped for the group

    store.update_settings(CHAT, {"random": False, "pair": False})
    assert call(bot, USER, "POST", f"/api/chats/{CHAT}/who", {"question": "x"})[1] == {"error": "feature_off"}
    assert call(bot, USER, "POST", f"/api/chats/{CHAT}/pair/roll")[0] == 409


def test_admin_pair_title(env):
    _, bot = env
    data = call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/title", {"pair_title": "Sweet couple"})[1]
    assert data["pair_title"] == "Sweet couple" and data["game_title"]
    assert call(bot, USER, "GET", f"/api/chats/{CHAT}")[1]["pair"]["title"] == "Sweet couple"


def test_admin_tone(env):
    _, bot = env
    assert call(bot, USER, "POST", f"/api/chats/{CHAT}/admin/tone", {"tone": 1})[0] == 403
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/tone", {"tone": 7})[0] == 400
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/admin/tone", {"tone": 5})[1] == {"tone": 5}
    admin = call(bot, ADMIN, "GET", f"/api/chats/{CHAT}/admin")[1]
    assert admin["tone"] == 5 and [x["id"] for x in admin["tones"]] == [1, 2, 3, 4, 5]
    assert all(x["sample"] and "<" not in x["sample"] for x in admin["tones"])


def test_push_endpoint(env):
    store, bot = env
    store.save_pick(CHAT, core.today().isoformat(), USER)
    view = call(bot, USER, "GET", f"/api/chats/{CHAT}")[1]
    assert view["push"]["available"] and {t["id"] for t in view["push"]["targets"]} == {ADMIN}
    assert call(bot, ADMIN, "GET", f"/api/chats/{CHAT}")[1]["push"]["available"] is False
    assert call(bot, ADMIN, "POST", f"/api/chats/{CHAT}/push", {"victim": USER})[1] == {"error": "push_not_hero"}
    status, data = call(bot, USER, "POST", f"/api/chats/{CHAT}/push", {"victim": ADMIN})
    assert status == 200 and isinstance(data["success"], bool)
    assert call(bot, USER, "POST", f"/api/chats/{CHAT}/push", {"victim": ADMIN})[0] == 409
