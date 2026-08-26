from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import FALLBACK, ConversationService


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


class FailingCatalogAI:
    """Live-shaped AI: intake works, every catalog reply is generic."""

    def __init__(self, analyses=(), reply="Я уточню этот вопрос и вернусь к вам."):
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
        return AiTurn(reply="Сейчас не могу подтвердить ответ по этому вопросу по каталогу.")

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        self.open_dialogs += 1
        return AiTurn(reply=self.reply)


def _qualified_apples() -> ClientProfile:
    return ClientProfile(
        telegram_id=2100000001,
        name="Марина",
        product="яблоки",
        current_interest="яблоки",
        volume="20 кг",
        status="квалифицирован",
        contact_skipped=True,
    )


def _assert_not_generic(text: str) -> None:
    lowered = text.lower()
    assert FALLBACK.lower() not in lowered
    assert "уточню" not in lowered or "вернус" not in lowered
    assert "не могу подтвердить" not in lowered


@pytest.mark.asyncio
async def test_apples_20kg_quote_survives_generic_ai(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_qualified_apples())
    ai = FailingCatalogAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000001, None, "так сколько будет за 20 кг яблок?")
    )
    saved = await repo.get_client(2100000001)

    _assert_not_generic(result.text)
    assert "1640" in result.text
    assert "820" in result.text
    assert saved.needs_human is False
    assert saved.pending_manager_question is None
    assert ai.catalog_calls
    assert "1640" in ai.catalog_calls[0]
    assert "Подтверждённый расчёт" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_severnaya_kaplya_10_packs_quote_survives_generic_ai(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000007,
            name="Ольга",
            product="напитки",
            current_interest="напитки",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = FailingCatalogAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000007, None, "10 упаковок Северной Капли это сколько?")
    )
    saved = await repo.get_client(2100000007)

    _assert_not_generic(result.text)
    assert "8900" in result.text
    assert "890" in result.text
    assert "лимон" not in result.text.lower()
    assert saved.needs_human is False
    assert ai.catalog_calls
    assert "8900" in ai.catalog_calls[0]
    assert "SOK-APPLE-001" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_corn_36_cans_quote_survives_generic_ai(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000005,
            name="Дмитрий",
            product="консервация",
            current_interest="кукуруза сладкая",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = FailingCatalogAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000005, None, "36 банок кукурузы это сколько?")
    )
    saved = await repo.get_client(2100000005)

    _assert_not_generic(result.text)
    assert "2070" in result.text
    assert "57.50" in result.text or "57,50" in result.text
    assert saved.needs_human is False
    assert ai.catalog_calls
    assert "2070" in ai.catalog_calls[0]
    assert "CAN-CORN-001" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_potato_100kg_quote_survives_generic_ai(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000006,
            name="Игорь",
            product="картофель",
            current_interest="картофель",
            volume="100 кг",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = FailingCatalogAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000006, None, "сколько стоит 100 кг картофеля?")
    )
    saved = await repo.get_client(2100000006)

    _assert_not_generic(result.text)
    assert "3000" in result.text
    assert "750" in result.text
    assert saved.needs_human is False
    assert "VEG-POTATO-001" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_horns_10_packs_quote_survives_generic_ai(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000008,
            name="Игорь",
            product="макароны",
            current_interest="рожки",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = FailingCatalogAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000008, None, "рожки 10 упаковок это сколько?")
    )
    saved = await repo.get_client(2100000008)

    _assert_not_generic(result.text)
    assert "3900" in result.text
    assert "390" in result.text
    assert saved.needs_human is False
    assert "PASTA-HORNS-001" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_fresh_cucumbers_20kg_quote_survives_generic_ai(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000009,
            name="Сергей",
            status="уточнение продукта",
            contact_skipped=True,
        )
    )
    ai = FailingCatalogAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000009, None, "огурцы 20 кг это сколько?")
    )
    saved = await repo.get_client(2100000009)

    _assert_not_generic(result.text)
    assert "2720" in result.text
    assert "680" in result.text
    assert "маринов" not in result.text.lower()
    assert saved.needs_human is False
    assert "VEG-CUCUMBER-001" in ai.catalog_calls[0]
    assert "CAN-PICKLES-001" not in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_explicit_potato_and_horns_composite_total(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000010,
            name="Игорь",
            product="картофель",
            current_interest="картофель",
            volume="100 кг",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = FailingCatalogAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(
            2100000010,
            None,
            "это не телефон, это объём. посчитайте итого: 4 сетки картофеля и 10 упаковок рожков",
        )
    )
    saved = await repo.get_client(2100000010)

    _assert_not_generic(result.text)
    assert "6900" in result.text
    assert "3000" in result.text
    assert "3900" in result.text
    assert saved.needs_human is False
    assert ai.catalog_calls
    assert "6900" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_intake_json_error_still_quotes_known_catalog_apples(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_qualified_apples())
    ai = FailingCatalogAI(analyses=[ValueError("JSONDecodeError")])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000001, None, "20 кг яблок")
    )
    saved = await repo.get_client(2100000001)

    _assert_not_generic(result.text)
    assert "1640" in result.text
    assert saved.needs_human is False
    assert saved.pending_manager_question is None


@pytest.mark.asyncio
async def test_none_ai_turn_still_quotes_live_catalog_hit(now):
    class DeadAI(FailingCatalogAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append(catalog_result)

        async def respond(self, profile, history, message):
            pass

    repo = InMemoryCRMRepository()
    await repo.save_client(_qualified_apples())
    ai = DeadAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000001, None, "два короба яблок сколько?")
    )

    _assert_not_generic(result.text)
    assert "1640" in result.text


@pytest.mark.asyncio
async def test_honest_unknown_stays_no_match(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000011,
            name="Марина",
            status="уточнение продукта",
            contact_skipped=True,
        )
    )
    ai = FailingCatalogAI(analyses=[IntakeAnalysis(intent="question", product="оливки")])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000011, None, "есть ли у вас оливки?")
    )
    saved = await repo.get_client(2100000011)

    assert "нет в каталоге" in result.text.lower()
    assert saved.catalog_no_match_query
    assert "1640" not in result.text
    assert "820" not in result.text


def test_grounding_rejects_flour_price_on_corn_and_stray_decimal():
    from stokozavr_bot.product_catalog import line_total_catalog_result
    from stokozavr_bot.service import is_unsafe_claim

    payload = line_total_catalog_result("кукуруза сладкая", "36 банок")
    assert payload is not None
    catalog, quote = payload
    assert "57.50" in quote.allowed_amounts
    assert "2070" in quote.allowed_amounts

    assert is_unsafe_claim("кукуруза 540 ₽", catalog) is True
    assert is_unsafe_claim("скидка за объём, итого 1800 ₽", catalog) is True
    assert is_unsafe_claim("36 банок — 2070 ₽, 57.50 ₽ за банку", catalog) is False
    assert is_unsafe_claim("банка 50 ₽", catalog) is True


@pytest.mark.asyncio
async def test_ai_cannot_attach_flour_price_to_corn(now):
    class MixAI(FailingCatalogAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append(catalog_result)
            return AiTurn(reply="Кукуруза сладкая — 540 ₽, как мука.")

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000012,
            name="Дмитрий",
            product="консервация",
            current_interest="кукуруза сладкая",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    result = await ConversationService(repo, MixAI(), clock=lambda: now).handle(
        IncomingMessage(2100000012, None, "кукуруза сладкая какая цена за 36 банок?")
    )

    _assert_not_generic(result.text)
    assert "540" not in result.text
    assert "2070" in result.text


@pytest.mark.asyncio
async def test_rice_unit_price_beats_stale_sugar_interest(now):
    class RiceAI(FailingCatalogAI):
        async def analyze_intake(self, profile, history, message):
            return IntakeAnalysis(intent="question", unit_price_request="кг", target_product=None)

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000013,
            name="Олег",
            product="бакалея",
            current_interest="сахар белый",
            volume="40 упаковок",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    result = await ConversationService(repo, RiceAI(), clock=lambda: now).handle(
        IncomingMessage(2100000013, None, "рис за кг")
    )

    _assert_not_generic(result.text)
    assert "85" in result.text
    assert "62" not in result.text


@pytest.mark.asyncio
async def test_legitimate_variant_word_is_not_wiped_by_competitor_filter(now):
    class VariantAI(FailingCatalogAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append(catalog_result)
            return AiTurn(
                reply="Есть два варианта огурцов: короткоплодные 680 ₽ и маринованные 860 ₽."
            )

        async def repair_response(self, *args):
            self.repairs += 1
            raise AssertionError("grounded cucumber answer must not be repaired")

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2100000014,
            name="Сергей",
            status="уточнение продукта",
            contact_skipped=True,
        )
    )
    result = await ConversationService(repo, VariantAI(), clock=lambda: now).handle(
        IncomingMessage(2100000014, None, "какие огурцы есть?")
    )

    assert "680" in result.text
    assert "860" in result.text
    assert "актуальную информацию уточню" not in result.text.lower()
    assert "вариант" in result.text.lower()
