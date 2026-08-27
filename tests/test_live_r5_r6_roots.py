from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import FALLBACK, ConversationService


@pytest.fixture
def now():
    return datetime(2026, 8, 27, 12, tzinfo=timezone.utc)


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
async def test_packaging_box_size_question_does_not_overwrite_order_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000001,
            name="Олег",
            product="яблоки",
            current_interest="яблоки",
            volume="20 кг",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", volume="10 кг")],
        reply="Короб 10 кг, 820 ₽ за короб.",
        volume="10 кг",
    )

    await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000001, None, "а фасовка какая, короб точно 10 кг?")
    )
    saved = await repo.get_client(2600000001)

    assert saved.volume == "20 кг"


@pytest.mark.asyncio
async def test_packaging_box_size_question_does_not_fill_empty_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000002,
            name="Олег",
            product="яблоки",
            current_interest="яблоки",
            status="уточнение объёма",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", volume="10 кг")],
        reply="Короб 10 кг, 820 ₽.",
        volume="10 кг",
    )

    await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000002, None, "а фасовка какая, короб точно 10 кг?")
    )
    saved = await repo.get_client(2600000002)

    assert saved.volume is None


@pytest.mark.asyncio
async def test_explicit_quantity_on_new_topic_overwrites_volume_and_keeps_apples(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000003,
            product="яблоки",
            current_interest="яблоки",
            volume="20 кг",
            status="получил предложение",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", product="картофель", volume="100 кг")],
        reply=FALLBACK,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000003, None, "нет, давай картофель 100 кг")
    )
    saved = await repo.get_client(2600000003)

    assert saved.volume is not None
    assert "100" in saved.volume
    assert "картофел" in (saved.current_interest or saved.product or "").lower()
    originals = [item.lower() for item in (saved.original_interests or [])]
    assert any("яблок" in item for item in originals)
    assert "3000" in result.text
    assert "1640" not in result.text


@pytest.mark.asyncio
async def test_jars_after_cucumbers_in_cans_quotes_pickles_not_veg_dump(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000004,
            name="Лена",
            product="овощи",
            current_interest="овощи",
            status="получил предложение",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[
            IntakeAnalysis(intent="question", product="овощи"),
            IntakeAnalysis(intent="question", volume="12 банок"),
        ],
        reply=FALLBACK,
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(2600000004, None, "огурцы в банках"))
    mid = await repo.get_client(2600000004)
    result = await service.handle(IncomingMessage(2600000004, None, "12 банок"))
    saved = await repo.get_client(2600000004)

    interest = (saved.current_interest or saved.product or "").lower()
    assert "маринов" in interest or "банк" in interest
    assert interest != "овощи"
    assert "1720" in result.text
    assert "картофель" not in result.text.lower()
    assert "морковь" not in result.text.lower()
    first_interest = (mid.current_interest or mid.product or "").lower()
    assert first_interest != "овощи"
    assert (
        "огурец" in first.text.lower()
        or "маринов" in first.text.lower()
        or "банк" in first.text.lower()
    )


@pytest.mark.asyncio
async def test_unit_price_anaphora_uses_mors_not_sibling_water(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000005,
            name="Вера",
            product="морс",
            current_interest="морс",
            status="квалифицирован",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", unit_price_request="л")],
        reply=FALLBACK,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000005, None, "а за литр?")
    )
    saved = await repo.get_client(2600000005)

    assert "106.67" in result.text
    assert "33.33" not in result.text
    assert "Источник Луга" not in result.text
    assert "вод" not in result.text.lower() or "морс" in result.text.lower()
    assert saved.current_interest and "морс" in saved.current_interest.lower()


@pytest.mark.asyncio
async def test_known_apple_manufacturer_is_answered_not_handoff(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000006,
            name="Олег",
            product="яблоки",
            current_interest="яблоки",
            volume="20 кг",
            status="получил предложение",
        )
    )
    ai = CatalogSpeakingAI(analyses=[IntakeAnalysis(intent="question")], reply=FALLBACK)

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000006, None, "другой производитель?")
    )
    saved = await repo.get_client(2600000006)

    lowered = result.text.lower()
    assert "садовый север" in lowered
    assert "нет подтверждённого производителя" not in lowered
    assert saved.needs_human is False
    assert "яблоневый край" not in lowered
    assert "900" in result.text
    assert "сетев" in lowered or "розничн" in lowered


@pytest.mark.asyncio
async def test_irritation_does_not_reask_volume_on_later_turns(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000007,
            name="Таня",
            product="яблоки",
            current_interest="яблоки",
            volume=None,
            status="уточнение объёма",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[
            IntakeAnalysis(intent="question"),
            IntakeAnalysis(intent="question"),
            IntakeAnalysis(intent="question"),
        ],
        reply="Подскажите, пожалуйста, какой объём продукции вам необходим?",
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(2600000007, None, "какой-то ты нудный"))
    second = await service.handle(IncomingMessage(2600000007, None, "яблоки ещё в наличии?"))
    third = await service.handle(IncomingMessage(2600000007, None, "ну так что"))
    saved = await repo.get_client(2600000007)

    assert any(word in first.text.lower() for word in ("извин", "понял", "короче"))
    for reply in (first, second, third):
        lowered = reply.text.lower()
        assert "объём" not in lowered
        assert "объем" not in lowered
        assert "телефон" not in lowered
        assert "номер" not in lowered
    assert saved.volume is None


@pytest.mark.asyncio
async def test_call_channel_does_not_ask_visit_or_reask_confirmed_slot(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000008,
            name="Илья",
            product="яблоки",
            current_interest="яблоки",
            volume="20 кг",
            status="готов к заказу",
            fulfillment_channel="call",
            requested_slot="16:00",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question")],
        reply="Отлично. Во сколько вам удобно приехать?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000008, None, "во сколько тогда?")
    )
    saved = await repo.get_client(2600000008)

    lowered = result.text.lower()
    assert saved.requested_slot == "16:00"
    assert saved.fulfillment_channel == "call"
    assert "приехать" not in lowered
    assert "аустрина" not in lowered
    assert "во сколько" not in lowered
    assert "16:00" in result.text


@pytest.mark.asyncio
async def test_cafe_after_occupation_question_is_not_catalog_no_match(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000009,
            name="Катя",
            product=None,
            current_interest=None,
            volume=None,
            status="уточнение продукта",
        )
    )
    await repo.append_history(
        2600000009,
        now,
        "не знаю",
        "Понимаю. Давайте подберём под вашу точку — вы магазин или кафе? "
        "Подскажите, чем занимаетесь, и я сориентирую по ассортименту.",
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", product="кафе")],
        reply="Тогда сориентирую по ходовым позициям. Что смотрите в первую очередь?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000009, None, "кафе")
    )
    saved = await repo.get_client(2600000009)

    assert saved.catalog_no_match_query != "кафе"
    assert saved.catalog_no_match_query is None
    assert saved.product != "кафе"
    assert "нет в каталоге" not in result.text.lower()
    assert "CATALOG_RESULT_EMPTY" not in (ai.catalog_calls[0] if ai.catalog_calls else "")


@pytest.mark.asyncio
async def test_mors_200l_recovery_quotes_both_nearest_pack_edges(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _qualified(
            2600000010,
            name="Вася",
            product="морс",
            current_interest="морс клюквенный",
            volume="200 литров",
            status="квалифицирован",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="морс", volume="200 литров")],
        reply="Сейчас не могу подтвердить ответ по этому вопросу по каталогу.",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2600000010, None, "200 литров")
    )

    lowered = result.text.lower()
    assert "не могу подтвердить" not in lowered
    assert "21120" in result.text
    assert "21760" in result.text
    assert "198" in result.text
    assert "204" in result.text
    assert "33" in result.text
    assert "34" in result.text
