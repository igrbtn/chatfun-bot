from downloader import extract_urls, is_supported


def test_extracts_supported_links_in_order():
    text = (
        "look https://www.instagram.com/reel/ABC123/?igsh=xx and also https://youtu.be/dQw4w9WgXcQ, "
        "and this one is not https://example.com/video.mp4"
    )
    assert extract_urls(text) == [
        "https://www.instagram.com/reel/ABC123/?igsh=xx",
        "https://youtu.be/dQw4w9WgXcQ",
    ]


def test_dedup_and_trailing_punctuation():
    text = "https://x.com/a/status/1). https://x.com/a/status/1"
    assert extract_urls(text) == ["https://x.com/a/status/1"]


def test_hosts():
    assert is_supported("https://m.facebook.com/watch/?v=1")
    assert is_supported("https://fb.watch/abc/")
    assert is_supported("https://vm.tiktok.com/ZM1/")
    assert is_supported("https://www.youtube.com/shorts/xyz")
    assert not is_supported("https://notyoutube.com/x")
    assert not is_supported("https://youtube.com.evil.io/x")


def test_empty():
    assert extract_urls(None) == []
    assert extract_urls("no links here") == []


def test_auth_errors_are_told_apart():
    from downloader import _failure

    msg = "ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies"
    assert _failure(Exception(msg)).reason == "auth"
    assert _failure(Exception("ERROR: Unsupported URL")).reason == "failed"


def test_hidden_links_are_found():
    from downloader import all_urls

    assert extract_urls("watch this", ["https://www.tiktok.com/@a/video/1"]) == ["https://www.tiktok.com/@a/video/1"]
    assert all_urls("https://x.com/a/status/1 https://example.org/p", ["https://x.com/a/status/1"]) == [
        "https://x.com/a/status/1",
        "https://example.org/p",
    ]


def test_entity_links_without_scheme():
    from types import SimpleNamespace

    from aiogram.types import MessageEntity

    from handlers import entity_links

    text = "look youtu.be/abc and this"
    msg = SimpleNamespace(
        text=text,
        entities=[
            MessageEntity(type="url", offset=5, length=12),
            MessageEntity(type="text_link", offset=22, length=4, url="https://vm.tiktok.com/Z1/"),
        ],
        caption=None,
        caption_entities=None,
    )
    links = entity_links(msg)
    assert links == ["https://youtu.be/abc", "https://vm.tiktok.com/Z1/"]
    assert extract_urls(text, links) == links


def test_link_handler_is_registered():
    import handlers

    callbacks = [h.callback for h in handlers.router.message.handlers]
    assert handlers.on_links in callbacks
    assert handlers.entity_links not in callbacks
