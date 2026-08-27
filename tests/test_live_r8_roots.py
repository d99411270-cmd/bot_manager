from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import FALLBACK, PHONE_QUESTION, ConversationService


@pytest.fixture
def now():
    return datetime(2026, 8, 27, 18, tzinfo=timezone.utc)


class CatalogSpeakingAI:
    def __init__(self, analyses=(), reply=None, volume=None):
        self.analyses = list(analyses)
        self.replies = list(reply) if isinstance(reply, (list, tuple)) else [reply]
        self.volume = volume
        self.catalog_calls = []

    def _next_reply(self):
        if not self.replies:
            return FALLBACK
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0] or FALLBACK

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            result = self.analyses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=self._next_reply(), volume=self.volume)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return AiTurn(reply=self._next_reply(), volume=self.volume)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=self._next_reply(), volume=self.volume)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=self._next_reply(), volume=self.volume)


@pytest.mark.asyncio
async def test_phone_slot_topic_switch_snapshots_apples_and_quotes_potato(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2800000001,
            name="Дима",
            product=None,
            current_interest="яблоки сезонные",
            volume="20 кг",
            original_interests=None,
            status="ожидает телефон",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", product="картофель", volume="100 кг")],
        reply=FALLBACK,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2800000001, None, "нет, давай картофель 100 кг")
    )
    saved = await repo.get_client(2800000001)

    assert saved.volume is not None
    assert "100" in saved.volume
    originals = [item.lower() for item in (saved.original_interests or [])]
    assert any("яблок" in item for item in originals)
    assert "3000" in result.text
    interest = (saved.current_interest or "").lower()
    assert "картофел" in interest
    assert saved.phone is None
    assert saved.status == "ожидает телефон"


@pytest.mark.asyncio
async def test_phone_slot_carrot_binds_interest_so_stock_anaphora_is_grounded(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2800000002,
            name="Таня",
            status="ожидает телефон",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[
            IntakeAnalysis(intent="provide_data", product="морковь"),
            IntakeAnalysis(intent="question"),
        ],
        reply=[PHONE_QUESTION, FALLBACK],
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(2800000002, None, "морковь"))
    mid = await repo.get_client(2800000002)
    interest = (mid.current_interest or "").lower()
    lowered = first.text.lower()
    assert "морков" in interest
    assert "морков" in lowered or "410" in first.text
    assert first.text.strip() != PHONE_QUESTION
    assert mid.product is None
    assert mid.phone is None
    assert mid.status == "ожидает телефон"

    second = await service.handle(IncomingMessage(2800000002, None, "в наличии есть?"))
    saved = await repo.get_client(2800000002)
    second_text = second.text.lower()
    assert "вернус" not in second_text
    assert "уточню" not in second_text
    assert "морков" in second_text or "410" in second.text
    assert saved.needs_human is False
    assert saved.phone is None
    assert saved.status == "ожидает телефон"
