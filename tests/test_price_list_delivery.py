from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import generated_price_list
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import EMAIL_QUESTION, ConversationService


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
async def test_plain_explicit_price_list_request_asks_email_without_attachment(now, message_text):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=701, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(701, None, message_text))
    saved = await repository.get_client(701)

    assert result.text == EMAIL_QUESTION
    assert result.attachment_content is None
    assert saved.price_list_requested is True


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
    assert "выслать актуальный прайс" not in result.text.lower()
    assert result.attachment_content is None


@pytest.mark.asyncio
async def test_normal_assortment_only_offers_price_list_without_attachment(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=707, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, AssortmentAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(707, None, "Какой у вас ассортимент?"))

    assert "выслать актуальный прайс" in result.text.lower()
    assert result.attachment_content is None


@pytest.mark.asyncio
async def test_email_after_plain_price_request_is_saved_and_does_not_claim_send(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=705, name="Анна"))
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    first = await service.handle(IncomingMessage(705, None, "Давайте прайс"))
    second = await service.handle(IncomingMessage(705, None, "anna@shop.ru"))
    saved = await repository.get_client(705)

    assert first.text == EMAIL_QUESTION
    assert saved.email == "anna@shop.ru"
    assert saved.price_list_requested is False
    assert second.attachment_content is None
    assert "отправ" not in second.text.lower()


@pytest.mark.asyncio
async def test_chat_followup_recovers_pending_price_request_from_history(now):
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=706, name="Анна", phone="+799****4567"))
    await repository.append_history(706, now, "Давайте прайс", EMAIL_QUESTION)
    service = ConversationService(repository, NoAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(706, None, "Прямо в чат можно?"))

    assert result.attachment_content == generated_price_list()
    assert result.attachment_filename == "stokozavr-price-list.md"
