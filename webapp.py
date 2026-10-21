"""Mini App backend: static UI + JSON API authenticated by Telegram initData."""
import asyncio
import hashlib
import html
import json
import logging
import random
import re
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.utils.web_app import WebAppInitData, safe_parse_webapp_init_data
from aiohttp import web

import downloader
import games
from core import (
    PAIR_TITLE_KEY,
    cfg,
    chat_tone,
    deliver_video,
    game_title,
    is_chat_admin,
    is_chat_member,
    normalize_title,
    pair_title,
    parse_chat_param,
    parse_players,
    roll_hero,
    roll_pair,
    set_tone,
    store,
    today,
    who_answer,
)
from messages import TONES, raw, t

log = logging.getLogger("chatfun.web")
STATIC_DIR = Path(__file__).with_name("webapp")
INIT_DATA_MAX_AGE = 24 * 3600
BOT_KEY = web.AppKey("bot", Bot)
INIT_KEY = web.RequestKey("init", WebAppInitData)
_tasks: set[asyncio.Task] = set()


_ERRORS = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
}


def _json_error(status: int, code: str) -> web.HTTPException:
    return _ERRORS[status](text=json.dumps({"error": code}), content_type="application/json")


@web.middleware
async def auth(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    raw = request.headers.get("X-Init-Data", "")
    try:
        init = safe_parse_webapp_init_data(cfg.bot_token, raw)
    except ValueError:
        raise _json_error(401, "bad_init_data") from None
    if init.user is None or time.time() - init.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        raise _json_error(401, "bad_init_data")
    request[INIT_KEY] = init
    return await handler(request)


def _init(request: web.Request) -> WebAppInitData:
    return request[INIT_KEY]


def _bot(request: web.Request) -> Bot:
    return request.app[BOT_KEY]


async def _chat(request: web.Request, admin: bool = False) -> int:
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        raise _json_error(400, "bad_chat") from None
    user_id = _init(request).user.id
    check = is_chat_admin if admin else is_chat_member
    if not await check(_bot(request), chat_id, user_id):
        raise _json_error(403, "forbidden")
    return chat_id


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise _json_error(400, "bad_json") from None
    if not isinstance(data, dict):
        raise _json_error(400, "bad_json")
    return data


def _person(chat_id: int, user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    m = store.member(chat_id, user_id)
    return {"id": user_id, "name": m.name if m else str(user_id)}


async def me(request: web.Request) -> web.Response:
    init, bot = _init(request), _bot(request)
    global_admin = init.user.id in cfg.admin_ids
    chats = []
    if global_admin:
        # Global admins manage every chat the bot knows, member or not.
        chats = [{"id": cid, "title": title, "admin": True} for cid, title in store.all_chats()]
    else:
        for chat_id, title in store.user_chats(init.user.id):
            if await is_chat_member(bot, chat_id, init.user.id):
                admin = await is_chat_admin(bot, chat_id, init.user.id)
                chats.append({"id": chat_id, "title": title, "admin": admin})
    return web.json_response({
        "user": {"id": init.user.id, "name": init.user.first_name},
        "global_admin": global_admin,
        "chats": chats,
        "start_chat": parse_chat_param(init.start_param),
    })


async def chat_view(request: web.Request) -> web.Response:
    chat_id = await _chat(request)
    user_id = _init(request).user.id
    settings = store.settings(chat_id)
    mine = store.member(chat_id, user_id)
    return web.json_response({
        "id": chat_id,
        "title": store.chat_title(chat_id) or str(chat_id),
        "hero_enabled": settings["hero"],
        "game_title": game_title(chat_id),
        "today": _person(chat_id, store.pick_for(chat_id, today().isoformat())),
        "top": [{"id": uid, "name": name, "count": n} for uid, name, n in store.top(chat_id, 20)],
        "players": len(store.active_members(chat_id)),
        "me": {"active": bool(mine and mine.active)},
        "admin": await is_chat_admin(_bot(request), chat_id, user_id),
        "features": {k: settings[k] for k in ("hero", "pair", "stats", "random", "duels")},
        "pair": {
            "title": pair_title(chat_id),
            "today": _pair(chat_id, store.pair_for(chat_id, today().isoformat())),
            "top": [
                {"a": _person(chat_id, a), "b": _person(chat_id, b), "count": n}
                for a, b, n in store.pair_top(chat_id, 10)
            ],
        },
    })


def _pair(chat_id: int, pair: tuple[int, int] | None) -> list[dict] | None:
    return [_person(chat_id, uid) for uid in pair] if pair else None


def _plain(text: str) -> str:
    """Bot message HTML -> text for the Mini App."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def pair_roll(request: web.Request) -> web.Response:
    chat_id = await _chat(request)
    if not store.settings(chat_id)["pair"]:
        raise _json_error(409, "feature_off")
    pair, is_new = await roll_pair(_bot(request), chat_id)
    if pair is None:
        raise _json_error(409, "no_players")
    return web.json_response({"pair": _pair(chat_id, pair), "new": is_new})


async def stats_view(request: web.Request) -> web.Response:
    chat_id = await _chat(request)
    settings = store.settings(chat_id)
    period = games.parse_period(request.query.get("period"))
    rows, total = (
        store.activity_top(chat_id, games.period_since(period, today()), 20) if settings["stats"] else ([], 0)
    )
    duels = store.duel_stats(chat_id, 20) if settings["duels"] else []
    return web.json_response({
        "period": period,
        "total": total,
        "rows": [{"id": uid, "name": name, "count": n} for uid, name, n in rows],
        "duels": [{"id": uid, "name": name, "wins": w, "losses": lo} for uid, name, w, lo in duels],
    })


async def who(request: web.Request) -> web.Response:
    chat_id = await _chat(request)
    if not store.settings(chat_id)["random"]:
        raise _json_error(409, "feature_off")
    question = str((await _body(request)).get("question", ""))[:200]
    answer = who_answer(chat_id, question)
    if answer is None:
        raise _json_error(409, "no_players")
    user = _init(request).user
    asker = html.escape(" ".join(filter(None, [user.first_name, user.last_name])) or str(user.id))
    await _bot(request).send_message(chat_id, f"{asker}: {answer}")
    return web.json_response({"text": _plain(answer)})


async def roll(request: web.Request) -> web.Response:
    chat_id = await _chat(request)
    if not store.settings(chat_id)["hero"]:
        raise _json_error(409, "hero_off")
    winner_id, is_new = await roll_hero(_bot(request), chat_id)
    if winner_id is None:
        raise _json_error(409, "no_players")
    return web.json_response({"winner": _person(chat_id, winner_id), "new": is_new})


async def set_me(request: web.Request) -> web.Response:
    chat_id = await _chat(request)
    user = _init(request).user
    active = bool((await _body(request)).get("active"))
    name = " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)
    store.set_active(chat_id, user.id, name, active)
    return web.json_response({"active": active})


async def download(request: web.Request) -> web.Response:
    user_id = _init(request).user.id
    url = str((await _body(request)).get("url", "")).strip()
    found = downloader.extract_urls(url)
    if not found:
        raise _json_error(400, "unsupported_url")
    bot = _bot(request)
    try:
        await bot.send_chat_action(user_id, "upload_video")
    except TelegramForbiddenError:
        raise _json_error(409, "need_start") from None

    async def job():
        try:
            await deliver_video(bot, user_id, found[0])
        except TelegramAPIError:
            log.exception("mini app download %s failed", found[0])

    task = asyncio.create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return web.json_response({"queued": True}, status=202)


async def admin_view(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    return web.json_response({
        "settings": store.settings(chat_id),
        "hero_title": store.hero_title(chat_id) or "",
        "pair_title": store.custom_text(chat_id, PAIR_TITLE_KEY) or "",
        "tone": chat_tone(chat_id),
        "tones": [
            {"id": n, "name": raw("tone_names")[n], "sample": _plain(t(
                "hero_reveal", random.Random(n), tone=n, mention=t("tone_sample_name"),
                title=game_title(chat_id), title_lc=game_title(chat_id)[:1].lower() + game_title(chat_id)[1:],
            ))}
            for n in TONES
        ],
        "members": [
            {"id": m.user_id, "name": m.name, "active": m.active, "manual": m.user_id < 0}
            for m in store.all_members(chat_id)
        ],
    })


async def admin_settings(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    return web.json_response({"settings": store.update_settings(chat_id, await _body(request))})


async def admin_title(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    body = await _body(request)
    for key in ("title", "pair_title"):
        if key in body and body[key] is not None and not isinstance(body[key], str):
            raise _json_error(400, "bad_title")
    if "title" in body:
        store.set_hero_title(chat_id, normalize_title(body["title"]))
    if "pair_title" in body:
        store.set_custom_text(chat_id, PAIR_TITLE_KEY, normalize_title(body["pair_title"]))
    return web.json_response({
        "hero_title": store.hero_title(chat_id) or "",
        "game_title": game_title(chat_id),
        "pair_title": store.custom_text(chat_id, PAIR_TITLE_KEY) or "",
    })


async def admin_tone(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    tone = (await _body(request)).get("tone")
    if not isinstance(tone, int) or tone not in TONES:
        raise _json_error(400, "bad_tone")
    set_tone(chat_id, tone)
    return web.json_response({"tone": chat_tone(chat_id)})


async def admin_players(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    text = (await _body(request)).get("text")
    if not isinstance(text, str):
        raise _json_error(400, "bad_players")
    usernames, names = parse_players(text)
    added, skipped = store.add_manual(chat_id, usernames, names)
    return web.json_response({"added": added, "skipped": skipped})


async def admin_remove_member(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    try:
        user_id = int(request.match_info["user_id"])
    except ValueError:
        raise _json_error(400, "bad_user") from None
    if not store.remove_member(chat_id, user_id):
        raise _json_error(409, "not_manual")
    return web.json_response({"ok": True})


async def admin_member(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    try:
        user_id = int(request.match_info["user_id"])
    except ValueError:
        raise _json_error(400, "bad_user") from None
    if store.member(chat_id, user_id) is None:
        raise _json_error(404, "no_member")
    store.set_active(chat_id, user_id, None, bool((await _body(request)).get("active")))
    return web.json_response({"ok": True})


async def admin_reroll(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    store.clear_pick(chat_id, today().isoformat())
    return await roll(request)


async def admin_reset(request: web.Request) -> web.Response:
    chat_id = await _chat(request, admin=True)
    return web.json_response({"removed": store.reset_scores(chat_id)})


async def healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app(bot: Bot, index_html: str | None = None) -> web.Application:
    """index_html overrides the page (the local dev server injects test initData)."""
    page = index_html or (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Telegram webviews cache static files hard; a content hash in the URL busts it on every deploy.
    for name in ("app.js", "style.css", "ru.json"):
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        page = page.replace(f"/static/{name}", f"/static/{name}?v={digest}")

    async def index(_: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-cache"})

    app = web.Application(middlewares=[auth], client_max_size=64 * 1024)
    app[BOT_KEY] = bot
    app.add_routes([
        web.get("/api/me", me),
        web.get("/api/chats/{chat_id}", chat_view),
        web.post("/api/chats/{chat_id}/roll", roll),
        web.post("/api/chats/{chat_id}/me", set_me),
        web.post("/api/chats/{chat_id}/pair/roll", pair_roll),
        web.get("/api/chats/{chat_id}/stats", stats_view),
        web.post("/api/chats/{chat_id}/who", who),
        web.post("/api/download", download),
        web.get("/api/chats/{chat_id}/admin", admin_view),
        web.post("/api/chats/{chat_id}/admin/settings", admin_settings),
        web.post("/api/chats/{chat_id}/admin/title", admin_title),
        web.post("/api/chats/{chat_id}/admin/players", admin_players),
        web.post("/api/chats/{chat_id}/admin/tone", admin_tone),
        web.post("/api/chats/{chat_id}/admin/members/{user_id}/remove", admin_remove_member),
        web.post("/api/chats/{chat_id}/admin/members/{user_id}", admin_member),
        web.post("/api/chats/{chat_id}/admin/reroll", admin_reroll),
        web.post("/api/chats/{chat_id}/admin/reset", admin_reset),
        web.get("/", index),
        web.get("/healthz", healthz),
        web.static("/static", STATIC_DIR),
    ])
    return app
