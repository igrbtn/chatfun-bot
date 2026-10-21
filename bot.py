import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from aiohttp import web

import handlers
import webapp
from core import cfg, sweeper
from messages import raw, t

log = logging.getLogger("chatfun")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # LOG_LEVEL=DEBUG traces every incoming message (chat id and type only) without aiogram's noise.
    logging.getLogger("chatfun").setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # Leftovers from a crash mid-download: nothing is meant to persist between runs.
    for stale in Path(tempfile.gettempdir()).glob("chatfun-*"):
        shutil.rmtree(stale, ignore_errors=True)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    handlers.router.message.outer_middleware(handlers.ChatGate())
    handlers.router.message.outer_middleware(handlers.AntiFlood())
    dp.include_router(handlers.router)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in raw("commands").items()])
    if cfg.webapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text=t("app_button"), web_app=WebAppInfo(url=cfg.webapp_url))
        )

    me = await bot.get_me()
    if not me.can_read_all_group_messages:
        # Privacy mode: in groups the bot only sees commands, so links are silently missed.
        log.warning("privacy mode is on: links in groups are invisible unless the bot is a group admin")
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, t("privacy_warning", username=me.username))
            except TelegramAPIError:
                pass

    runner = web.AppRunner(webapp.build_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.web_port).start()
    log.info("mini app api on :%d", cfg.web_port)
    sweep = asyncio.create_task(sweeper(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        sweep.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
