"""Shared state and actions used by both the chat handlers and the Mini App API."""
import asyncio
import html
import logging
import random
import re
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatAction, ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import FSInputFile, Message, ReplyParameters, User

import config
import downloader
import games
import hero
from messages import DEFAULT_TONE, TONES, raw, t
from storage import Store

log = logging.getLogger("chatfun")
cfg = config.load()
cfg.data_dir.mkdir(parents=True, exist_ok=True)
store = Store(cfg.data_dir / "chatfun.db")
rng = random.Random()
# Downloads are heavy (ffmpeg merge); keep at most two at once.
dl_slots = asyncio.Semaphore(2)

ADMIN_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
MEMBER_STATUSES = ADMIN_STATUSES | {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}
_status_cache: dict[tuple[int, int], tuple[float, str | None]] = {}
STATUS_TTL = 60


def chat_tone(chat_id: int) -> int:
    """1 = ultra toxic ... 5 = sickly sweet; 3 (default) is the plain texts.yaml."""
    tone = store.custom_text(chat_id, "tone")
    return tone if tone in TONES else DEFAULT_TONE


def set_tone(chat_id: int, tone: int) -> None:
    store.set_custom_text(chat_id, "tone", tone if tone in TONES and tone != DEFAULT_TONE else None)


def tt(chat_id: int, key: str, rng_: random.Random | None = None, **kw) -> str:
    """t() in the chat's tone."""
    return t(key, rng_, tone=chat_tone(chat_id), **kw)


def today() -> date:
    return datetime.now(cfg.tz).date()


def display_name(user: User) -> str:
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def chat_param(chat_id: int) -> str:
    """startapp payload allows only [A-Za-z0-9_-]; group ids are negative."""
    return f"c{-chat_id}" if chat_id < 0 else f"p{chat_id}"


def parse_chat_param(param: str | None) -> int | None:
    if param and len(param) > 1 and param[1:].isdigit():
        return -int(param[1:]) if param[0] == "c" else int(param[1:])
    return None


async def member_status(bot: Bot, chat_id: int, user_id: int) -> str | None:
    key = (chat_id, user_id)
    hit = _status_cache.get(key)
    if hit and time.monotonic() - hit[0] < STATUS_TTL:
        return hit[1]
    try:
        status = (await bot.get_chat_member(chat_id, user_id)).status
    except TelegramAPIError:
        status = None
    _status_cache[key] = (time.monotonic(), status)
    return status


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    return user_id in cfg.admin_ids or await member_status(bot, chat_id, user_id) in ADMIN_STATUSES


async def is_chat_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    return user_id in cfg.admin_ids or await member_status(bot, chat_id, user_id) in MEMBER_STATUSES


TITLE_MAX = 40


def normalize_title(raw: str | None) -> str | None:
    """Admin-entered game name: one line, collapsed spaces, capped; empty -> None (default)."""
    if not raw:
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in raw).split())
    return text[:TITLE_MAX] or None


def game_title(chat_id: int) -> str:
    return store.hero_title(chat_id) or t("hero_title_default")


def title_vars(chat_id: int) -> dict:
    """{title} and {title_lc} for texts.yaml, HTML-escaped (bot messages use HTML parse mode)."""
    title = game_title(chat_id)
    lowered = title[:1].lower() + title[1:]
    return {"title": html.escape(title), "title_lc": html.escape(lowered)}


USERNAME_RE = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{4,31})")  # Telegram: 5-32 chars
NAME_MAX = 40
PLAYERS_MAX = 100


def parse_players(text: str) -> tuple[list[str], list[str]]:
    """'@ann @bob Sanya' -> (['ann', 'bob'], ['Sanya']). @-words are usernames, other words are names."""
    usernames, names = [], []
    for word in text.replace(",", " ").split()[:PLAYERS_MAX]:
        if word.startswith("@"):
            m = USERNAME_RE.fullmatch(word)
            if m and m.group(1) not in usernames:
                usernames.append(m.group(1))
        else:
            name = normalize_title(word)
            if name and name not in names:
                names.append(name[:NAME_MAX])
    return usernames, names


def quiet_name(chat_id: int, user_id: int) -> str:
    """Bold name with no @ and no link: Telegram sends no notification. For repeats and follow-ups,
    where the person was already pinged once."""
    member = store.member(chat_id, user_id)
    name = (member.name if member else str(user_id)).lstrip("@")
    return f"<b>{html.escape(name)}</b>"


def winner_mention(chat_id: int, user_id: int) -> str:
    """@username when known (Telegram pings it), a clickable name for real users, plain bold name
    for admin-added players without a username (no id to link to)."""
    member = store.member(chat_id, user_id)
    if member and member.username:
        return f"@{html.escape(member.username)}"
    name = member.name if member else str(user_id)
    if user_id < 0:
        return f"<b>{html.escape(name)}</b>"
    return mention(user_id, name)


MILESTONES = (5, 10, 25, 50, 100, 200, 365)
COMEBACK_DAYS = 30


def activity_weights(chat_id: int) -> dict[int, float]:
    """Weights from yesterday's and today's message counts (players with no messages weigh 1)."""
    since = (today() - timedelta(days=1)).isoformat()
    return {uid: hero.activity_weight(n) for uid, n in store.messages_since(chat_id, since).items()}


def day_champion(chat_id: int) -> int | None:
    """Who holds "X of the day" after today's whole fuss (pick, coin bonuses, pushes, doubles)."""
    rows = store.day_points(chat_id, today().isoformat())
    return rows[0][0] if rows else None


def milestone_line(chat_id: int, user_id: int) -> str | None:
    """One extra line for a fresh hero, if there is something to celebrate (or mock)."""
    day = today().isoformat()
    who = quiet_name(chat_id, user_id)
    wins = store.wins(chat_id, user_id)
    if wins == 1:
        return tt(chat_id, "hero_first", rng, mention=who)
    if wins in MILESTONES:
        return tt(chat_id, "hero_count", rng, mention=who, count=wins)
    last = store.last_win_before(chat_id, user_id, day)
    if last and (today() - date.fromisoformat(last)).days >= COMEBACK_DAYS:
        return tt(chat_id, "hero_comeback", rng, mention=who, days=(today() - date.fromisoformat(last)).days)
    best, tied = store.leaders(chat_id)
    if user_id in tied and best > 1:
        key = "hero_leader" if len(tied) == 1 else "hero_caught"
        return tt(chat_id, key, rng, mention=who, count=best)
    return None


async def roll_hero(bot: Bot, chat_id: int) -> tuple[int | None, bool]:
    """Pick today's hero; a fresh pick is announced in the group."""
    weights = activity_weights(chat_id) if store.settings(chat_id)["weighted"] else None
    winner_id, is_new, chance = hero.pick_today(store, chat_id, today(), rng, weights)
    if winner_id is not None and is_new:
        if store.settings(chat_id)["drumroll"]:
            # One message that grows line by line; the reveal stays separate so the winner gets pinged.
            lines = rng.choice(raw("hero_drumroll", chat_tone(chat_id)))
            drum = await bot.send_message(chat_id, lines[0])
            for i in range(1, len(lines)):
                await asyncio.sleep(suspense())
                try:
                    await bot.edit_message_text("\n".join(lines[: i + 1]), chat_id=chat_id, message_id=drum.message_id)
                except TelegramAPIError:
                    pass
            await asyncio.sleep(suspense())
        lines = [tt(chat_id, "hero_reveal", rng, mention=winner_mention(chat_id, winner_id), **title_vars(chat_id))]
        if weights is not None and chance is not None:
            lines.append(tt(chat_id, "hero_chance", rng, pct=max(1, round(chance * 100))))
        extra = milestone_line(chat_id, winner_id)
        if extra:
            lines.append(extra)
        await bot.send_message(chat_id, "\n".join(lines))
        await settle_tie(bot, chat_id)
    return winner_id, is_new


PAIR_TITLE_KEY = "pair_title"
DUEL_TTL = 10 * 60


def pair_title(chat_id: int) -> str:
    return store.custom_text(chat_id, PAIR_TITLE_KEY) or t("pair_title_default")


def pair_vars(chat_id: int) -> dict:
    title = pair_title(chat_id)
    return {"title": html.escape(title), "title_lc": html.escape(title[:1].lower() + title[1:])}


async def roll_pair(bot: Bot, chat_id: int) -> tuple[tuple[int, int] | None, bool]:
    """Today's pair; a fresh one is announced in the group. (None, False) with fewer than 2 players."""
    day = today()
    existing = store.pair_for(chat_id, day.isoformat())
    if existing:
        return existing, False
    yesterday = store.pair_for(chat_id, (day - timedelta(days=1)).isoformat())
    pair = games.choose_pair(store.active_members(chat_id), yesterday, rng)
    if pair is None:
        return None, False
    if not store.save_pair(chat_id, day.isoformat(), *pair):
        return store.pair_for(chat_id, day.isoformat()), False
    pair = tuple(sorted(pair))  # same order as stored, so callers compare equal
    a, b = pair
    text = tt(chat_id, "pair_reveal", rng, a=winner_mention(chat_id, a), b=winner_mention(chat_id, b), **pair_vars(chat_id))
    await bot.send_message(chat_id, text)
    return pair, True


def who_answer(chat_id: int, question: str) -> str | None:
    members = store.active_members(chat_id)
    if not members:
        return None
    picked = rng.choice(members).user_id
    question = html.escape(question.strip().rstrip("?").strip()) or t("who_default")
    return tt(chat_id, "who_answer", rng, question=question, mention=winner_mention(chat_id, picked))


SUSPENSE = (2.5, 4.0)  # seconds between suspense lines; jittered so it does not feel mechanical


def suspense() -> float:
    return rng.uniform(*SUSPENSE)


def duel_call_text(chat_id: int, duel_id: int, challenger: int, target: int | None) -> str:
    """First line of a duel message; later events are appended below it, never replace it.
    The variant is seeded by the duel id, so rebuilding the line for every edit gives the same text."""
    pick = random.Random(duel_id)
    me = winner_mention(chat_id, challenger)
    if target:
        return tt(chat_id, "duel_call", pick, a=me, b=winner_mention(chat_id, target))
    return tt(chat_id, "duel_open", pick, a=me)


class PushError(Exception):
    """Why /push cannot happen: not_hero, used, self, unknown, off."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def push_title(bot: Bot, chat_id: int, hero_id: int, victim_id: int, reply_to: int | None = None) -> bool:
    """Anyone who got a point today (the daily pick or a bonus from a tie) tries once to push it onto
    someone, with activity-based odds shown up front. Success hands that point to the victim; failure
    gives the pusher a double. Returns success."""
    settings = store.settings(chat_id)
    if not (settings["push"] and settings["hero"]):
        raise PushError("off")
    day = today().isoformat()
    holds_pick = store.pick_for(chat_id, day) == hero_id
    if not holds_pick and not store.has_bonus(chat_id, day, hero_id):
        raise PushError("not_hero")
    if victim_id == hero_id:
        raise PushError("self")
    if store.member(chat_id, victim_id) is None:
        raise PushError("unknown")
    if store.push_for(chat_id, day, hero_id) is not None:
        raise PushError("used")
    weights = activity_weights(chat_id)
    chance = hero.push_success_chance(weights.get(hero_id, 1.0), weights.get(victim_id, 1.0))
    success = rng.random() < chance
    if not store.save_push(chat_id, day, hero_id, victim_id, victim_id if success else hero_id):
        raise PushError("used")
    if success and holds_pick:
        store.transfer_pick(chat_id, day, victim_id)
    elif success:
        store.move_bonus(chat_id, day, hero_id, victim_id)

    pct = round(chance * 100)
    story = [tt(chat_id, "push_call", rng, hero=winner_mention(chat_id, hero_id),
                victim=winner_mention(chat_id, victim_id), pct=pct, pct_victim=100 - pct, **title_vars(chat_id))]
    reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    msg = await bot.send_message(chat_id, story[0], reply_parameters=reply)
    # Lets the victim answer this very message with /push to throw it back (hot potato).
    store.remember(chat_id, msg.message_id, f"push:{hero_id}", time.time())
    a, b = quiet_name(chat_id, hero_id), quiet_name(chat_id, victim_id)
    for line in (tt(chat_id, "push_fight", rng, hero=a, victim=b),
                 tt(chat_id, "push_ok" if success else "push_fail", rng, hero=a, victim=b, **title_vars(chat_id))):
        await asyncio.sleep(suspense())
        story.append(line)
        try:
            await bot.edit_message_text("\n".join(story), chat_id=chat_id, message_id=msg.message_id)
        except TelegramAPIError:
            pass
    return success


def duel_outcome(challenger: int, opponent: int) -> tuple[int, int]:
    """(winner, loser), a fair coin."""
    return (challenger, opponent) if rng.random() < 0.5 else (opponent, challenger)


TIE_POLL_SECONDS = 15 * 60
POLL_MAX_OPTIONS = 10


async def settle_tie(bot: Bot, chat_id: int) -> bool:
    """Shared first place -> coin flips settle it once per distinct tie (knockout for 3+).
    Heads/tails give the champion +1; a coin on its edge gives everyone tied +1 and stops."""
    if not store.settings(chat_id)["tie_poll"]:
        return False
    best, tied = store.leaders(chat_id)
    if len(tied) < 2:
        return False
    key = f"{best}:" + ",".join(str(u) for u in sorted(tied))
    if store.tie_polled(chat_id, key):
        return False
    order = tied[:]
    rng.shuffle(order)
    rounds, champion = games.coin_tournament(order, rng)
    day = today().isoformat()
    reason = "tie:" + key[:60]
    names = ", ".join(quiet_name(chat_id, u) for u in order)
    story = [tt(chat_id, "coin_tie_start", rng, names=names, score=best, **title_vars(chat_id))]
    msg = await bot.send_message(chat_id, story[0])
    store.save_tie_poll(chat_id, key, msg.message_id, order, time.time())
    store.close_tie_poll(chat_id, key)
    side_names = {"heads": t("coin_heads"), "tails": t("coin_tails"), "edge": t("coin_edge")}
    for a, b, side, winner in rounds:
        await asyncio.sleep(suspense())
        if side == "edge":
            line = tt(chat_id, "coin_tie_edge", rng, a=quiet_name(chat_id, a), b=quiet_name(chat_id, b))
        else:
            line = tt(chat_id, "coin_tie_round", rng, a=quiet_name(chat_id, a), b=quiet_name(chat_id, b),
                      side=side_names[side], winner=quiet_name(chat_id, winner))
        story.append(line)
        try:
            await bot.edit_message_text("\n".join(story), chat_id=chat_id, message_id=msg.message_id)
        except TelegramAPIError:
            pass
    if champion is None:
        for uid in tied:
            store.add_bonus(chat_id, day, uid, reason)
        await bot.send_message(chat_id, tt(chat_id, "coin_tie_all", rng, names=names))
    else:
        store.add_bonus(chat_id, day, champion, reason)
        await asyncio.sleep(suspense())
        await bot.send_message(chat_id, tt(chat_id, "coin_tie_winner", rng, mention=winner_mention(chat_id, champion),
                                           **title_vars(chat_id)))
    return True


def poll_winner(options: list[int], counts: list[int]) -> tuple[int, bool]:
    """(winner user id, decided by votes). Options are in leader order, so a draw - or no votes -
    goes to the first one: whoever reached the top score first."""
    top = max(counts, default=0)
    if top == 0 or counts.count(top) > 1:
        return options[0], False
    return options[counts.index(top)], True


async def close_tie_poll(bot: Bot, chat_id: int, state_key: str, message_id: int, options: list[int]) -> None:
    try:
        poll = await bot.stop_poll(chat_id, message_id)
        counts = [o.voter_count for o in poll.options]
    except TelegramAPIError:
        counts = []  # poll deleted: nobody's votes to count
    store.close_tie_poll(chat_id, state_key)
    winner, by_votes = poll_winner(options, counts)
    # The winner gets +1 so the tie is really broken; the poll itself is removed to keep the chat clean.
    store.add_bonus(chat_id, today().isoformat(), winner, "tie")
    mention_ = winner_mention(chat_id, winner)
    text = "\n".join([
        tt(chat_id, "tie_winner" if by_votes else "tie_winner_draw", mention=mention_, **title_vars(chat_id)),
        tt(chat_id, "tie_bonus", rng, mention=quiet_name(chat_id, winner)),
    ])
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass  # no rights to delete or already gone: the result still goes out
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        log.warning("tie poll result for chat %s not delivered", chat_id)


STATUS_EVERY = 3.0       # seconds between progress edits (Telegram flood limits)
GROUP_ERROR_TTL = 60     # failed-download notices in groups clean themselves up
SWEEP_EVERY = 20
MB = 1024 * 1024


def progress_bar(done: int | None, total: int | None, width: int = 10) -> str:
    filled = round(width * min(done / total, 1.0)) if done and total else 0
    return t("bar_full") * filled + t("bar_empty") * (width - filled)


def render_status(
    phrase: str, stage: str, done: int | None = None, total: int | None = None, passed: tuple = ()
) -> str:
    """Stages already passed stay as lines above the current one; only the progress line changes."""
    lines = [html.escape(phrase)] + [t(f"dl_stage_{p}") for p in passed] + [t(f"dl_stage_{stage}")]
    if stage == "download" and done:
        if total:
            pct = min(100, done * 100 // total)
            lines.append(t("dl_progress", bar=progress_bar(done, total), pct=pct,
                           done=f"{done / MB:.1f}", total=f"{total / MB:.1f}"))
        else:
            lines.append(t("dl_progress_unknown", done=f"{done / MB:.1f}"))
    return "\n".join(lines)


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass  # already gone or older than 48h
    store.forget(chat_id, message_id)


async def _ticker(bot: Bot, chat_id: int, message_id: int, phrase: str, state: dict, shown: list) -> None:
    """Edits the status message from `state` (written by the worker thread) at most every STATUS_EVERY s."""
    while True:
        await asyncio.sleep(STATUS_EVERY)
        text = render_status(phrase, *state["now"], passed=tuple(state["passed"]))
        if text == shown[0]:
            continue
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            shown[0] = text
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError:
            pass  # "message is not modified" and friends are not worth failing a download for


async def deliver_video(
    bot: Bot, chat_id: int, url: str, reply_to: int | None = None, quiet_fail: bool = False
) -> bool:
    """Status message with live progress -> video reply -> status removed. Nothing is kept on disk.

    quiet_fail: on a plain "can't download" just drop the status (groups: unsupported posts are noise).
    """
    reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    phrase = tt(chat_id, "dl_phrases", rng)
    first = render_status(phrase, "queue")
    status = await bot.send_message(chat_id, first, reply_parameters=reply)
    store.remember(chat_id, status.message_id, "status", time.time())
    state = {"now": ("queue", None, None), "passed": []}
    shown = [first]

    def on_progress(stage: str, done: int | None, total: int | None) -> None:
        if stage != state["now"][0]:
            state["passed"].append(state["now"][0])
        state["now"] = (stage, done, total)

    workdir = Path(tempfile.mkdtemp(prefix="chatfun-"))
    ticker = asyncio.create_task(_ticker(bot, chat_id, status.message_id, phrase, state, shown))
    try:
        async with dl_slots:
            on_progress("probe", None, None)
            try:
                video = await asyncio.to_thread(
                    downloader.download, url, workdir, cfg.max_upload_bytes, cfg.max_duration,
                    cfg.cookies_file, on_progress,
                )
            except downloader.DownloadError as e:
                ticker.cancel()
                log.info("download %s failed: %s", url, e.reason)
                if quiet_fail and e.reason == "failed":
                    await _safe_delete(bot, chat_id, status.message_id)
                    return False
                text = {
                    "too_long": t("dl_too_long", limit=cfg.max_duration // 60),
                    "too_big": t("dl_too_big", limit=cfg.max_upload_bytes // MB),
                    "auth": tt(chat_id, "dl_auth"),
                }.get(e.reason, tt(chat_id, "dl_failed"))
                await bot.edit_message_text(text, chat_id=chat_id, message_id=status.message_id)
                if chat_id < 0:
                    store.schedule_delete(chat_id, status.message_id, "error", time.time() + GROUP_ERROR_TTL)
                else:
                    store.forget(chat_id, status.message_id)
                return False
            ticker.cancel()
            try:
                await bot.edit_message_text(
                    render_status(phrase, "upload", passed=(*state["passed"], state["now"][0])),
                    chat_id=chat_id, message_id=status.message_id,
                )
            except TelegramAPIError:
                pass
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            sent = await bot.send_video(
                chat_id,
                FSInputFile(video.path),
                caption=t("dl_caption", url=html.escape(url, quote=True), title=html.escape(video.title)),
                width=video.width,
                height=video.height,
                duration=video.duration,
                supports_streaming=True,
                reply_parameters=reply,
            )
            store.remember(chat_id, sent.message_id, "video", time.time())
            await _safe_delete(bot, chat_id, status.message_id)
            return True
    finally:
        ticker.cancel()
        shutil.rmtree(workdir, ignore_errors=True)


CLEANUP_HOURS_KEY = "cleanup_hours"
CLEANUP_CHOICES = (0, 1, 6, 24, 47)  # Telegram lets bots delete messages only within 48 hours
COMMAND_TTL = 30                     # seconds before a /command to the bot is removed (when enabled)


def cleanup_hours(chat_id: int) -> int:
    value = store.custom_text(chat_id, CLEANUP_HOURS_KEY)
    return value if value in CLEANUP_CHOICES else 0


def set_cleanup_hours(chat_id: int, hours: int) -> None:
    store.set_custom_text(chat_id, CLEANUP_HOURS_KEY, hours if hours in CLEANUP_CHOICES and hours else None)


async def bot_can_delete(bot: Bot, chat_id: int) -> bool:
    """Both cleanups need the bot to be a group admin with the "delete messages" right."""
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError:
        return False
    return member.status == ChatMemberStatus.CREATOR or bool(getattr(member, "can_delete_messages", False))


class RecordSent(BaseRequestMiddleware):
    """Remembers every message the bot sends to a group, so old ones can be cleaned up later.
    Edits are not new messages and are skipped."""

    async def __call__(self, make_request, bot, method):
        response = await make_request(bot, method)
        result = getattr(response, "result", None)
        sent = type(method).__name__.startswith(("Send", "Copy", "Forward"))
        if sent and isinstance(result, Message) and result.chat.id < 0:
            store.remember_new(result.chat.id, result.message_id, "out", time.time())
        return response


async def sweeper(bot: Bot) -> None:
    """Deletes scheduled bot messages; on start, statuses orphaned by a crash/restart go first."""
    store.orphan_statuses(time.time())
    while True:
        for chat_id, _title in store.all_chats():
            hours = cleanup_hours(chat_id)
            if hours:
                store.expire_older_than(chat_id, time.time() - hours * 3600, time.time())
        for chat_id, message_id in store.due_for_delete(time.time()):
            await _safe_delete(bot, chat_id, message_id)
        for chat_id, key, message_id, options in store.due_tie_polls(time.time()):
            await close_tie_poll(bot, chat_id, key, message_id, options)
        for duel_id, chat_id, message_id in store.stale_duels(time.time() - DUEL_TTL):
            duel = store.duel(duel_id)
            if store.finish_duel(duel_id, "expired") and message_id:
                text = duel_call_text(chat_id, duel_id, duel["challenger"], duel["target"]) + "\n" + tt(chat_id, "duel_expired")
                try:
                    await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
                except TelegramAPIError:
                    pass
        store.prune_messages(time.time())
        await asyncio.sleep(SWEEP_EVERY)
