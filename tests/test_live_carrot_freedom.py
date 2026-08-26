from datetime import datetime, timezone

import pytest

from stokozavr_bot.catalog_quotes import line_total_quote
from stokozavr_bot.deepseek import SYSTEM_PROMPT
from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import search, unit_price_quote
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    FALLBACK,
    ConversationService,
    is_unsafe_claim,
    resolve_catalog_query,
)


@pytest.fixture
def now():
    return datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class GenericCatalogAI:
    """Live-shaped AI: intake may fail, catalog answers are the FALLBACK muzzle."""

    def __init__(self, analyses=(), reply=FALLBACK):
        self.analyses = list(analyses)
        self.reply = reply
        self.catalog_calls = []
        self.repairs = 0
        self.open_dialogs = 0

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            result = self.analyses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=self.reply)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return AiTurn(reply=self.reply)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        self.repairs += 1
        return AiTurn(reply=self.reply)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        self.open_dialogs += 1
        return AiTurn(reply=self.reply)


def _browsing_client(telegram_id: int) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Алексей",
        phone="+799****0410",
        status="уточнение продукта",
        contact_skipped=True,
    )


def _assert_not_fallback(text: str) -> None:
    lowered = text.lower()
    assert FALLBACK.lower() not in lowered
    assert "уточню" not in lowered or "вернус" not in lowered
    assert "нет в каталоге" not in lowered


def test_system_prompt_lets_deepseek_speak_instead_of_fallback_muzzle():
    assert FALLBACK not in SYSTEM_PROMPT
    assert "ответь ровно" not in SYSTEM_PROMPT.lower()
    assert "needs_human" in SYSTEM_PROMPT
    assert "живым" in SYSTEM_PROMPT.lower() or "жив" in SYSTEM_PROMPT.lower()


def test_unit_price_quote_carrot_bag_is_41_per_kg_without_piece_count():
    quote = unit_price_quote("морковь", "кг")

    assert quote is not None
    assert quote.unit_price == "41 ₽/кг"
    assert quote.total_quantity == "10 кг"
    assert quote.record.sku == "VEG-CARROT-001"
    assert quote.record.packaging == "мешок 10 кг"


def test_unit_price_quote_mesh_and_box_packaging_also_divide_by_kg():
    potato = unit_price_quote("картофель продовольственный", "кг")
    cucumber = unit_price_quote("огурцы короткоплодные", "кг")

    assert potato is not None
    assert potato.unit_price == "30 ₽/кг"
    assert cucumber is not None
    assert cucumber.unit_price == "136 ₽/кг"


def test_kept_unit_and_line_goldens_rice_corn_juice_apples():
    rice = unit_price_quote("рис длиннозёрный", "кг")
    corn = unit_price_quote("кукуруза сладкая", "шт")
    juice = unit_price_quote("сок яблочный", "л")
    apples = line_total_quote("яблоки", "20 кг")

    assert rice is not None and rice.unit_price == "85 ₽/кг"
    assert corn is not None and corn.unit_price == "57.50 ₽/шт"
    assert juice is not None and juice.unit_price == "148.33 ₽/л"
    assert apples.total == "1640 ₽"


def test_carrot_41_per_kg_is_allowed_on_raw_search_not_only_dump_text():
    catalog = search("морковь")

    assert "410" in catalog
    assert is_unsafe_claim("Морковь — 41 ₽/кг (410 ₽ за мешок 10 кг).", catalog) is False
    assert is_unsafe_claim("Морковь 35 ₽/кг, скидка за объём.", catalog) is True
    assert is_unsafe_claim("остаток: 12", catalog) is True


def test_vsya_is_assortment_query_not_unknown_sku():
    client = _browsing_client(2100000411)
    query, owner = resolve_catalog_query(
        "Вся",
        IntakeAnalysis(intent="question", product="Вся"),
        client,
    )

    assert owner != "semantic"
    assert query != "Вся"
    listing = search(query or "")
    assert "Доступные категории" in listing
    assert "CATALOG_RESULT_EMPTY" not in listing
    assert "VEG-CARROT-001" not in listing or query == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["Вся", "все", "всё", "всё что есть", "что есть"],
)
async def test_live_vsya_is_assortment_not_no_match(now, message):
    repo = InMemoryCRMRepository()
    await repo.save_client(_browsing_client(2100000412))
    ai = GenericCatalogAI(analyses=[IntakeAnalysis(intent="question", product="Вся")])

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000412, None, message)
    )
    saved = await repo.get_client(2100000412)

    _assert_not_fallback(result.text)
    assert saved.product != "Вся"
    assert saved.catalog_no_match_query is None
    assert ai.catalog_calls
    assert "CATALOG_RESULT_EMPTY" not in ai.catalog_calls[0]
    assert "Доступные категории" in ai.catalog_calls[0] or "бакале" in result.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["морковь сколько за кг", "Морква сколько за кг"])
async def test_live_carrot_per_kg_is_41_not_fallback_or_dump(now, message):
    repo = InMemoryCRMRepository()
    await repo.save_client(_browsing_client(2100000413))
    ai = GenericCatalogAI()

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000413, None, message)
    )
    saved = await repo.get_client(2100000413)

    _assert_not_fallback(result.text)
    assert "41 ₽" in result.text
    assert "SKU:" not in result.text
    assert saved.volume != "1 кг"
    assert saved.needs_human is False


@pytest.mark.asyncio
async def test_price_for_one_kg_is_unit_price_not_saved_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_browsing_client(2100000414))
    ai = GenericCatalogAI(
        analyses=[IntakeAnalysis(intent="question", product="морковь", volume="1 кг")]
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000414, None, "морковь цена за 1 кг")
    )
    saved = await repo.get_client(2100000414)

    _assert_not_fallback(result.text)
    assert "41 ₽" in result.text
    assert saved.volume is None


@pytest.mark.asyncio
async def test_intake_json_error_still_grounds_known_carrot_unit_price(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_browsing_client(2100000415))
    ai = GenericCatalogAI(analyses=[ValueError("JSONDecodeError")])

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000415, None, "морковь сколько за кг")
    )
    saved = await repo.get_client(2100000415)

    _assert_not_fallback(result.text)
    assert "41 ₽" in result.text
    assert saved.volume != "1 кг"


@pytest.mark.asyncio
async def test_live_41_reply_on_raw_carrot_search_is_accepted(now):
    class LiveAI(GenericCatalogAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append(catalog_result)
            return AiTurn(reply="Морковь — 41 ₽/кг (410 ₽ за мешок 10 кг).")

        async def repair_response(self, *args):
            self.repairs += 1
            raise AssertionError("grounded 41 ₽/кг must not be repaired")

    repo = InMemoryCRMRepository()
    await repo.save_client(_browsing_client(2100000416))
    ai = LiveAI(analyses=[IntakeAnalysis(intent="question", product="морковь")])

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000416, None, "морковь какая цена?")
    )

    assert "41 ₽" in result.text
    assert "410" in result.text
    assert ai.repairs == 0
