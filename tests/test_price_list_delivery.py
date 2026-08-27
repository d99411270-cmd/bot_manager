from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import generated_price_list
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    EMAIL_QUESTION,
    PRICE_CONSULT_OFFERED_MARKER,
    PRICE_LIST_EMAIL_OFFER,
    PRODUCT_QUESTION,
    VOLUME_QUESTION,
    ConversationService,
)


class NoAI:
    async def analyze_intake(self, profile, history, message):
        raise AssertionError("explicit Telegram price-list requests must not call AI")

    async def respond(self, profile, history, message):
        raise AssertionError("explicit Telegram price-list requests must not call AI")


class AssortmentAI:
    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply="В каталоге есть категории продуктов.")


@pytest.fixture
def now():
    return datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "message_text",
    [
        "А прайс у вас есть?",
        "Прайс можно?",
        "прайс есть?",
        "прайс можно?",
    ],
)
@pytest.mark.asyncio
async def test_availability_price_list_question_asks_email_without_attachment(now, message_text):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(telegram_id=720, name="Сергей", phone="+799****4567")
    )
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(720, None, message_text))
    saved = await repository.get_client(720)

    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert result.text == PRICE_LIST_EMAIL_OFFER
    assert EMAIL_QUESTION not in result.text
    assert "почт" in result.text.lower()
    assert "я отправил" not in result.text.lower()
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None
    assert ConversationService.should_use_ai(saved, message_text) is False


@pytest.mark.parametrize("message_text", ["Прайс в чат", "Пришлите прайс сюда", "Прайс в Telegram"])
@pytest.mark.asyncio
async def test_explicit_price_list_in_chat_returns_generated_attachment_without_email(
    now, message_text
):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=700, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(700, None, message_text))

    assert result.text == "Отправляю актуальный прайс прямо сюда."
    assert result.attachment_content == generated_price_list()
    assert result.attachment_filename == "stokozavr-price-list.md"
    assert "почт" not in result.text.lower()
    assert result.attachment_content.count("SKU:") == 30
    assert "-ALT-" not in result.attachment_content
    assert "статус наличия" not in result.attachment_content.lower()
    assert "дата обновления" not in result.attachment_content.lower()
    assert "остат" not in result.attachment_content.lower()


@pytest.mark.parametrize("message_text", ["Давайте прайс", "Пришлите прайс", "Каталог"])
@pytest.mark.asyncio
async def test_plain_price_list_request_asks_email_without_attachment(now, message_text):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=701, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(701, None, message_text))
    saved = await repository.get_client(701)

    assert result.text == PRICE_LIST_EMAIL_OFFER
    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert EMAIL_QUESTION not in result.text
    assert "почт" in result.text.lower()
    assert "я отправил" not in result.text.lower()
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None


@pytest.mark.asyncio
async def test_chat_followup_after_email_question_returns_price_list_attachment(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(
            telegram_id=702,
            name="Анна",
            phone="+799****4567",
            price_list_requested=True,
        )
    )
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(702, None, "Прямо в чат можно?"))

    assert result.text == "Отправляю актуальный прайс прямо сюда."
    assert result.attachment_content == generated_price_list()
    assert result.attachment_filename == "stokozavr-price-list.md"
    assert "почт" not in result.text.lower()


@pytest.mark.asyncio
async def test_price_list_success_callback_records_send_and_clears_pending(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(
            telegram_id=703,
            name="Анна",
            phone="+799****4567",
            price_list_requested=True,
        )
    )
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    await service.mark_price_list_sent(703)
    saved = await repository.get_client(703)

    assert saved.price_list_requested is False
    assert saved.price_list_sent_at == now


@pytest.mark.asyncio
async def test_assortment_after_successful_price_send_does_not_repeat_offer(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(
            telegram_id=704,
            name="Анна",
            phone="+799****4567",
            price_list_sent_at=now,
        )
    )
    service = ConversationService(repository, AssortmentAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(704, None, "Какой у вас ассортимент?"))

    assert result.text == "В каталоге есть категории продуктов."
    assert "почт" not in result.text.lower() or "чат" not in result.text.lower()
    assert "выслать актуальный прайс" not in result.text.lower()
    assert result.attachment_content is None


@pytest.mark.asyncio
async def test_normal_assortment_does_not_offer_or_attach_price_list(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=707, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, AssortmentAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(707, None, "Какой у вас ассортимент?"))
    saved = await repository.get_client(707)

    assert "категории" in result.text.lower()
    assert "почт" in result.text.lower()
    assert "чат" in result.text.lower()
    assert result.text.count("?") <= 1
    assert VOLUME_QUESTION not in result.text
    assert result.attachment_content is None
    assert saved.price_list_requested is False
    assert saved.comment and PRICE_CONSULT_OFFERED_MARKER in saved.comment


@pytest.mark.parametrize(
    "message_text",
    ["прайс на почту", "Пришлите прайс на email", "вышлите прайс на почту"],
)
@pytest.mark.asyncio
async def test_explicit_email_price_list_request_asks_email_without_attachment(now, message_text):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=721, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(721, None, message_text))
    saved = await repository.get_client(721)

    assert result.text == PRICE_LIST_EMAIL_OFFER
    assert EMAIL_QUESTION not in result.text
    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None


@pytest.mark.asyncio
async def test_email_after_plain_price_request_is_saved_and_does_not_claim_send(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=705, name="Анна"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    first = await service.handle(IncomingMessage(705, None, "прайс на почту"))
    second = await service.handle(IncomingMessage(705, None, "anna@shop.ru"))
    saved = await repository.get_client(705)

    assert first.text == PRICE_LIST_EMAIL_OFFER
    assert EMAIL_QUESTION not in first.text
    assert saved.email == "anna@shop.ru"
    assert saved.price_list_requested is True
    assert PRODUCT_QUESTION not in second.text
    assert second.attachment_content is None
    assert second.text == "Ок, почту записал. Вам отправят актуальный прайс."
    assert "я отправил" not in second.text.lower()


@pytest.mark.asyncio
async def test_chat_here_after_email_keeps_pending_and_sends_attachment(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=708, name="Анна"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    await service.handle(IncomingMessage(708, None, "прайс на почту"))
    await service.handle(IncomingMessage(708, None, "anna@shop.ru"))
    result = await service.handle(IncomingMessage(708, None, "а можно прямо в чат?"))
    saved = await repository.get_client(708)

    assert result.attachment_filename == "stokozavr-price-list.md"
    assert result.attachment_content == generated_price_list()
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None


@pytest.mark.asyncio
async def test_chat_followup_recovers_pending_price_request_from_history(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=706, name="Анна", phone="+799****4567"))
    await repository.append_history(706, now, "Давайте прайс", PRICE_LIST_EMAIL_OFFER)
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(706, None, "Прямо в чат можно?"))

    assert result.attachment_content == generated_price_list()
    assert result.attachment_filename == "stokozavr-price-list.md"


@pytest.mark.parametrize(
    "message_text",
    [
        "на почту не надо, киньте в чат",
        "прайс на почту не надо, киньте в чат",
        "не на почту",
        "в чат",
        "сюда",
        "Почту не дам, киньте в чат",
    ],
)
@pytest.mark.asyncio
async def test_after_email_question_chat_or_not_email_sends_file_not_cancel(now, message_text):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(telegram_id=722, name="Наталья", phone="+799****4567")
    )
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    first = await service.handle(IncomingMessage(722, None, "прайс на почту"))
    result = await service.handle(IncomingMessage(722, None, message_text))
    saved = await repository.get_client(722)

    assert first.text == PRICE_LIST_EMAIL_OFFER
    assert result.text == "Отправляю актуальный прайс прямо сюда."
    assert result.attachment_content == generated_price_list()
    assert result.attachment_filename == "stokozavr-price-list.md"
    assert "без прайса" not in result.text.lower()
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None


@pytest.mark.parametrize(
    "message_text", ["прайс не надо", "прайс повторно не надо", "файл уже прислали"]
)
@pytest.mark.asyncio
async def test_price_list_refusal_cancels_pending_without_new_request(now, message_text):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(
            telegram_id=709,
            name="Анна",
            phone="+799****4567",
            price_list_requested=True,
        )
    )
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(709, None, message_text))
    saved = await repository.get_client(709)

    assert saved.price_list_requested is False
    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert PRODUCT_QUESTION not in result.text


@pytest.mark.asyncio
async def test_explicit_price_file_in_telegram_still_sends_attachment(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=710, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(710, None, "прайс файлом сюда в Telegram"))
    saved = await repository.get_client(710)

    assert result.attachment_filename == "stokozavr-price-list.md"
    assert result.attachment_content == generated_price_list()
    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None


@pytest.mark.asyncio
async def test_chat_channel_without_price_request_skips_contact_and_sends_no_file(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(
        ClientProfile(telegram_id=711, name="Анна", status="ожидает телефон")
    )
    service = ConversationService(repository, AssortmentAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(711, None, "почту не дам, пишите в этот чат"))
    saved = await repository.get_client(711)

    assert saved.contact_skipped is True
    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert saved.price_list_requested is False


class VegAI:
    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply="Из овощей: морковь, картофель, лук, огурцы. Что берёте?")

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply="Из овощей: морковь, картофель, лук, огурцы. Что берёте?")


class SkuAI:
    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply="Морковь: 410 ₽, мешок 10 кг. Какой объём берёте?")

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply="Морковь: 410 ₽, мешок 10 кг. Какой объём берёте?")


def _named_client(telegram_id: int) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Анна",
        phone="+799****4567",
        status="уточнение продукта",
    )


@pytest.mark.asyncio
async def test_first_product_turn_offers_email_or_chat_once(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_named_client(740))
    service = ConversationService(repository, VegAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(740, None, "какие овощи?"))
    saved = await repository.get_client(740)

    assert "морков" in result.text.lower() or "овощ" in result.text.lower()
    assert "почт" in result.text.lower()
    assert "чат" in result.text.lower()
    assert result.text.count("?") <= 1
    assert VOLUME_QUESTION not in result.text
    assert result.attachment_content is None
    assert saved.price_list_requested is False
    assert saved.comment and PRICE_CONSULT_OFFERED_MARKER in saved.comment


@pytest.mark.asyncio
async def test_second_product_turn_does_not_repeat_consult_fork(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_named_client(741))
    service = ConversationService(repository, VegAI(), clock=lambda: now)

    first = await service.handle(IncomingMessage(741, None, "какие овощи?"))
    second = await service.handle(IncomingMessage(741, None, "какие напитки есть?"))

    assert "почт" in first.text.lower() and "чат" in first.text.lower()
    assert not ("почт" in second.text.lower() and "чат" in second.text.lower())
    assert second.attachment_content is None
    assert first.text.count("?") <= 1
    assert second.text.count("?") <= 1


@pytest.mark.asyncio
async def test_bare_price_list_does_not_double_offer_consult(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_named_client(742))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(742, None, "прайс есть?"))
    saved = await repository.get_client(742)

    assert result.text == PRICE_LIST_EMAIL_OFFER
    assert result.attachment_content is None
    assert result.text.count("?") <= 1
    assert "чат" not in result.text.lower()
    assert saved.price_list_requested is True
    assert saved.comment and PRICE_CONSULT_OFFERED_MARKER in saved.comment


@pytest.mark.asyncio
async def test_first_sku_turn_answers_then_consult_not_volume(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_named_client(743))
    service = ConversationService(repository, SkuAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(743, None, "морковь есть?"))

    assert "морков" in result.text.lower()
    assert "410" in result.text
    assert "почт" in result.text.lower()
    assert "чат" in result.text.lower()
    assert VOLUME_QUESTION not in result.text
    assert "какой объём" not in result.text.lower()
    assert result.text.count("?") <= 1
    assert result.attachment_content is None


@pytest.mark.asyncio
async def test_consult_chat_choice_continues_catalog_without_file(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_named_client(744))
    service = ConversationService(repository, VegAI(), clock=lambda: now)

    await service.handle(IncomingMessage(744, None, "какие овощи?"))
    result = await service.handle(IncomingMessage(744, None, "подскажите тут"))
    saved = await repository.get_client(744)

    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert "без прайса" not in result.text.lower()
    assert saved.price_list_requested is False
    assert ConversationService.should_use_ai(saved, "в чате") is False


@pytest.mark.asyncio
async def test_consult_email_choice_asks_price_list_email(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(_named_client(745))
    service = ConversationService(repository, VegAI(), clock=lambda: now)

    await service.handle(IncomingMessage(745, None, "какие овощи?"))
    result = await service.handle(IncomingMessage(745, None, "на почту"))
    saved = await repository.get_client(745)

    assert result.text == PRICE_LIST_EMAIL_OFFER
    assert result.attachment_content is None
    assert saved.price_list_requested is True
