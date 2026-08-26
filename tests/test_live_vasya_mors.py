from datetime import datetime, timezone

import pytest

from stokozavr_bot.catalog_quotes import QuoteFailure, line_total_quote
from stokozavr_bot.deepseek import SYSTEM_PROMPT, compose_system_prompt
from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import line_total_catalog_result, unit_price_quote
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import FALLBACK, ConversationService, is_unsafe_claim


@pytest.fixture
def now():
    return datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)


class CatalogSpeakingAI:
    """Live-shaped AI: DeepSeek speaks; code only grounds facts."""

    def __init__(self, analyses=(), reply=None):
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
        return AiTurn(reply=self.reply or FALLBACK)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        if self.reply is not None:
            return AiTurn(reply=self.reply)
        return AiTurn(reply=FALLBACK)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        self.repairs += 1
        return AiTurn(reply=self.reply or FALLBACK)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        self.open_dialogs += 1
        return AiTurn(reply=self.reply or FALLBACK)


def _vasya(
    telegram_id: int,
    *,
    product: str | None = None,
    current_interest: str | None = None,
    volume: str | None = None,
    status: str = "уточнение продукта",
) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Вася",
        phone="+799****0492",
        product=product,
        current_interest=current_interest,
        volume=volume,
        status=status,
        contact_skipped=True,
    )


def _assert_no_price_list_spam(text: str) -> None:
    lowered = text.lower()
    assert "выслать актуальный прайс" not in lowered
    assert "могу проконсультировать по товарам" not in lowered


def _assert_not_muzzle(text: str) -> None:
    lowered = text.lower()
    assert FALLBACK.lower() not in lowered
    assert "не могу подтвердить" not in lowered
    assert "уточню" not in lowered or "вернус" not in lowered


def test_prompts_do_not_order_price_list_offer():
    bundle = compose_system_prompt(SYSTEM_PROMPT).lower()

    assert "выслать актуальный прайс" not in bundle
    assert "могу проконсультировать по товарам" not in bundle
    assert FALLBACK not in SYSTEM_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Всё", "Всё что есть", "все"])
async def test_vsyo_does_not_prefix_price_list_offer(now, message):
    repo = InMemoryCRMRepository()
    await repo.save_client(_vasya(2100000492))
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="Всё")],
        reply="Работаем оптом по основным категориям. Что смотрите?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000492, None, message)
    )

    _assert_no_price_list_spam(result.text)
    _assert_not_muzzle(result.text)
    assert result.text == "Работаем оптом по основным категориям. Что смотрите?"
    assert ai.catalog_calls


@pytest.mark.asyncio
async def test_drinks_list_does_not_prefix_price_list_offer(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_vasya(2100000493))
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="напитки")],
        reply="Из напитков: сок, лимонад, вода, морс и чай. Что берёте?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000493, None, "Какие напитки есть?")
    )

    _assert_no_price_list_spam(result.text)
    _assert_not_muzzle(result.text)
    assert "морс" in result.text.lower()
    assert result.text.startswith("Из напитков")
    assert ai.catalog_calls


def test_mors_unit_price_is_106_67_per_liter():
    quote = unit_price_quote("морс", "л")

    assert quote is not None
    assert quote.unit_price == "106.67 ₽/л"
    assert quote.record.sku == "MORSE-CRANBERRY-001"
    assert quote.record.packaging == "6 x 1 л"


def test_mors_200_liters_quotes_nearest_packs_33_and_34():
    quote = line_total_quote("морс", "200 литров")

    assert not isinstance(quote, QuoteFailure)
    assert type(quote).__name__ == "NearestPackQuote"
    assert quote.record.sku == "MORSE-CRANBERRY-001"
    assert quote.lower is not None and quote.upper is not None
    assert quote.lower.pack_count == 33
    assert quote.lower.content_total == 198
    assert quote.lower.total == "21120 ₽"
    assert quote.upper.pack_count == 34
    assert quote.upper.content_total == 204
    assert quote.upper.total == "21760 ₽"
    assert {"21120", "21760", "640", "106.67"} <= set(quote.allowed_amounts)
    assert "21334" not in quote.allowed_amounts


def test_mors_200_liters_catalog_result_allows_nearest_pack_amounts():
    calculated = line_total_catalog_result("морс", "200 литров")

    assert calculated is not None
    rendered, quote = calculated
    assert type(quote).__name__ == "NearestPackQuote"
    assert "21120" in rendered
    assert "21760" in rendered
    assert "198" in rendered
    assert "204" in rendered
    assert (
        is_unsafe_claim(
            "Морс 200 л кратно 6 л: 33 упаковки = 198 л — 21120 ₽, либо 34 упаковки = 204 л — 21760 ₽.",
            rendered,
        )
        is False
    )
    assert is_unsafe_claim("Морс 200 л выйдет 21334 ₽.", rendered) is True


@pytest.mark.asyncio
async def test_mors_price_per_liter_is_106_67_not_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_vasya(2100000494, current_interest="морс клюквенный", product="морс"))
    ai = CatalogSpeakingAI(analyses=[ValueError("JSONDecodeError")])

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000494, None, "морс цена за литр")
    )
    saved = await repo.get_client(2100000494)

    _assert_not_muzzle(result.text)
    _assert_no_price_list_spam(result.text)
    assert "106.67" in result.text
    assert saved.volume != "1 л"
    assert "SKU:" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Литров 200", "200 литров"])
async def test_mors_200_liters_is_order_quoted_as_nearest_packs(now, message):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _vasya(
            2100000495,
            product="морс",
            current_interest="морс клюквенный",
            status="квалифицирован",
        )
    )
    ai = CatalogSpeakingAI(analyses=[ValueError("JSONDecodeError")])

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000495, None, message)
    )
    saved = await repo.get_client(2100000495)

    _assert_not_muzzle(result.text)
    _assert_no_price_list_spam(result.text)
    assert "21120" in result.text
    assert "21760" in result.text
    assert "198" in result.text
    assert "204" in result.text
    assert "SKU:" not in result.text
    assert saved.volume and "200" in saved.volume
    assert saved.needs_human is False
    assert ai.catalog_calls
    catalog = ai.catalog_calls[0]
    assert "21120" in catalog
    assert "21760" in catalog
    assert "Подтверждённый расчёт" in catalog


@pytest.mark.asyncio
async def test_per_liter_question_does_not_wipe_order_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _vasya(
            2100000496,
            product="морс",
            current_interest="морс клюквенный",
            volume="200 литров",
            status="квалифицирован",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[
            IntakeAnalysis(
                intent="question",
                product="морс",
                volume="1 л",
                unit_price_request="л",
                target_product="морс",
            )
        ]
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000496, None, "а цена за литр?")
    )
    saved = await repo.get_client(2100000496)

    _assert_not_muzzle(result.text)
    assert "106.67" in result.text
    assert saved.volume == "200 литров"


@pytest.mark.asyncio
async def test_known_sku_never_sends_cannot_confirm_or_fallback(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        _vasya(
            2100000497,
            product="морс",
            current_interest="морс клюквенный",
            status="квалифицирован",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="морс", volume="200 литров")],
        reply="Сейчас не могу подтвердить ответ по этому вопросу по каталогу.",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000497, None, "200 литров")
    )

    _assert_not_muzzle(result.text)
    assert "21120" in result.text
    assert "21760" in result.text


@pytest.mark.asyncio
async def test_deepseek_live_nearest_pack_reply_is_accepted_not_repaired(now):
    class LiveAI(CatalogSpeakingAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append(catalog_result)
            return AiTurn(
                reply=(
                    "200 л морса не кратно 6 л. Ближайшее: 33 упаковки = 198 л — 21120 ₽ "
                    "или 34 упаковки = 204 л — 21760 ₽."
                )
            )

        async def repair_response(self, *args):
            self.repairs += 1
            raise AssertionError("grounded nearest packs must not be repaired")

    repo = InMemoryCRMRepository()
    await repo.save_client(
        _vasya(
            2100000498,
            product="морс",
            current_interest="морс клюквенный",
            status="квалифицирован",
        )
    )
    ai = LiveAI(analyses=[IntakeAnalysis(intent="question", product="морс")])

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000498, None, "200 литров")
    )

    assert "21120" in result.text
    assert "21760" in result.text
    assert ai.repairs == 0
    _assert_no_price_list_spam(result.text)


def test_kept_goldens_carrot_rice_apples_and_mors_unit():
    from stokozavr_bot.catalog_quotes import line_total_quote as exact_quote
    from stokozavr_bot.product_catalog import unit_price_quote as unit

    assert unit("морковь", "кг").unit_price == "41 ₽/кг"
    assert unit("рис длиннозёрный", "кг").unit_price == "85 ₽/кг"
    apples = exact_quote("яблоки", "20 кг")
    assert apples.total == "1640 ₽"
    assert unit("морс", "л").unit_price == "106.67 ₽/л"
