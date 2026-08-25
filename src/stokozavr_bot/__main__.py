import asyncio
import logging
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from .config import Settings
from .deepseek import DeepSeekClient
from .followup_worker import followup_loop
from .google_sheets import GoogleSheetsCRMRepository
from .service import ConversationService
from .telegram import create_router


def build_bot(settings: Settings) -> Bot:
    if settings.telegram_proxy_url:
        session = AiohttpSession(proxy=settings.telegram_proxy_url)
        return Bot(settings.telegram_bot_token, session=session)
    return Bot(settings.telegram_bot_token)


async def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    repository = GoogleSheetsCRMRepository.from_service_account(
        settings.google_spreadsheet_id,
        credentials_file=settings.google_service_account_file,
        credentials_json=settings.google_service_account_json,
    )
    ai = DeepSeekClient(
        settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.deepseek_timeout_seconds,
        max_tokens=settings.deepseek_max_tokens,
    )
    service = ConversationService(
        repository,
        ai,
        followup_delay=timedelta(seconds=settings.followup_delay_seconds),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_router(
            service,
            repository,
            delay_probability=settings.reply_delay_probability,
        )
    )
    bot = build_bot(settings)

    async def send_followup(telegram_id: int, text: str) -> None:
        await bot.send_message(telegram_id, text)

    worker = asyncio.create_task(
        followup_loop(
            repository,
            send_followup,
            interval_seconds=settings.followup_poll_seconds,
            planner=ai.plan_followup,
        )
    )
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        worker.cancel()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
