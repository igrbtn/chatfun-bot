import pytest

import config


@pytest.fixture
def env(monkeypatch):
    for name in ("ADMIN_IDS", "ALLOWED_CHATS", "MAX_UPLOAD_MB", "MAX_DURATION_SEC", "WEB_PORT", "TZ"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    return monkeypatch


def test_tolerant_id_lists(env):
    env.setenv("ADMIN_IDS", '"123, 456;789 1000"  # me and friends')
    env.setenv("ALLOWED_CHATS", "-1003835089169,-1001524411948")
    cfg = config.load()
    assert cfg.admin_ids == {123, 456, 789, 1000}
    assert cfg.allowed_chats == {-1003835089169, -1001524411948}  # 14 chars, nothing truncated


def test_username_instead_of_id_is_explained(env):
    env.setenv("ADMIN_IDS", "@vasya")
    with pytest.raises(config.ConfigError) as err:
        config.load()
    assert "ADMIN_IDS" in str(err.value) and "@userinfobot" in str(err.value)


@pytest.mark.parametrize("name, value", [("MAX_UPLOAD_MB", "49MB"), ("WEB_PORT", "port"), ("TZ", "Mars/Base")])
def test_bad_numbers_and_tz_name_the_variable(env, name, value):
    env.setenv(name, value)
    with pytest.raises(config.ConfigError) as err:
        config.load()
    assert name in str(err.value)


def test_defaults_and_empty_values(env):
    env.setenv("MAX_UPLOAD_MB", "")
    env.setenv("WEB_PORT", "'8096'")
    cfg = config.load()
    assert cfg.max_upload_bytes == 49 * 1024 * 1024 and cfg.web_port == 8096 and cfg.admin_ids == frozenset()


def test_empty_token(env):
    env.setenv("BOT_TOKEN", "  ")
    with pytest.raises(config.ConfigError):
        config.load()
