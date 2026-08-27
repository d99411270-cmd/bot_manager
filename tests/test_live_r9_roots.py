from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import FALLBACK, PHONE_QUESTION, ConversationService


@pytest.fixture
def now():
    return datetime(2026, 8, 27, 20, tzinfo=timezone.utc)


class CatalogSpeakingAI:
    def __init__(self, analyses=(), reply=None, volume=None):
        self.analyses = list(analyses)
        self.reply = reply
        self.volume = volume
        self.catalog_calls = []

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            result = self.analyses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=self.reply or FALLBACK, volume=self.volume)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return AiTurn(reply=self.reply or FALLBACK, volume=self.volume)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=self.reply or FALLBACK, volume=self.volume)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=self.reply or FALLBACK, volume=self.volume)


@pytest.mark.asyncio
async def test_phone_slot_volume_only_twelve_jars_quotes_1720_not_phone_greeting(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2900000001,
            name="Лена",
            product=None,
            current_interest="огурцы маринованные",
            volume="пару банок",
            status="ожидает телефон",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", volume="12 банок")],
        reply=f"Очень приятно, Лена.\n{PHONE_QUESTION}",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2900000001, None, "12 банок")
    )
    saved = await repo.get_client(2900000001)

    assert saved.volume is not None
    assert "12" in saved.volume
    assert "1720" in result.text
    assert result.text.strip() != PHONE_QUESTION
    assert not result.text.strip().startswith("Очень приятно") or "1720" in result.text
    assert saved.phone is None
    assert saved.status == "ожидает телефон"
    assert saved.product is None
