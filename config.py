import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


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
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    cookies = os.getenv("COOKIES_FILE", "").strip()
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_ids=_ids(os.getenv("ADMIN_IDS", "")),
        allowed_chats=_ids(os.getenv("ALLOWED_CHATS", "")),
        data_dir=data_dir,
        tz=ZoneInfo(os.getenv("TZ", "UTC")),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_MB", "49")) * 1024 * 1024,
        max_duration=int(os.getenv("MAX_DURATION_SEC", "900")),
        cookies_file=(data_dir / cookies) if cookies else None,
        web_port=int(os.getenv("WEB_PORT", "8095")),
        webapp_url=os.getenv("WEBAPP_URL", "").rstrip("/"),
        app_shortname=os.getenv("APP_SHORTNAME", ""),
    )
