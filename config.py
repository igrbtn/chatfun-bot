import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ID_RE = re.compile(r"-?\d+")


class ConfigError(SystemExit):
    """Bad .env value: exits with a readable message instead of a Python traceback."""


def _clean(raw: str | None) -> str:
    """Tolerate what people type into .env: inline '# comments' and surrounding quotes."""
    value = (raw or "").split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _ids(name: str) -> frozenset[int]:
    """Comma, semicolon or space separated Telegram ids (group ids are negative, e.g. -1001234567890)."""
    ids = set()
    for part in re.split(r"[,;\s]+", _clean(os.getenv(name))):
        part = part.strip("\"'")
        if not part:
            continue
        if not ID_RE.fullmatch(part):
            hint = " (that is a username; get the numeric id from @userinfobot)" if part.startswith("@") else ""
            raise ConfigError(f"config error: {name}: {part!r} is not a numeric Telegram id{hint}")
        ids.add(int(part))
    return frozenset(ids)


def _int(name: str, default: int) -> int:
    value = _clean(os.getenv(name)) or str(default)
    if not ID_RE.fullmatch(value):
        raise ConfigError(f"config error: {name}: {value!r} is not a whole number")
    return int(value)


def _tz(name: str, default: str) -> ZoneInfo:
    value = _clean(os.getenv(name)) or default
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"config error: {name}: {value!r} is not a time zone (e.g. Europe/Moscow)") from None


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    allowed_chats: frozenset[int]
    data_dir: Path
    tz: ZoneInfo
    max_upload_bytes: int
    max_duration: int
    cookies_file: Path | None
    web_port: int
    webapp_url: str
    app_shortname: str


def load() -> Config:
    token = _clean(os.getenv("BOT_TOKEN"))
    if not token:
        raise ConfigError("config error: BOT_TOKEN is empty (get it from @BotFather)")
    data_dir = Path(_clean(os.getenv("DATA_DIR")) or "./data")
    cookies = _clean(os.getenv("COOKIES_FILE"))
    return Config(
        bot_token=token,
        admin_ids=_ids("ADMIN_IDS"),
        allowed_chats=_ids("ALLOWED_CHATS"),
        data_dir=data_dir,
        tz=_tz("TZ", "UTC"),
        max_upload_bytes=_int("MAX_UPLOAD_MB", 49) * 1024 * 1024,
        max_duration=_int("MAX_DURATION_SEC", 900),
        cookies_file=(data_dir / cookies) if cookies else None,
        web_port=_int("WEB_PORT", 8095),
        webapp_url=_clean(os.getenv("WEBAPP_URL")).rstrip("/"),
        app_shortname=_clean(os.getenv("APP_SHORTNAME")),
    )
