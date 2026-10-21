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
from aiogram.enums import ChatAction, ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import FSInputFile, ReplyParameters, User

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


async def roll_hero(bot: Bot, chat_id: int) -> tuple[int | None, bool]:
    """Pick today's hero; a fresh pick is announced in the group."""
    winner_id, is_new = hero.pick_today(store, chat_id, today(), rng)
    if winner_id is not None and is_new:
        if store.settings(chat_id)["drumroll"]:
            for line in rng.choice(raw("hero_drumroll", chat_tone(chat_id))):
                await bot.send_message(chat_id, line)
                await asyncio.sleep(1.5)
        reveal = tt(chat_id, "hero_reveal", rng, mention=winner_mention(chat_id, winner_id), **title_vars(chat_id))
        await bot.send_message(chat_id, reveal)
        await maybe_tie_poll(bot, chat_id)
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


def duel_outcome(challenger: int, opponent: int) -> tuple[int, int]:
    """(winner, loser), a fair coin."""
    return (challenger, opponent) if rng.random() < 0.5 else (opponent, challenger)


TIE_POLL_SECONDS = 15 * 60
POLL_MAX_OPTIONS = 10


async def maybe_tie_poll(bot: Bot, chat_id: int) -> bool:
    """Shared first place -> one 15-minute poll per distinct tie. Closed and announced by sweeper()."""
    if not store.settings(chat_id)["tie_poll"]:
        return False
    best, tied = store.leaders(chat_id)
    tied = tied[:POLL_MAX_OPTIONS]
    if len(tied) < 2:
        return False
    key = f"{best}:" + ",".join(str(u) for u in sorted(tied))
    if store.tie_polled(chat_id, key):
        return False
    labels = []
    for uid in tied:
        member = store.member(chat_id, uid)
        label = (f"@{member.username}" if member and member.username else (member.name if member else str(uid)))[:100]
        labels.append(label if label not in labels else f"{label} ({len(labels) + 1})")
    msg = await bot.send_poll(
        chat_id,
        question=tt(chat_id, "tie_poll_question", title=game_title(chat_id), score=best)[:300],
        options=labels,
        is_anonymous=False,
    )
    store.save_tie_poll(chat_id, key, msg.message_id, tied, time.time() + TIE_POLL_SECONDS)
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
    text = tt(chat_id, "tie_winner" if by_votes else "tie_winner_draw", mention=winner_mention(chat_id, winner),
             **title_vars(chat_id))
    try:
        await bot.send_message(
            chat_id, text, reply_parameters=ReplyParameters(message_id=message_id, allow_sending_without_reply=True)
        )
    except TelegramAPIError:
        log.warning("tie poll result for chat %s not delivered", chat_id)


STATUS_EVERY = 3.0       # seconds between progress edits (Telegram flood limits)
GROUP_ERROR_TTL = 60     # failed-download notices in groups clean themselves up
SWEEP_EVERY = 20
MB = 1024 * 1024


def progress_bar(done: int | None, total: int | None, width: int = 10) -> str:
    filled = round(width * min(done / total, 1.0)) if done and total else 0
    return t("bar_full") * filled + t("bar_empty") * (width - filled)


def render_status(phrase: str, stage: str, done: int | None = None, total: int | None = None) -> str:
    lines = [html.escape(phrase), t(f"dl_stage_{stage}")]
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
        text = render_status(phrase, *state["now"])
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
    state = {"now": ("queue", None, None)}
    shown = [first]

    def on_progress(stage: str, done: int | None, total: int | None) -> None:
        state["now"] = (stage, done, total)

    workdir = Path(tempfile.mkdtemp(prefix="chatfun-"))
    ticker = asyncio.create_task(_ticker(bot, chat_id, status.message_id, phrase, state, shown))
    try:
        async with dl_slots:
            state["now"] = ("probe", None, None)
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
                    render_status(phrase, "upload"), chat_id=chat_id, message_id=status.message_id
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


async def sweeper(bot: Bot) -> None:
    """Deletes scheduled bot messages; on start, statuses orphaned by a crash/restart go first."""
    store.orphan_statuses(time.time())
    while True:
        for chat_id, message_id in store.due_for_delete(time.time()):
            await _safe_delete(bot, chat_id, message_id)
        for chat_id, key, message_id, options in store.due_tie_polls(time.time()):
            await close_tie_poll(bot, chat_id, key, message_id, options)
        for duel_id, chat_id, message_id in store.stale_duels(time.time() - DUEL_TTL):
            if store.finish_duel(duel_id, "expired") and message_id:
                try:
                    await bot.edit_message_text(tt(chat_id, "duel_expired"), chat_id=chat_id, message_id=message_id)
                except TelegramAPIError:
                    pass
        store.prune_messages(time.time())
        await asyncio.sleep(SWEEP_EVERY)
