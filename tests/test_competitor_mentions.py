from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import ConversationService, limit_competitor_mentions


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


def test_unsolicited_named_competitor_is_stripped_primary_kept():
    profile = client()
    text = "Северная Капля 890 ₽ за 6 x 1 л, Росинка Поля 990 ₽."

    result = limit_competitor_mentions(profile, text)

    assert "890" in result
    assert "Северная Капля" in result
    assert "росинка" not in result.lower()
    assert "990" not in result
    assert "вернусь" not in result.lower()
    assert profile.competitor_mentions == 0


def test_named_competitor_pair_counts_once_when_allowed():
    profile = client()
    text = "Северная Капля 890 ₽ за 6 x 1 л, Росинка Поля 990 ₽."

    result = limit_competitor_mentions(profile, text, allowed=True)

    assert "890" in result
    assert "990" in result
    assert "Росинка Поля" in result
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


def test_third_visible_mention_keeps_primary_without_generic():
    profile = client()
    profile.competitor_mentions = 2
    profile.competitor_last_reply = False
    text = "Северная Капля 890 ₽ за 6 x 1 л, Росинка Поля 990 ₽."

    result = limit_competitor_mentions(profile, text, allowed=True)

    assert "890" in result
    assert "Северная Капля" in result
    assert "росинка" not in result.lower()
    assert "990" not in result
    assert "вернусь" not in result.lower()
    assert "уточню" not in result.lower()
    assert profile.competitor_mentions == 2


def test_two_competitor_answers_cannot_be_consecutive():
    profile = client()
    first_text = "Северная Капля 890 ₽, Росинка Поля 990 ₽."
    second_text = "Ещё раз: Росинка Поля 990 ₽ против Северной Капли 890 ₽."

    first = limit_competitor_mentions(profile, first_text, allowed=True)
    second = limit_competitor_mentions(profile, second_text, allowed=True)

    assert "Росинка Поля" in first
    assert profile.competitor_mentions == 1
    assert "росинка" not in second.lower()
    assert "990" not in second
    assert "890" in second
    assert "вернусь" not in second.lower()
    assert profile.competitor_mentions == 1
    assert profile.competitor_last_reply is False


def test_intervening_non_competitor_reply_clears_last_reply():
    profile = client()
    limit_competitor_mentions(profile, "Северная Капля 890 ₽, Росинка Поля 990 ₽.", allowed=True)
    assert profile.competitor_last_reply is True

    limit_competitor_mentions(profile, "Фасовка 6 x 1 л, Северная Капля 890 ₽.")

    assert profile.competitor_last_reply is False
    assert profile.competitor_mentions == 1


@pytest.mark.asyncio
async def test_primary_availability_names_linked_competitor_once(now):
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
    assert "росинка" in result.text.lower()
    assert "990" in result.text
    assert saved.competitor_mentions == 1
    assert saved.competitor_last_reply is True
    assert "SOK-APPLE-ALT-001" in catalog
    assert "росинка" in catalog.lower()


@pytest.mark.asyncio
async def test_thanks_on_known_sku_does_not_name_competitor(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000106))
    ai = ScriptedCatalogAI(["Пожалуйста, Олег. Если нужно — посчитаю объём."])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000106, None, "спасибо")
    )
    saved = await repo.get_client(2100000106)

    assert "росинка" not in result.text.lower()
    assert "990" not in result.text
    assert saved.competitor_mentions == 0
    assert saved.competitor_last_reply is False


@pytest.mark.asyncio
async def test_category_list_does_not_name_or_include_competitors(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000107))
    ai = ScriptedCatalogAI(
        ["Из напитков: сок Северная Капля 890 ₽, Росинка Поля 990 ₽, лимонад и морс."]
    )
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000107, None, "какие напитки есть?")
    )
    saved = await repo.get_client(2100000107)
    catalog = "\n".join(ai.catalog_calls)

    assert "росинка" not in result.text.lower()
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
    assert "росинка" in first.text.lower()
    assert saved.competitor_mentions == 1

    mid = await service.handle(IncomingMessage(2100000108, None, "какая фасовка?"))
    saved = await repo.get_client(2100000108)
    assert "росинка" not in mid.text.lower()
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
    pair = "Северная Капля 890 ₽ за 6 x 1 л, Росинка Поля 990 ₽ за 6 x 1 л."
    ai = ScriptedCatalogAI(
        [
            "Да, сок яблочный Северная Капля, 890 ₽ за 6 x 1 л.",
            "Фасовка 6 x 1 л, Северная Капля 890 ₽.",
            pair,
        ]
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(2100000109, None, "яблочный сок есть?"))
    saved = await repo.get_client(2100000109)
    assert "росинка" in first.text.lower()
    assert saved.competitor_mentions == 1

    await service.handle(IncomingMessage(2100000109, None, "какая фасовка?"))
    second = await service.handle(IncomingMessage(2100000109, None, "сравните"))
    saved = await repo.get_client(2100000109)
    assert "990" in second.text
    assert "росинка" in second.text.lower()
    assert saved.competitor_mentions == 2


@pytest.mark.asyncio
async def test_cheaper_may_keep_primary_without_naming_competitor(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000102))
    ai = ScriptedCatalogAI(["Самая доступная цена — Северная Капля 890 ₽ за 6 x 1 л."])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000102, None, "подешевле?")
    )
    saved = await repo.get_client(2100000102)

    assert "890" in result.text
    assert "росинка" not in result.text.lower()
    assert "990" not in result.text
    assert saved.competitor_mentions == 0


@pytest.mark.asyncio
async def test_compare_shows_pair_and_counts_one_mention(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000103))
    ai = ScriptedCatalogAI(["Северная Капля 890 ₽ за 6 x 1 л, Росинка Поля 990 ₽ за 6 x 1 л."])
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2100000103, None, "сравните")
    )
    saved = await repo.get_client(2100000103)

    assert "890" in result.text
    assert "990" in result.text
    assert "росинка" in result.text.lower()
    assert saved.competitor_mentions == 1


@pytest.mark.asyncio
async def test_compare_budget_second_and_third_mentions(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000104))
    pair = "Северная Капля 890 ₽ за 6 x 1 л, Росинка Поля 990 ₽ за 6 x 1 л."
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
    assert "росинка" not in mid.text.lower()
    assert saved.competitor_last_reply is False
    assert saved.competitor_mentions == 1

    second = await service.handle(IncomingMessage(2100000104, None, "сравните"))
    saved = await repo.get_client(2100000104)
    assert "990" in second.text
    assert saved.competitor_mentions == 2

    third = await service.handle(IncomingMessage(2100000104, None, "сравните"))
    saved = await repo.get_client(2100000104)
    assert "990" not in third.text
    assert "росинка" not in third.text.lower()
    assert "вернусь" not in third.text.lower()
    assert "уточню" not in third.text.lower() or "вернус" not in third.text.lower()
    assert "890" in third.text
    assert saved.competitor_mentions == 2


@pytest.mark.asyncio
async def test_catalog_dump_naming_competitor_increments_counter(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_juice_client(2100000105))
    result = await ConversationService(repo, DumpingCatalogAI(), clock=lambda: now).handle(
        IncomingMessage(2100000105, None, "сравните")
    )
    saved = await repo.get_client(2100000105)

    assert "росинка поля" in result.text.lower()
    assert "990" in result.text
    assert saved.competitor_mentions == 1
