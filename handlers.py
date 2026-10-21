import asyncio
import html
import logging
import time

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

import downloader
import games
from core import (
    TITLE_MAX,
    PushError,
    cfg,
    chat_param,
    chat_tone,
    deliver_video,
    display_name,
    duel_call_text,
    duel_outcome,
    is_chat_admin,
    normalize_title,
    pair_vars,
    parse_players,
    push_title,
    quiet_name,
    rng,
    roll_hero,
    roll_pair,
    set_tone,
    store,
    suspense,
    title_vars,
    today,
    tt,
    who_answer,
)
from messages import TONES, raw, t

log = logging.getLogger("chatfun")
router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}


SERVICE_TTL = 60          # "already chosen", "admins only" and the like vanish after a minute
COMMAND_GAP = 3.0         # min seconds between two commands of one person in a group
COMMAND_BURST = 5         # max commands per person per minute in a group
DOWNLOADS_PER_USER = 2    # parallel downloads per person; extra links are ignored
_command_times: dict[tuple[int, int], list[float]] = {}
_active_downloads: dict[tuple[int, int], int] = {}
_scolded: dict[tuple[int, int], float] = {}
SCOLD_TONES = (1, 2)      # toxic and cheeky tones snap back at flooders; the rest stay silent
SCOLD_EVERY = 60          # at most one snap per person per minute, so the bot does not flood itself


def command_allowed(chat_id: int, user_id: int, now: float) -> bool:
    """Sliding-window limit for one person's commands in one chat."""
    key = (chat_id, user_id)
    recent = [ts for ts in _command_times.get(key, []) if now - ts < 60]
    if (recent and now - recent[-1] < COMMAND_GAP) or len(recent) >= COMMAND_BURST:
        _command_times[key] = recent
        return False
    recent.append(now)
    _command_times[key] = recent
    return True


class AntiFlood(BaseMiddleware):
    """Drops a person's commands that come too fast in a group; the spam itself is deleted if the bot may."""

    async def __call__(self, handler, event: Message, data):
        text = event.text or event.caption or ""
        user = event.from_user
        if event.chat.type in GROUPS and text.startswith("/") and user and not user.is_bot:
            now = time.time()
            if not command_allowed(event.chat.id, user.id, now):
                log.info("antiflood: dropped command in chat %s from %s", event.chat.id, user.id)
                store.remember(event.chat.id, event.message_id, "spam", now, delete_at=now)
                await scold_flooder(event, now)
                return None
        return await handler(event, data)


async def scold_flooder(message: Message, now: float) -> None:
    chat_id, user_id = message.chat.id, message.from_user.id
    if chat_tone(chat_id) not in SCOLD_TONES or now - _scolded.get((chat_id, user_id), -SCOLD_EVERY) < SCOLD_EVERY:
        return
    _scolded[(chat_id, user_id)] = now
    try:
        await service_reply(message, tt(chat_id, "flood_scold", rng, name=quiet_name(chat_id, user_id)))
    except TelegramAPIError:
        pass


async def service_reply(message: Message, text: str) -> None:
    """Reply that only matters for a moment: in groups it and the command behind it are removed later."""
    sent = await message.reply(text)
    if message.chat.type in GROUPS:
        at = time.time() + SERVICE_TTL
        store.remember(message.chat.id, sent.message_id, "service", time.time(), delete_at=at)
        store.remember(message.chat.id, message.message_id, "service_cmd", time.time(), delete_at=at)


class ChatGate(BaseMiddleware):
    """Drops updates from chats outside ALLOWED_CHATS and records group members as they speak."""

    async def __call__(self, handler, event: Message, data):
        chat = event.chat
        kinds = [e.type for e in (event.entities or event.caption_entities or [])]
        log.debug("incoming chat=%s type=%s content=%s entities=%s", chat.id, chat.type, event.content_type, kinds)
        if cfg.allowed_chats and chat.type != ChatType.PRIVATE and chat.id not in cfg.allowed_chats:
            return None
        user = event.from_user
        if chat.type in GROUPS:
            store.upsert_chat(chat.id, chat.title or str(chat.id))
            if user and not user.is_bot:
                settings = store.settings(chat.id)
                store.touch(
                    chat.id, user.id, display_name(user), register=settings["auto_register"], username=user.username
                )
                if settings["stats"]:
                    store.count_message(chat.id, user.id, today().isoformat())
        return await handler(event, data)


def app_keyboard(bot_username: str, chat_id: int | None) -> InlineKeyboardMarkup | None:
    if not cfg.webapp_url:
        return None
    if chat_id is None:
        button = InlineKeyboardButton(text=t("app_button"), web_app=WebAppInfo(url=cfg.webapp_url))
    elif cfg.app_shortname:
        url = f"https://t.me/{bot_username}/{cfg.app_shortname}?startapp={chat_param(chat_id)}"
        button = InlineKeyboardButton(text=t("app_button"), url=url)
    else:
        # web_app buttons are private-chat only; bounce through the bot's private chat.
        button = InlineKeyboardButton(text=t("app_button"), url=f"https://t.me/{bot_username}?start=app")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


@router.my_chat_member()
async def on_added(update: ChatMemberUpdated) -> None:
    if update.chat.type in GROUPS:
        store.upsert_chat(update.chat.id, update.chat.title or str(update.chat.id))


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message, bot: Bot, command: CommandObject | None = None) -> None:
    me = await bot.me()
    in_private = message.chat.type == ChatType.PRIVATE
    title = t("hero_title_default") if in_private else title_vars(message.chat.id)["title"]
    await message.reply(
        t("help", title=title), reply_markup=app_keyboard(me.username, None if in_private else message.chat.id)
    )


@router.message(Command("app"))
async def cmd_app(message: Message, bot: Bot) -> None:
    me = await bot.me()
    in_private = message.chat.type == ChatType.PRIVATE
    markup = app_keyboard(me.username, None if in_private else message.chat.id)
    await message.reply(t("app_text") if markup else t("app_disabled"), reply_markup=markup)


@router.message(Command("reg"), F.chat.type.in_(GROUPS))
async def cmd_reg(message: Message) -> None:
    name = display_name(message.from_user)
    changed = store.set_active(message.chat.id, message.from_user.id, name, True)
    await service_reply(message, tt(message.chat.id, "reg_ok" if changed else "reg_already", name=html.escape(name)))


@router.message(Command("unreg"), F.chat.type.in_(GROUPS))
async def cmd_unreg(message: Message) -> None:
    name = display_name(message.from_user)
    changed = store.set_active(message.chat.id, message.from_user.id, name, False)
    await service_reply(message, tt(message.chat.id, "unreg_ok" if changed else "unreg_absent", name=html.escape(name)))


@router.message(Command("hero"))
async def cmd_hero(message: Message, bot: Bot) -> None:
    if message.chat.type not in GROUPS:
        await service_reply(message, t("hero_private"))
        return
    if not store.settings(message.chat.id)["hero"]:
        await service_reply(message, t("hero_off", **title_vars(message.chat.id)))
        return
    winner_id, is_new = await roll_hero(bot, message.chat.id)
    if winner_id is None:
        await service_reply(message, tt(message.chat.id, "hero_empty"))
    elif not is_new:
        mention = quiet_name(message.chat.id, winner_id)
        await service_reply(message, tt(message.chat.id, "hero_already", mention=mention, **title_vars(message.chat.id)))


@router.message(Command("push"))
async def cmd_push(message: Message, bot: Bot) -> None:
    """Today's hero replies /push to someone: one shot per day to hand the title over, odds by activity."""
    if not await feature_on(message, "push"):
        return
    chat_id = message.chat.id
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if target is None or target.is_bot:
        await service_reply(message, tt(chat_id, "push_no_reply"))
        return
    store.touch(chat_id, target.id, display_name(target), register=False, username=target.username)
    try:
        await push_title(bot, chat_id, message.from_user.id, target.id, reply_to=message.message_id)
    except PushError as e:
        await service_reply(message, tt(chat_id, f"push_{e.code}") if e.code != "off" else tt(chat_id, "feature_off"))


@router.message(Command("title"), F.chat.type.in_(GROUPS))
async def cmd_title(message: Message, bot: Bot, command: CommandObject) -> None:
    chat_id = message.chat.id
    if not command.args:
        await service_reply(message, t("title_usage", limit=TITLE_MAX, **title_vars(chat_id)))
        return
    if not await is_chat_admin(bot, chat_id, message.from_user.id):
        await service_reply(message, t("title_admin_only"))
        return
    store.set_hero_title(chat_id, normalize_title(command.args))
    await message.reply(t("title_set", **title_vars(chat_id)))


@router.message(Command("tone"), F.chat.type.in_(GROUPS))
async def cmd_tone(message: Message, bot: Bot, command: CommandObject) -> None:
    chat_id = message.chat.id
    names = raw("tone_names")
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) not in TONES:
        listing = "\n".join(f"{n} - {names[n]}" for n in TONES)
        await service_reply(message, t("tone_usage", current=names[chat_tone(chat_id)], list=listing))
        return
    if not await is_chat_admin(bot, chat_id, message.from_user.id):
        await service_reply(message, t("tone_admin_only"))
        return
    set_tone(chat_id, int(arg))
    await message.reply(t("tone_set", name=names[int(arg)]))


@router.message(Command("add"), F.chat.type.in_(GROUPS))
async def cmd_add(message: Message, bot: Bot, command: CommandObject) -> None:
    chat_id = message.chat.id
    if not await is_chat_admin(bot, chat_id, message.from_user.id):
        await service_reply(message, t("add_admin_only"))
        return
    usernames, names = parse_players(command.args or "")
    if not usernames and not names:
        await service_reply(message, t("add_usage"))
        return
    added, skipped = store.add_manual(chat_id, usernames, names)
    await message.reply(t("add_done", added=len(added), skipped=len(skipped)))


@router.message(Command("top"), F.chat.type.in_(GROUPS))
async def cmd_top(message: Message) -> None:
    rows = store.top(message.chat.id)
    if not rows:
        await service_reply(message, t("top_empty"))
        return
    lines = [t("top_title", **title_vars(message.chat.id))] + [
        t("top_row", place=i, name=html.escape(name), count=n) for i, (_, name, n) in enumerate(rows, 1)
    ]
    await message.reply("\n".join(lines))


def entity_links(message: Message) -> list[str]:
    """Links Telegram marked up: hyperlinked text (text_link) and bare links like
    "youtu.be/x" pasted without a scheme (url entity), which the plain-text regex misses."""
    links = []
    for text, entities in ((message.text, message.entities), (message.caption, message.caption_entities)):
        for e in entities or []:
            if e.type == "text_link" and e.url:
                links.append(e.url)
            elif e.type == "url" and text:
                url = e.extract_from(text)
                links.append(url if "://" in url else f"https://{url}")
    return links


async def feature_on(message: Message, key: str) -> bool:
    """Group-only feature gate; tells the user when an admin switched it off."""
    if message.chat.type not in GROUPS:
        await service_reply(message, t("group_only"))
        return False
    if not store.settings(message.chat.id)[key]:
        await service_reply(message, tt(message.chat.id, "feature_off"))
        return False
    return True


# --- stats -------------------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject) -> None:
    if not await feature_on(message, "stats"):
        return
    period = games.parse_period(command.args)
    rows, total = store.activity_top(message.chat.id, games.period_since(period, today()))
    if not rows:
        await service_reply(message, t("stats_empty"))
        return
    lines = [t("stats_title", period=t(f"period_{period}"), total=total)] + [
        t("top_row", place=i, name=html.escape(name), count=n) for i, (_, name, n) in enumerate(rows, 1)
    ]
    await message.reply("\n".join(lines))


# --- random ------------------------------------------------------------------

@router.message(Command("who"))
async def cmd_who(message: Message, command: CommandObject) -> None:
    if not await feature_on(message, "random"):
        return
    answer = who_answer(message.chat.id, command.args or "")
    await message.reply(answer or tt(message.chat.id, "hero_empty"))


@router.message(Command("ball"))
async def cmd_ball(message: Message) -> None:
    if message.chat.type in GROUPS and not store.settings(message.chat.id)["random"]:
        return
    await message.reply(tt(message.chat.id, "ball_answers", rng))


@router.message(Command("coin"))
async def cmd_coin(message: Message) -> None:
    if message.chat.type in GROUPS and not store.settings(message.chat.id)["random"]:
        return
    await message.reply(tt(message.chat.id, "coin_result", side=t("coin_heads") if rng.random() < 0.5 else t("coin_tails")))


@router.message(Command("roll"))
async def cmd_roll(message: Message, command: CommandObject) -> None:
    if message.chat.type in GROUPS and not store.settings(message.chat.id)["random"]:
        return
    top = games.parse_roll(command.args)
    await message.reply(tt(message.chat.id, "roll_result", value=rng.randint(1, top), top=top))


# --- pair of the day ---------------------------------------------------------

@router.message(Command("pair"))
async def cmd_pair(message: Message, bot: Bot) -> None:
    if not await feature_on(message, "pair"):
        return
    pair, is_new = await roll_pair(bot, message.chat.id)
    if pair is None:
        await service_reply(message, tt(message.chat.id, "pair_empty"))
    elif not is_new:
        a, b = pair
        await service_reply(message, tt(message.chat.id, "pair_already", a=quiet_name(message.chat.id, a),
                              b=quiet_name(message.chat.id, b), **pair_vars(message.chat.id)))


@router.message(Command("pairs"))
async def cmd_pairs(message: Message) -> None:
    if not await feature_on(message, "pair"):
        return
    chat_id = message.chat.id
    rows = store.pair_top(chat_id)
    if not rows:
        await service_reply(message, t("top_empty"))
        return
    names = {m.user_id: m.name for m in store.all_members(chat_id)}
    lines = [t("pairs_title", **pair_vars(chat_id))] + [
        t("pair_row", place=i, a=html.escape(names.get(a, str(a))), b=html.escape(names.get(b, str(b))), count=n)
        for i, (a, b, n) in enumerate(rows, 1)
    ]
    await message.reply("\n".join(lines))


# --- duels -------------------------------------------------------------------

def duel_keyboard(duel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("duel_accept"), callback_data=f"duel:{duel_id}:accept"),
        InlineKeyboardButton(text=t("duel_decline"), callback_data=f"duel:{duel_id}:decline"),
    ]])


@router.message(Command("duel"))
async def cmd_duel(message: Message, bot: Bot) -> None:
    if not await feature_on(message, "duels"):
        return
    chat_id, challenger = message.chat.id, message.from_user.id
    target_user = message.reply_to_message.from_user if message.reply_to_message else None
    if target_user and target_user.is_bot:
        await service_reply(message, t("duel_bot"))
        return
    if target_user and target_user.id == challenger:
        # Challenging yourself is an instant loss (winner 0 = nobody), counted in /duels.
        store.finish_duel(store.create_duel(chat_id, challenger, challenger, time.time()), "done", winner=0)
        await message.reply(tt(chat_id, "duel_self_shot", rng, a=quiet_name(chat_id, challenger)))
        return
    if store.pending_duel(chat_id, challenger):
        await service_reply(message, t("duel_pending"))
        return
    target = target_user.id if target_user else None
    if target_user:
        store.touch(chat_id, target, display_name(target_user), register=False, username=target_user.username)
    duel_id = store.create_duel(chat_id, challenger, target, time.time())
    sent = await message.reply(duel_call_text(chat_id, duel_id, challenger, target), reply_markup=duel_keyboard(duel_id))
    store.set_duel_message(duel_id, sent.message_id)


@router.callback_query(F.data.startswith("duel:"))
async def on_duel_button(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, raw_id, action = callback.data.split(":")
        duel = store.duel(int(raw_id))
    except ValueError:
        duel = None
    if not duel or duel["status"] != "pending":
        await callback.answer(t("duel_over"))
        return
    chat_id, presser = duel["chat_id"], callback.from_user.id
    challenger, target = duel["challenger"], duel["target"]
    store.touch(chat_id, presser, display_name(callback.from_user), register=False,
                username=callback.from_user.username)
    # Every step is appended under the challenge as a new line: the whole story stays readable.
    story = [duel_call_text(chat_id, duel["id"], challenger, target)]

    if action == "decline":
        if presser == challenger:
            status, text = "cancelled", tt(chat_id, "duel_cancelled", a=quiet_name(chat_id, challenger))
        elif target is not None and presser == target:
            status, text = "declined", tt(chat_id, "duel_declined", b=quiet_name(chat_id, target))
        else:
            await callback.answer(t("duel_not_you"))
            return
        if store.finish_duel(duel["id"], status):
            story.append(text)
            await callback.message.edit_text("\n".join(story))
        await callback.answer()
        return

    if presser == challenger:
        await callback.answer(t("duel_self"))
        return
    if target is not None and presser != target:
        await callback.answer(t("duel_not_you"))
        return
    winner, loser = duel_outcome(challenger, presser)
    if not store.finish_duel(duel["id"], "done", opponent=presser, winner=winner):
        await callback.answer(t("duel_over"))
        return
    await callback.answer()
    # The challenge already pinged both; the fight and result are follow-ups in the same thread.
    a, b = quiet_name(chat_id, challenger), quiet_name(chat_id, presser)
    try:
        story.append(tt(chat_id, "duel_fight", rng, a=a, b=b))
        await callback.message.edit_text("\n".join(story))
        await asyncio.sleep(suspense())
        story.append(tt(chat_id, "duel_result", rng, winner=quiet_name(chat_id, winner),
                        loser=quiet_name(chat_id, loser)))
        await callback.message.edit_text("\n".join(story))
    except TelegramAPIError:
        log.warning("duel %s result not shown", duel["id"])


@router.message(Command("duels"))
async def cmd_duels(message: Message) -> None:
    if not await feature_on(message, "duels"):
        return
    rows = store.duel_stats(message.chat.id)
    if not rows:
        await service_reply(message, t("duels_empty"))
        return
    lines = [t("duels_title")] + [
        t("duel_row", place=i, name=html.escape(name), wins=w, losses=lo) for i, (_, name, w, lo) in enumerate(rows, 1)
    ]
    await message.reply("\n".join(lines))


CHATTER_COOLDOWN = 5.0  # seconds per chat, so two people baiting the bot cannot make it flood
_last_chatter: dict[int, float] = {}


async def addressed_to_bot(message: Message, bot: Bot) -> bool:
    me = await bot.me()
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == me.id:
        return True
    return f"@{me.username}".lower() in (message.text or message.caption or "").lower()


async def maybe_chatter(message: Message, bot: Bot) -> None:
    """Someone replied to the bot or mentioned it: answer in the chat's tone."""
    text = message.text or message.caption or ""
    if not text or text.startswith("/") or message.from_user is None or message.from_user.is_bot:
        return
    chat_id = message.chat.id
    if message.chat.type in GROUPS and not store.settings(chat_id)["chatter"]:
        return
    if not await addressed_to_bot(message, bot):
        return
    now = time.monotonic()
    if now - _last_chatter.get(chat_id, -CHATTER_COOLDOWN) < CHATTER_COOLDOWN:
        return
    _last_chatter[chat_id] = now
    kind = games.classify_chatter(text, raw("chatter_keywords"))
    key = "ball_answers" if kind == "question" else f"chatter_{kind}"
    await message.reply(tt(chat_id, key, rng, name=quiet_name(chat_id, message.from_user.id)))


@router.message(F.text | F.caption)
async def on_links(message: Message, bot: Bot) -> None:
    text = message.text or message.caption
    every = downloader.all_urls(text, entity_links(message))
    if not every:
        await maybe_chatter(message, bot)
        return
    urls = [u for u in every if downloader.is_supported(u)]
    skipped = sorted({downloader.host_of(u) for u in every if u not in urls})
    log.info("chat %s: %d video link(s), skipped hosts %s", message.chat.id, len(urls), skipped or "-")
    if not urls:
        return
    if message.chat.type in GROUPS and not store.settings(message.chat.id)["downloads"]:
        return
    in_group = message.chat.type in GROUPS
    key = (message.chat.id, message.from_user.id if message.from_user else 0)
    for url in urls:
        if _active_downloads.get(key, 0) >= DOWNLOADS_PER_USER:
            log.info("antiflood: skipped link in chat %s, too many downloads from one person", message.chat.id)
            break
        _active_downloads[key] = _active_downloads.get(key, 0) + 1
        try:
            await deliver_video(bot, message.chat.id, url, reply_to=message.message_id, quiet_fail=in_group)
        except Exception:
            log.exception("sending %s failed", url)
        finally:
            _active_downloads[key] -= 1
