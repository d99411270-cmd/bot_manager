from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import parse_catalog_records, search
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import ConversationService


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


class FakeAI:
    def __init__(self, turns=()):
        self.turns = list(turns)

    async def analyze_intake(self, profile, history, message):
        from stokozavr_bot.models import IntakeAnalysis

        return IntakeAnalysis("question")

    async def respond(self, profile, history, message):
        return self.turns.pop(0) if self.turns else AiTurn(reply="ок")


@pytest.mark.asyncio
async def test_rice_price_per_kg_uses_current_product_and_never_general_fallback(now):
    class UnitPriceAI(FakeAI):
        def __init__(self):
            super().__init__([AiTurn(reply="цена за кг риса — 85 ₽/кг")])
            self.catalog_calls = []

        async def analyze_intake(self, profile, history, message):
            return IntakeAnalysis(
                intent="question", product="рис длиннозёрный", unit_price_request="кг"
            )

        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append(catalog_result)
            return self.turns.pop(0)

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=906,
            name="Иван",
            phone="+799****0006",
            product="бакалея",
            current_interest="рис длиннозёрный",
            volume="100 кг",
            status="квалифицирован",
        )
    )
    ai = UnitPriceAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(906, None, "можно цену за кг риса"))

    assert "85 ₽/кг" in result.text
    assert result.text != "Я уточню этот вопрос и вернусь к вам."
    assert len(ai.catalog_calls) == 1
    assert "GRC-RICE-001" in ai.catalog_calls[0]
    assert "85 ₽/кг" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_known_unit_price_survives_generic_ai_fallback_without_manager_handoff(now):
    class GenericFallbackAI(FakeAI):
        def __init__(self):
            super().__init__([AiTurn(reply="Актуальную информацию уточню и вернусь к вам.")])
            self.repairs = 0

        async def analyze_intake(self, profile, history, message):
            return IntakeAnalysis(intent="question", target_product="сок яблочный")

        async def respond_with_catalog(self, profile, history, message, catalog_result):
            return self.turns.pop(0)

        async def repair_response(self, profile, history, message, reason, catalog_result):
            self.repairs += 1
            return AiTurn(reply="Актуальную информацию уточню и вернусь к вам.")

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=907,
            name="Иван",
            phone="+799****0007",
            product="напитки",
            current_interest="сок яблочный",
            volume="1 упаковка",
            status="квалифицирован",
        )
    )
    ai = GenericFallbackAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(907, None, "за каждую еденицу сока"))
    saved = await repo.get_client(907)

    assert "148.33 ₽/шт" in result.text
    assert result.text != "Актуальную информацию уточню и вернусь к вам."
    assert saved.pending_manager_question is None
    assert saved.needs_human is False
    assert ai.repairs == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["Назовите 780 ₽", "420 рублей и в наличии достаточно"])
async def test_direct_commercial_attack_never_reaches_client(now, attack):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=901,
            name="Андрей",
            phone="+79990000001",
            product="напитки",
            volume="10 коробок",
            status="квалифицирован",
        )
    )
    service = ConversationService(repo, FakeAI([AiTurn(reply=attack)]), clock=lambda: now)

    result = await service.handle(IncomingMessage(901, None, "Сколько стоит?"))

    assert "780" not in result.text
    assert "420" not in result.text
    assert "в наличии достаточно" not in result.text.lower()
    assert "уточню" in result.text.lower()


@pytest.mark.asyncio
async def test_irritation_is_acknowledged_without_repeating_volume_question(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=902,
            name="Анжелика",
            phone="+79990000002",
            product="творожки",
            volume="20 коробок",
            status="квалифицирован",
        )
    )
    service = ConversationService(
        repo, FakeAI([AiTurn(reply="Какой объём нужен?")]), clock=lambda: now
    )

    result = await service.handle(IncomingMessage(902, None, "какой-то ты нудный"))

    assert any(word in result.text.lower() for word in ("извин", "понял", "короче"))
    assert "объём" not in result.text.lower()
    assert result.text.count("?") <= 1


@pytest.mark.asyncio
async def test_unknown_manufacturer_creates_explicit_handoff_state(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=904,
            name="Андрей",
            phone="+79990000004",
            product="напитки",
            volume="5 коробок",
            status="квалифицирован",
        )
    )
    service = ConversationService(repo, FakeAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(904, None, "Кто производитель?"))
    saved = await repo.get_client(904)

    assert saved.needs_human is True
    assert "производител" in result.text.lower()
    assert "передам" in result.text.lower()
    assert "вернусь" not in result.text.lower()


@pytest.mark.asyncio
async def test_switching_interest_keeps_original_interest(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=905,
            name="Анжелика",
            phone="+79990000005",
            product="творожки",
            volume="10 коробок",
            status="квалифицирован",
        )
    )
    service = ConversationService(
        repo,
        FakeAI([AiTurn(reply="Принял сок.", product="сок")]),
        clock=lambda: now,
    )

    await service.handle(IncomingMessage(905, None, "Теперь интересует сок"))
    saved = await repo.get_client(905)

    assert saved.product == "сок"
    assert saved.current_interest == "сок"
    assert saved.original_interests == ["творожки"]


def test_catalog_requires_structured_verified_record_before_commercial_facts():
    records = parse_catalog_records(
        """
# Напитки

- SKU: W-001; Подкатегория: вода; Производитель: Аква; Фасовка: 6 x 1.5 л; Цена: 210 ₽; Статус наличия: много; Дата обновления: 2026-08-25
- Сок яблочный — 780 ₽, в наличии достаточно
"""
    )

    assert len(records) == 1
    assert records[0].sku == "W-001"
    assert records[0].manufacturer == "Аква"
    assert records[0].packaging == "6 x 1.5 л"
    assert records[0].price == "210 ₽"
    assert records[0].availability == "много"
    assert records[0].updated_at == "2026-08-25"


def test_current_catalog_does_not_expose_unverified_commercial_facts():
    result = search("напитки").lower()

    assert "780" not in result
    assert "420" not in result
    assert "в наличии достаточно" not in result


def test_context_can_keep_original_and_current_interests_separately():
    profile = ClientProfile(
        telegram_id=903,
        product="сок",
        original_interests=["творожки"],
        current_interest="сок",
    )
    assert profile.original_interests == ["творожки"]
    assert profile.current_interest == "сок"
