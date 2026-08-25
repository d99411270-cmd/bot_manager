from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    EMAIL_QUESTION,
    FALLBACK,
    PRODUCT_QUESTION,
    ConversationService,
    normalize_email,
)


class SemanticAI:
    def __init__(self, analyses=(), turns=()):
        self.analyses = list(analyses)
        self.turns = list(turns)

    async def analyze_intake(self, profile, history, message):
        return self.analyses.pop(0)

    async def respond(self, profile, history, message):
        return self.turns.pop(0)


def analysis(intent, **kwargs):
    return IntakeAnalysis(intent=intent, **kwargs)


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def test_normalize_email():
    assert normalize_email("Пишите на Shop@Mail.ru пожалуйста") == "shop@mail.ru"
    assert normalize_email("телефон не дам") is None


@pytest.mark.asyncio
async def test_phone_refusal_asks_email_only(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=1, name="Анна", status="ожидает телефон"))
    service = ConversationService(repo, SemanticAI([analysis("refusal")]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Телефон не дам"))
    saved = await repo.get_client(1)

    assert saved.phone is None
    assert saved.email is None
    assert saved.contact_skipped is False
    assert saved.status == "ожидает почту"
    assert result.text == EMAIL_QUESTION
    assert PRODUCT_QUESTION not in result.text


@pytest.mark.asyncio
async def test_email_after_phone_refusal_is_saved(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=2, name="Анна", status="ожидает почту"))
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(2, None, "anna@shop.ru"))
    saved = await repo.get_client(2)

    assert saved.email == "anna@shop.ru"
    assert saved.phone is None
    assert saved.status == "уточнение продукта"
    assert PRODUCT_QUESTION in result.text


@pytest.mark.asyncio
async def test_email_refusal_continues_without_contact(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=3, name="Анна", status="ожидает почту"))
    service = ConversationService(repo, SemanticAI([analysis("refusal")]), clock=lambda: now)

    result = await service.handle(IncomingMessage(3, None, "Почту тоже не дам"))
    saved = await repo.get_client(3)

    assert saved.phone is None
    assert saved.email is None
    assert saved.contact_skipped is True
    assert saved.status == "уточнение продукта"
    assert "без контакта" in result.text.lower()
    assert PRODUCT_QUESTION in result.text


@pytest.mark.asyncio
async def test_giving_phone_does_not_ask_email(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=4, name="Анна", status="ожидает телефон"))
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(4, None, "+7 999 111-22-33"))
    saved = await repo.get_client(4)

    assert saved.phone == "+79991112233"
    assert saved.email is None
    assert saved.contact_skipped is False
    assert "почт" not in result.text.lower()
    assert PRODUCT_QUESTION in result.text


@pytest.mark.asyncio
async def test_write_here_skips_phone_and_does_not_ask_again(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=5, name="Сергей", status="ожидает телефон"))
    service = ConversationService(
        repo,
        SemanticAI(
            [analysis("question")],
            [AiTurn(reply="Хорошо, продолжим здесь. Какая продукция нужна?")],
        ),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(5, None, "Зачем вам номер? пишите сюда просто"))
    saved = await repo.get_client(5)

    assert saved.contact_skipped is True
    assert saved.phone is None
    assert "номер телефона" not in result.text.lower()
    assert "почт" not in result.text.lower()


@pytest.mark.asyncio
async def test_cucumber_volume_question_does_not_ask_phone(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(telegram_id=6, name="Сергей", status="уточнение продукта", product="овощи")
    )
    service = ConversationService(
        repo,
        SemanticAI(
            [analysis("question")],
            [AiTurn(reply="20 кг огурцов наберём, их сейчас мало. Когда забрать?")],
        ),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(6, None, "Огурцов кг 20 будет?"))

    assert "номер телефона" not in result.text.lower()
    assert "огурц" in result.text.lower() or result.text == FALLBACK
