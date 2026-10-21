import asyncio

import pytest

import core
import downloader
from fakes import FakeBot
from storage import Store

GROUP, PRIVATE = -100500, 42


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = Store(":memory:")
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "STATUS_EVERY", 0.01)
    return store, FakeBot()


def fake_download(fail: str | None = None):
    def run(url, workdir, max_bytes, max_duration, cookies=None, progress=None):
        progress("download", 3 * core.MB, 6 * core.MB)
        if fail:
            raise downloader.DownloadError(fail)
        path = workdir / "video.mp4"
        path.write_bytes(b"x")
        return downloader.Video(path=path, title="Clip", width=720, height=1280, duration=10)
    return run


def test_progress_rendering():
    assert core.progress_bar(5, 10) == core.t("bar_full") * 5 + core.t("bar_empty") * 5
    assert core.progress_bar(None, None) == core.t("bar_empty") * 10
    text = core.render_status("Go", "download", 3 * core.MB, 6 * core.MB)
    assert "50%" in text and "3.0/6.0" in text
    assert "<" not in core.render_status("<b>", "probe").split("\n")[0]


def test_success_swaps_status_for_video(env, monkeypatch):
    store, bot = env
    monkeypatch.setattr(downloader, "download", fake_download())
    assert asyncio.run(core.deliver_video(bot, GROUP, "https://youtu.be/x", reply_to=7))
    status_id = 101
    assert [s[0] for s in bot.sent if s[0] != "action"] == ["message", "video"]
    assert (GROUP, status_id) in bot.deleted
    assert store.messages(GROUP) == [(102, "video")]


def test_group_error_expires_and_quiet_fail_vanishes(env, monkeypatch):
    store, bot = env
    monkeypatch.setattr(downloader, "download", fake_download("too_long"))
    assert not asyncio.run(core.deliver_video(bot, GROUP, "https://youtu.be/x", quiet_fail=True))
    assert store.messages(GROUP) == [(101, "error")]
    assert store.due_for_delete(10**12) == [(GROUP, 101)]

    monkeypatch.setattr(downloader, "download", fake_download("failed"))
    asyncio.run(core.deliver_video(bot, GROUP, "https://youtu.be/y", quiet_fail=True))
    assert (GROUP, 102) in bot.deleted


def test_private_error_stays(env, monkeypatch):
    store, bot = env
    monkeypatch.setattr(downloader, "download", fake_download("auth"))
    asyncio.run(core.deliver_video(bot, PRIVATE, "https://youtu.be/x"))
    assert bot.edits[-1][2] == core.t("dl_auth")
    assert store.messages(PRIVATE) == [] and bot.deleted == []


def test_orphan_statuses_are_swept(env):
    store, _bot = env
    store.remember(GROUP, 5, "status", now=1000.0)
    store.remember(GROUP, 6, "video", now=1000.0)
    assert store.orphan_statuses(now=2000.0) == 1
    assert store.due_for_delete(now=2000.0) == [(GROUP, 5)]
    assert store.prune_messages(now=1000.0 + 49 * 3600) == 2
