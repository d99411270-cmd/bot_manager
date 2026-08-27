from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    FALLBACK,
    ConversationService,
    requested_identity_slot,
    resolve_catalog_query,
)


@pytest.fixture
def now():
    return datetime(2026, 8, 27, 16, tzinfo=timezone.utc)


LIVE_MANGO_STALL = (
    "Сейчас уточню по манго в каталоге. Минуту.\n\n"
    "Подскажите, вам нужен именно свежий манго или что-то из продуктов с манго — "
    "например, сок, нектар или консервация?"
)

LIVE_MORS_PACK_AND_VOLUME = (
    "Морс клюквенный «Ягодный Свет» — упаковка 6 бутылок по 1 литру, "
    "640 ₽ за упаковку. В наличии много.\n\n"
    "Подскажите, какой объём вам нужен?"
)


class CatalogSpeakingAI:
    def __init__(self, analyses=(), reply=None):
        self.analyses = list(analyses)
        self.reply = reply
        self.catalog_calls = []

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            result = self.analyses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=self.reply or FALLBACK)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return AiTurn(reply=self.reply or FALLBACK)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=self.reply or FALLBACK)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=self.reply or FALLBACK)


def _named_no_phone(telegram_id: int, *, status: str = "ожидает телефон") -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Лена",
        phone=None,
        status=status,
    )


def _qualified_mors(telegram_id: int) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Вася",
        phone="+799****0492",
        product="морс",
        current_interest="морс клюквенный",
        status="квалифицирован",
        contact_skipped=True,
    )


@pytest.mark.asyncio
async def test_phone_slot_unknown_mango_is_honest_no_match_not_invented_juice(now):
    repo = InMemoryCRMRepository()
    client = _named_no_phone(3600000001)
    await repo.save_client(client)
    assert requested_identity_slot(client) == "phone"
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="манго")],
        reply=LIVE_MANGO_STALL,
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(3600000001, None, "манго есть?"))
    saved = await repo.get_client(3600000001)

    lowered = result.text.lower()
    assert "нет в каталоге" in lowered
    assert "сок" not in lowered
    assert "нектар" not in lowered
    assert "консервац" not in lowered
    assert not ("уточню" in lowered and "минуту" in lowered)
    assert saved is not None
    assert saved.catalog_no_match_query
    assert "манго" in saved.catalog_no_match_query.lower()
    assert saved.product is None or "манго" not in saved.product.lower()
    assert saved.current_interest is None or "манго" not in saved.current_interest.lower()

    ai.analyses = [IntakeAnalysis(intent="question", product="огурцы маринованные")]
    ai.reply = "Из овощей есть картофель, морковь и лук. Какой объём смотрите?"
    pickles = await service.handle(
        IncomingMessage(3600000001, None, "а огурцы маринованные пару банок")
    )
    after = await repo.get_client(3600000001)

    pickle_text = pickles.text.lower()
    assert "860" in pickles.text
    assert "картофель" not in pickle_text
    assert "морковь" not in pickle_text
    assert "лук" not in pickle_text
    assert after is not None
    assert after.catalog_no_match_query is None


def test_phone_slot_does_not_drop_semantic_catalog_search():
    named = ClientProfile(telegram_id=3600000002, name="Лена", status="ожидает телефон")
    analysis = IntakeAnalysis(intent="question", product="манго")

    phone_query, phone_owner = resolve_catalog_query("манго есть?", analysis, named)
    anon_query, anon_owner = resolve_catalog_query(
        "манго есть?",
        analysis,
        ClientProfile(telegram_id=3600000003, status="новый"),
    )

    assert requested_identity_slot(named) == "phone"
    assert (phone_query, phone_owner) == (anon_query, anon_owner)
    assert phone_owner == "semantic"
    assert phone_query and "манго" in phone_query.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["привет", "потом", "еда", "прайс есть?"])
async def test_phone_slot_non_product_is_not_catalog_no_match(now, message):
    repo = InMemoryCRMRepository()
    await repo.save_client(_named_no_phone(3600000010))
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="greeting" if message == "привет" else "question")],
        reply="Хорошо, продолжим. Что смотрите?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(3600000010, None, message)
    )
    saved = await repo.get_client(3600000010)

    assert "нет в каталоге" not in result.text.lower()
    assert saved is not None
    assert saved.catalog_no_match_query is None


@pytest.mark.asyncio
async def test_unqualified_mors_per_liter_enforces_106_67_not_pack_only(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=3600000004,
            name="Света",
            phone=None,
            status="новый",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="морс")],
        reply=LIVE_MORS_PACK_AND_VOLUME,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(3600000004, None, "а морс за литр")
    )
    saved = await repo.get_client(3600000004)

    compact = result.text.replace(",", ".")
    assert "106.67" in compact
    assert "33.33" not in compact
    assert saved is not None
    assert saved.volume != "1 л"
    assert saved.volume is None or "1 л" not in saved.volume
    interest = (saved.current_interest or saved.product or "").lower()
    assert "вод" not in interest
    assert "морс" in interest or "106.67" in compact


@pytest.mark.asyncio
async def test_qualified_mors_200_liters_still_nearest_packs(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_qualified_mors(3600000005))
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="морс")],
        reply=FALLBACK,
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(3600000005, None, "200 литров")
    )

    assert "21120" in result.text
    assert "21760" in result.text


@pytest.mark.asyncio
async def test_mors_price_per_liter_still_106_67(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_qualified_mors(3600000006))
    ai = CatalogSpeakingAI(analyses=[IntakeAnalysis(intent="question", product="морс")])

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(3600000006, None, "морс цена за литр")
    )

    compact = result.text.replace(",", ".")
    assert "106.67" in compact
    assert "33.33" not in compact
