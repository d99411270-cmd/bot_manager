from aiogram.client.session.aiohttp import AiohttpSession

from stokozavr_bot.__main__ import build_bot
from stokozavr_bot.config import Settings


def settings_kwargs(**overrides):
    values = {
        "_env_file": None,
        "TELEGRAM_BOT_TOKEN": "123456789:test-token",
        "DEEPSEEK_API_KEY": "test-deepseek-key",
        "GOOGLE_SPREADSHEET_ID": "test-sheet",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
    }
    values.update(overrides)
    return values


def test_telegram_proxy_url_is_empty_by_default():
    settings = Settings(**settings_kwargs())

    assert settings.telegram_proxy_url == ""


def test_build_bot_uses_proxy_session_when_telegram_proxy_url_is_set():
    proxy_url = "socks5://127.0.0.1:10808"
    settings = Settings(**settings_kwargs(TELEGRAM_PROXY_URL=proxy_url))

    bot = build_bot(settings)

    assert isinstance(bot.session, AiohttpSession)
    assert bot.session._proxy == proxy_url


def test_build_bot_keeps_default_session_without_telegram_proxy_url():
    settings = Settings(**settings_kwargs())

    bot = build_bot(settings)

    assert isinstance(bot.session, AiohttpSession)
    assert bot.session._proxy is None
