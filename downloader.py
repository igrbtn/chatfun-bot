import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

SUPPORTED_HOSTS = (
    "youtube.com", "youtu.be",
    "instagram.com",
    "facebook.com", "fb.watch", "fb.com",
    "tiktok.com",
    "twitter.com", "x.com",
    "vk.com", "vkvideo.ru",
    "reddit.com", "redd.it",
    "pinterest.com", "pin.it",
    "threads.net", "threads.com",
)

# Height tiers: try the best first, fall back while the file is above the upload limit.
HEIGHT_TIERS = (720, 480, 360)


class DownloadError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "too_long" | "too_big" | "auth" | "failed"


AUTH_MARKERS = ("confirm you", "sign in", "log in", "login required", "cookies")


def _failure(e: Exception) -> DownloadError:
    """Platform wants a logged-in session (cookies) vs. anything else."""
    text = str(e).lower()
    return DownloadError("auth" if any(m in text for m in AUTH_MARKERS) else "failed")


@dataclass(frozen=True)
class Video:
    path: Path
    title: str
    width: int | None
    height: int | None
    duration: int | None


def _host(url: str) -> str:
    host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    return host.removeprefix("www.").removeprefix("m.")


def is_supported(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS)


def all_urls(text: str | None, extra: list[str] | None = None) -> list[str]:
    """Every http(s) link: plain ones from the text plus hidden ones (text_link entities), deduplicated."""
    found = [m.group(0).rstrip(".,;:!?)]}") for m in URL_RE.finditer(text or "")] + list(extra or [])
    return list(dict.fromkeys(u for u in found if URL_RE.fullmatch(u)))


def extract_urls(text: str | None, extra: list[str] | None = None) -> list[str]:
    """Supported video links from a message, deduplicated, in order."""
    return [u for u in all_urls(text, extra) if is_supported(u)]


def host_of(url: str) -> str:
    return _host(url)


def _format(height: int) -> str:
    return (
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/b[height<={height}][ext=mp4]"
        f"/bv*[height<={height}]+ba/b[height<={height}]/b"
    )


def _base_opts(cookies: Path | None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 2,
    }
    if cookies and cookies.is_file():
        opts["cookiefile"] = str(cookies)
    return opts


def probe(path: Path) -> tuple[int | None, int | None]:
    """Real width/height of the downloaded file (info dict describes the source, not our tier)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        stream = json.loads(out)["streams"][0]
        return stream.get("width"), stream.get("height")
    except (OSError, subprocess.SubprocessError, KeyError, IndexError, ValueError):
        return None, None


# progress(stage, done_bytes, total_bytes); stages: "probe", "download", "merge". Called from the worker thread.
Progress = Callable[[str, int | None, int | None], None]


def _hooks(progress: Progress) -> dict:
    def on_download(d: dict) -> None:
        if d.get("status") == "downloading":
            progress("download", d.get("downloaded_bytes"), d.get("total_bytes") or d.get("total_bytes_estimate"))

    def on_post(d: dict) -> None:
        if d.get("status") == "started":
            progress("merge", None, None)

    return {"progress_hooks": [on_download], "postprocessor_hooks": [on_post]}


def download(
    url: str,
    workdir: Path,
    max_bytes: int,
    max_duration: int,
    cookies: Path | None = None,
    progress: Progress | None = None,
) -> Video:
    """Blocking: run via asyncio.to_thread. The caller owns workdir and removes it."""
    progress = progress or (lambda *_: None)
    progress("probe", None, None)
    try:
        with yt_dlp.YoutubeDL(_base_opts(cookies)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise _failure(e) from e
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise DownloadError("failed")
        info = entries[0]
    duration = info.get("duration")
    if duration and duration > max_duration:
        raise DownloadError("too_long")

    for height in HEIGHT_TIERS:
        tier_dir = Path(tempfile.mkdtemp(dir=workdir))
        opts = _base_opts(cookies) | {
            "format": _format(height),
            "outtmpl": str(tier_dir / "video.%(ext)s"),
            "max_filesize": max_bytes,
        } | _hooks(progress)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([info.get("webpage_url") or url])
        except yt_dlp.utils.DownloadError as e:
            raise _failure(e) from e
        files = [p for p in tier_dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not files:
            continue  # max_filesize aborted this tier
        path = max(files, key=lambda p: p.stat().st_size)
        if path.stat().st_size <= max_bytes:
            width, height = probe(path)
            return Video(
                path=path,
                title=(info.get("title") or info.get("description") or "video").strip()[:200],
                width=width,
                height=height,
                duration=int(duration) if duration else None,
            )
    raise DownloadError("too_big")
