from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import generated_price_list
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    CATALOG_NO_MATCH_REPLY,
    EMAIL_QUESTION,
    FULL_ASSORTMENT_EMAIL_OFFER,
    PRODUCT_QUESTION,
    ConversationService,
    wants_full_assortment,
)


class NoAI:
    async def analyze_intake(self, profile, history, message):
        raise AssertionError("full-assortment price offer must not call AI")

    async def respond(self, profile, history, message):
        raise AssertionError("full-assortment price offer must not call AI")


class VolumeHungryAI:
    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="provide_data", product=message, volume="большой объём")

    async def respond(self, profile, history, message):
        return AiTurn(reply="Вся — это большой объём. Подскажите категорию?")

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply="Вся — это большой объём. Подскажите категорию?")


class BrowseAI:
    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply="Из овощей: морковь, картофель, лук, огурцы. Что берёте?")

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply="Из овощей: морковь, картофель, лук, огурцы. Что берёте?")


@pytest.fixture
def now():
    return datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _dima(telegram_id: int = 679025492) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Дима",
        phone="+799****0492",
        status="уточнение продукта",
    )


@pytest.mark.parametrize(
    "text",
    [
        "Вся",
        "все",
        "всё",
        "Все продукты которые есть",
        "Все категории",
        "все продукты",
        "весь ассортимент",
        "что есть из продукции",
        "много всего",
        "всё что есть",
        "все что имеется",
        "что есть",
        "еда",
        "продукты питания",
        "продукты",
        "продовольствие",
        "питание",
        "интересует еда",
    ],
)
def test_wants_full_assortment_covers_live_phrases_and_paraphrases(text):
    assert wants_full_assortment(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "какие овощи?",
        "Какой у вас ассортимент?",
        "Какие напитки есть?",
        "все огурцы",
        "все понятно",
        "всё беру",
        "морковь",
        "прайс есть?",
        "молочные продукты",
        "готовая еда",
    ],
)
def test_wants_full_assortment_skips_browse_sku_and_explicit_price(text):
    assert wants_full_assortment(text) is False


@pytest.mark.parametrize(
    "message_text",
    ["Вся", "Все продукты которые есть", "Все категории"],
)
@pytest.mark.asyncio
async def test_live_full_assortment_offers_price_list_email_not_sku_or_volume(now, message_text):
    repository = InMemoryCRMRepository()
    await repository.save_client(_dima())
    service = ConversationService(repository, VolumeHungryAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(679025492, None, message_text))
    saved = await repository.get_client(679025492)

    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert CATALOG_NO_MATCH_REPLY not in result.text
    assert "нет в каталоге" not in result.text.lower()
    assert "больш" not in result.text.lower() or "объём" not in result.text.lower()
    assert "почт" in result.text.lower()
    assert "прайс" in result.text.lower()
    assert "ассортимент" in result.text.lower()
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None
    assert saved.catalog_no_match_query is None
    assert saved.volume is None
    assert saved.product not in {"Вся", "Все продукты которые есть", "Все категории"}
    assert ConversationService.should_use_ai(saved, message_text) is False


@pytest.mark.asyncio
async def test_full_assortment_email_is_saved_with_will_send_ack_not_i_sent(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_dima(730))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    first = await service.handle(IncomingMessage(730, None, "Все категории"))
    second = await service.handle(IncomingMessage(730, None, "dima@shop.ru"))
    saved = await repository.get_client(730)

    assert "почт" in first.text.lower()
    assert first.attachment_content is None
    assert saved.email == "dima@shop.ru"
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None
    assert second.attachment_content is None
    assert PRODUCT_QUESTION not in second.text
    assert second.text == "Ок, почту записал. Вам отправят актуальный прайс."
    assert "я отправил" not in second.text.lower()
    assert "я отправила" not in second.text.lower()


@pytest.mark.parametrize(
    "refusal",
    [
        "в чат",
        "сюда",
        "не на почту",
        "почту не дам, киньте в чат",
        "на почту не надо",
    ],
)
@pytest.mark.asyncio
async def test_full_assortment_email_refuse_sends_chat_attachment(now, refusal):
    repository = InMemoryCRMRepository()
    await repository.save_client(_dima(731))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    await service.handle(IncomingMessage(731, None, "Все продукты которые есть"))
    result = await service.handle(IncomingMessage(731, None, refusal))
    saved = await repository.get_client(731)

    assert result.text == "Отправляю актуальный прайс прямо сюда."
    assert result.attachment_content == generated_price_list()
    assert result.attachment_filename == "stokozavr-price-list.md"
    assert "без прайса" not in result.text.lower()
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None


@pytest.mark.asyncio
async def test_full_assortment_bare_email_refusal_sends_chat_attachment(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_dima(733))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    await service.handle(IncomingMessage(733, None, "Вся"))
    result = await service.handle(IncomingMessage(733, None, "почту не дам"))
    saved = await repository.get_client(733)

    assert result.text == "Отправляю актуальный прайс прямо сюда."
    assert result.attachment_content == generated_price_list()
    assert result.attachment_filename == "stokozavr-price-list.md"
    assert saved.price_list_requested is True


@pytest.mark.asyncio
async def test_short_category_browse_does_not_auto_offer_price_list(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_dima(732))
    service = ConversationService(repository, BrowseAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(732, None, "какие овощи?"))
    saved = await repository.get_client(732)

    assert "морков" in result.text.lower() or "овощ" in result.text.lower()
    assert "почт" in result.text.lower()
    assert "чат" in result.text.lower()
    assert EMAIL_QUESTION not in result.text
    assert result.attachment_content is None
    assert saved.price_list_requested is False
    assert result.text.count("?") <= 1


@pytest.mark.asyncio
async def test_live_food_hypernyms_offer_price_list_not_catalog_no_match(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(
            telegram_id=741,
            name="Дмитрий",
            phone="+799****0492",
            status="уточнение продукта",
        )
    )
    await repository.append_history(741, now, "Дмитрий", PRODUCT_QUESTION)
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    first = await service.handle(IncomingMessage(741, None, "продукты питания"))
    after_first = await repository.get_client(741)
    second = await service.handle(IncomingMessage(741, None, "еда"))
    saved = await repository.get_client(741)

    assert first.text == FULL_ASSORTMENT_EMAIL_OFFER
    assert second.text == FULL_ASSORTMENT_EMAIL_OFFER
    assert CATALOG_NO_MATCH_REPLY not in first.text
    assert CATALOG_NO_MATCH_REPLY not in second.text
    assert after_first.catalog_no_match_query is None
    assert saved.catalog_no_match_query is None
    assert after_first.product not in {"еда", "продукты питания"}
    assert saved.product not in {"еда", "продукты питания"}
    assert after_first.price_list_requested is True
    assert saved.price_list_requested is True
