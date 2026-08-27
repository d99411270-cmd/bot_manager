from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import ConversationService, limit_competitor_mentions

DEAD_COMPETITOR_BRANDS = (
    "росинка",
    "крупяной",
    "яблоневый",
    "белый колос",
    "источник луга",
)

RETAIL_JUICE_PAIR = (
    "Северная Капля 890 ₽ за 6 x 1 л, в обычных сетевых магазинах такая фасовка доходит до 990 ₽."
)
RETAIL_JUICE_SHORT = "Северная Капля 890 ₽, в сетевых магазинах такая фасовка доходит до 990 ₽."


def _has_dead_brand(text: str) -> bool:
    lowered = text.lower()
    return any(brand in lowered for brand in DEAD_COMPETITOR_BRANDS)


def _shows_retail_comparison(text: str) -> bool:
    lowered = text.lower()
    return bool("990" in text and ("сетев" in lowered or "розничн" in lowered))


def client() -> ClientProfile:
    return ClientProfile(telegram_id=700)


def _juice_client(telegram_id: int = 2100000100) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Олег",
        product="сок яблочный",
        current_interest="сок яблочный",
        status="квалифицирован",
        contact_skipped=True,
    )


class ScriptedCatalogAI:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.catalog_calls = []

    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=self.replies.pop(0))

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return AiTurn(reply=self.replies.pop(0))

    async def repair_response(self, profile, history, message, reason, catalog_result):
        raise AssertionError(f"grounded competitor reply must not be repaired: {reason}")


class DumpingCatalogAI:
    """Force catalog dump via recovery."""

    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply="Я уточню этот вопрос и вернусь к вам.")

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply="Я уточню этот вопрос и вернусь к вам.")

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply="Сейчас не могу подтвердить ответ по этому вопросу по каталогу.")

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply="Я уточню этот вопрос и вернусь к вам.")


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


def test_zero_mentions_stays_zero():
    profile = client()

    assert limit_competitor_mentions(profile, "Цена подтверждена.") == "Цена подтверждена."
    assert profile.competitor_mentions == 0


def test_compare_language_does_not_burn_primary_answer():
    profile = client()
    text = "Северная Капля 890 ₽ за 6 x 1 л, сравнение с рынком не требуется."

    result = limit_competitor_mentions(profile, text)

    assert "890" in result
    assert "Северная Капля" in result
    assert "6 x 1" in result
    assert "уточню" not in result.lower()
    assert "вернусь" not in result.lower()
    assert profile.competitor_mentions == 0


def test_unsolicited_retail_comparison_is_stripped_primary_kept():
    profile = client()

    result = limit_competitor_mentions(profile, RETAIL_JUICE_PAIR)

    assert "890" in result
    assert "Северная Капля" in result
    assert not _shows_retail_comparison(result)
    assert "990" not in result
    assert not _has_dead_brand(result)
    assert "вернусь" not in result.lower()
    assert profile.competitor_mentions == 0


def test_retail_comparison_counts_once_when_allowed():
    profile = client()

    result = limit_competitor_mentions(profile, RETAIL_JUICE_PAIR, allowed=True)

    assert "890" in result
    assert "990" in result
    assert _shows_retail_comparison(result)
    assert not _has_dead_brand(result)
    assert profile.competitor_mentions == 1
    assert profile.competitor_last_reply is True


def test_primary_only_cheaper_reply_does_not_increment():
    profile = client()
    text = "Самая доступная цена — Северная Капля 890 ₽ за 6 x 1 л."

    result = limit_competitor_mentions(profile, text, allowed=True)

    assert result == text
    assert profile.competitor_mentions == 0
    assert profile.competitor_last_reply is False


def test_packaging_grams_are_not_competitor_prices():
    profile = client()
    text = "Рис длиннозёрный есть, фасовка 10 x 800 г."

    result = limit_competitor_mentions(profile, text)

    assert result == text
    assert "рис" in result.lower()
    assert profile.competitor_mentions == 0


def test_potato_nets_are_not_retail_comparisons():
    profile = client()
    text = "Картофель: 4 сетки по 25 кг — 3000 ₽. Какой объём берёте?"

    result = limit_competitor_mentions(profile, text, allowed=True)

    assert result == text
    assert profile.competitor_mentions == 0


def test_third_visible_mention_keeps_primary_without_generic():
    profile = client()
    profile.competitor_mentions = 2
    profile.competitor_last_reply = False

    result = limit_competitor_mentions(profile, RETAIL_JUICE_PAIR, allowed=True)

    assert "890" in result
    assert "Северная Капля" in result
    assert "990" not in result
    assert not _shows_retail_comparison(result)
    assert "вернусь" not in result.lower()
    assert "уточню" not in result.lower()
    assert profile.competitor_mentions == 2


def test_two_retail_comparisons_cannot_be_consecutive():
    profile = client()
    first_text = RETAIL_JUICE_SHORT
    second_text = "Ещё раз: в сетевых магазинах доходит до 990 ₽ против Северной Капли 890 ₽."

    first = limit_competitor_mentions(profile, first_text, allowed=True)
    second = limit_competitor_mentions(profile, second_text, allowed=True)

    assert _shows_retail_comparison(first)
    assert profile.competitor_mentions == 1
    assert "990" not in second
    assert "890" in second
    assert not _shows_retail_comparison(second)
    assert "вернусь" not in second.lower()
    assert profile.competitor_mentions == 1
    assert profile.competitor_last_reply is False


def test_intervening_non_competitor_reply_clears_last_reply():
    profile = client()
    limit_competitor_mentions(profile, RETAIL_JUICE_SHORT, allowed=True)
    assert profile.competitor_last_reply is True

    limit_competitor_mentions(profile, "Фасовка 6 x 1 л, Северная Капля 890 ₽.")

    assert profile.competitor_last_reply is False
    assert profile.competitor_mentions == 1


@pytest.mark.asyncio
async def test_primary_availability_compares_retail_once_without_brand(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000101))
    ai = ScriptedCatalogAI(["Да, сок яблочный Северная Капля, 890 ₽ за 6 x 1 л."])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000101, None, "яблочный сок есть?")
    )
    saved = await repo.get_client(2100000101)
    catalog = "\n".join(ai.catalog_calls)

    assert "северная капля" in result.text.lower()
    assert "890" in result.text
    assert "6 x 1" in result.text.lower()
    assert "990" in result.text
    assert _shows_retail_comparison(result.text)
    assert not _has_dead_brand(result.text)
    assert "для сравнения" not in result.text.lower()
    assert saved.competitor_mentions == 1
    assert saved.competitor_last_reply is True
    assert "SOK-APPLE-ALT-001" in catalog
    assert not _has_dead_brand(catalog)


@pytest.mark.asyncio
async def test_thanks_on_known_sku_does_not_name_retail_comparison(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000106))
    ai = ScriptedCatalogAI(["Пожалуйста, Олег. Если нужно — посчитаю объём."])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000106, None, "спасибо")
    )
    saved = await repo.get_client(2100000106)

    assert not _shows_retail_comparison(result.text)
    assert "990" not in result.text
    assert saved.competitor_mentions == 0
    assert saved.competitor_last_reply is False


@pytest.mark.asyncio
async def test_category_list_does_not_include_retail_or_competitor_rows(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000107))
    ai = ScriptedCatalogAI(
        ["Из напитков: сок Северная Капля 890 ₽, в сетевых магазинах до 990 ₽, лимонад и морс."]
    )
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000107, None, "какие напитки есть?")
    )
    saved = await repo.get_client(2100000107)
    catalog = "\n".join(ai.catalog_calls)

    assert not _shows_retail_comparison(result.text)
    assert "990" not in result.text
    assert "-ALT-" not in catalog
    assert saved.competitor_mentions == 0


@pytest.mark.asyncio
async def test_second_unsolicited_primary_quote_keeps_silent(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000108))
    ai = ScriptedCatalogAI(
        [
            "Да, сок яблочный Северная Капля, 890 ₽ за 6 x 1 л.",
            "Фасовка 6 x 1 л, Северная Капля 890 ₽.",
            "Лимонад цитрусовый Лимонный Берег, 720 ₽ за 6 x 1.5 л.",
        ]
    )
    service = ConversationService(repo, ai, clock=lambda: now)
    first = await service.handle(IncomingMessage(2100000108, None, "яблочный сок есть?"))
    saved = await repo.get_client(2100000108)
    assert _shows_retail_comparison(first.text)
    assert saved.competitor_mentions == 1

    mid = await service.handle(IncomingMessage(2100000108, None, "какая фасовка?"))
    saved = await repo.get_client(2100000108)
    assert "990" not in mid.text
    assert saved.competitor_last_reply is False

    second = await service.handle(IncomingMessage(2100000108, None, "лимонад есть?"))
    saved = await repo.get_client(2100000108)
    assert "солнечный" not in second.text.lower()
    assert "810" not in second.text
    assert saved.competitor_mentions == 1


@pytest.mark.asyncio
async def test_compare_can_be_second_mention_after_unsolicited_quote(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000109))
    ai = ScriptedCatalogAI(
        [
            "Да, сок яблочный Северная Капля, 890 ₽ за 6 x 1 л.",
            "Фасовка 6 x 1 л, Северная Капля 890 ₽.",
            RETAIL_JUICE_PAIR,
        ]
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(2100000109, None, "яблочный сок есть?"))
    saved = await repo.get_client(2100000109)
    assert _shows_retail_comparison(first.text)
    assert saved.competitor_mentions == 1

    await service.handle(IncomingMessage(2100000109, None, "какая фасовка?"))
    second = await service.handle(IncomingMessage(2100000109, None, "сравните"))
    saved = await repo.get_client(2100000109)
    assert "990" in second.text
    assert _shows_retail_comparison(second.text)
    assert not _has_dead_brand(second.text)
    assert saved.competitor_mentions == 2


@pytest.mark.asyncio
async def test_cheaper_may_keep_primary_without_retail_comparison(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000102))
    ai = ScriptedCatalogAI(["Самая доступная цена — Северная Капля 890 ₽ за 6 x 1 л."])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000102, None, "подешевле?")
    )
    saved = await repo.get_client(2100000102)

    assert "890" in result.text
    assert not _shows_retail_comparison(result.text)
    assert "990" not in result.text
    assert saved.competitor_mentions == 0


@pytest.mark.asyncio
async def test_compare_shows_retail_pair_and_counts_one_mention(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000103))
    ai = ScriptedCatalogAI([RETAIL_JUICE_PAIR])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000103, None, "сравните")
    )
    saved = await repo.get_client(2100000103)

    assert "890" in result.text
    assert "990" in result.text
    assert _shows_retail_comparison(result.text)
    assert not _has_dead_brand(result.text)
    assert saved.competitor_mentions == 1


@pytest.mark.asyncio
async def test_compare_budget_second_and_third_mentions(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000104))
    pair = RETAIL_JUICE_PAIR
    ai = ScriptedCatalogAI(
        [
            pair,
            "Фасовка 6 x 1 л, Северная Капля 890 ₽.",
            pair,
            pair,
        ]
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(2100000104, None, "сравните"))
    saved = await repo.get_client(2100000104)
    assert "990" in first.text
    assert saved.competitor_mentions == 1
    assert saved.competitor_last_reply is True

    mid = await service.handle(IncomingMessage(2100000104, None, "какая фасовка?"))
    saved = await repo.get_client(2100000104)
    assert "990" not in mid.text
    assert saved.competitor_last_reply is False
    assert saved.competitor_mentions == 1

    second = await service.handle(IncomingMessage(2100000104, None, "сравните"))
    saved = await repo.get_client(2100000104)
    assert "990" in second.text
    assert saved.competitor_mentions == 2

    third = await service.handle(IncomingMessage(2100000104, None, "сравните"))
    saved = await repo.get_client(2100000104)
    assert "990" not in third.text
    assert not _shows_retail_comparison(third.text)
    assert "вернусь" not in third.text.lower()
    assert "уточню" not in third.text.lower() or "вернус" not in third.text.lower()
    assert "890" in third.text
    assert saved.competitor_mentions == 2


@pytest.mark.asyncio
async def test_catalog_dump_retail_comparison_increments_counter(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000105))
    result = await ConversationService(repo, DumpingCatalogAI(), clock=lambda: now).handle(
        IncomingMessage(2100000105, None, "сравните")
    )
    saved = await repo.get_client(2100000105)

    assert "990" in result.text
    assert not _has_dead_brand(result.text)
    assert saved.competitor_mentions == 1
