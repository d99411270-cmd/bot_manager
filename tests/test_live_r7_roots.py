from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import FALLBACK, PHONE_QUESTION, ConversationService


@pytest.fixture
def now():
    return datetime(2026, 8, 27, 16, tzinfo=timezone.utc)


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


def _qualified(telegram_id: int, **kwargs) -> ClientProfile:
    profile = ClientProfile(
        telegram_id=telegram_id,
        name="Дима",
        phone="+790****0100",
        status="квалифицирован",
    )
    for key, value in kwargs.items():
        setattr(profile, key, value)
    return profile


@pytest.mark.asyncio
async def test_topic_switch_without_product_snapshots_original_apples(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2700000001,
            product=None,
            current_interest="яблоки сезонные",
            volume="20 кг",
            original_interests=None,
            status="получил предложение",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", product="картофель", volume="100 кг")],
        reply=FALLBACK,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2700000001, None, "нет, давай картофель 100 кг")
    )
    saved = await repo.get_client(2700000001)

    assert saved.volume is not None
    assert "100" in saved.volume
    originals = [item.lower() for item in (saved.original_interests or [])]
    assert any("яблок" in item for item in originals)
    assert "3000" in result.text


@pytest.mark.asyncio
async def test_same_topic_explicit_twelve_jars_overwrites_paru_banok(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2700000002,
            name="Лена",
            product="огурцы маринованные",
            current_interest="огурцы маринованные",
            volume="пару банок",
            status="получил предложение",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", volume="12 банок")],
        reply=FALLBACK,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2700000002, None, "12 банок")
    )
    saved = await repo.get_client(2700000002)

    assert saved.volume is not None
    assert "12" in saved.volume
    assert "1720" in result.text


@pytest.mark.asyncio
async def test_pickles_while_waiting_phone_answers_catalog_not_phone_only(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2700000003,
            name="Лена",
            status="ожидает телефон",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", product="огурцы в банках")],
        reply=PHONE_QUESTION,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2700000003, None, "огурцы в банках")
    )
    saved = await repo.get_client(2700000003)

    lowered = result.text.lower()
    assert "маринов" in lowered or "860" in result.text
    assert result.text.strip() != PHONE_QUESTION
    assert not (
        PHONE_QUESTION in result.text and "860" not in result.text and "маринов" not in lowered
    )
    assert saved.phone is None
    assert saved.status == "ожидает телефон"
    interest = (saved.current_interest or "").lower()
    assert "маринов" in interest or "банк" in interest
    assert saved.product is None
