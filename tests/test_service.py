from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import ConversationService, normalize_phone


class FakeAI:
    def __init__(self, turns=None):
        self.turns = list(turns or [])
        self.calls = []

    async def respond(self, profile, history, message):
        self.calls.append((profile, history, message))
        return self.turns.pop(0)

    async def analyze_intake(self, profile, history, message):
        values = {
            "Анна": IntakeAnalysis("provide_data", name="Анна"),
            "123": IntakeAnalysis("provide_data", phone="123"),
            "оливки": IntakeAnalysis("provide_data", product="оливки"),
            "аджика": IntakeAnalysis("provide_data", product="аджика"),
            "20 коробок": IntakeAnalysis("provide_data", volume="20 коробок"),
        }
        return values.get(message, IntakeAnalysis("offtopic"))


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_first_contact_requires_name_phone_product_and_volume_before_ai(now):
    repo = InMemoryCRMRepository()
    ai = FakeAI([AiTurn(reply="Спасибо, всё зафиксировал.")])
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(10, "buyer", "/start"))
    assert "как я могу к вам обращаться" in first.text.lower()
    assert not ai.calls

    second = await service.handle(IncomingMessage(10, "buyer", "Анна"))
    assert "телефон" in second.text.lower()
    assert second.request_contact is False
    assert not ai.calls

    invalid = await service.handle(IncomingMessage(10, "buyer", "123"))
    assert "коррект" in invalid.text.lower()
    assert not ai.calls

    accepted = await service.handle(IncomingMessage(10, "buyer", "+7 999 123-45-67"))
    assert "продукц" in accepted.text.lower()
    assert not ai.calls

    product = await service.handle(IncomingMessage(10, "buyer", "оливки"))
    assert "объём" in product.text.lower()
    assert not ai.calls

    answer = await service.handle(IncomingMessage(10, "buyer", "20 коробок"))
    assert answer.text == "Спасибо, всё зафиксировал."
    assert len(ai.calls) == 1
    profile = await repo.get_client(10)
    assert profile.name == "Анна"
    assert profile.phone == normalize_phone("+7 999 123-45-67")
    assert profile.product == "оливки"
    assert profile.volume == "20 коробок"
    assert profile.status == "квалифицирован"


@pytest.mark.asyncio
async def test_product_and_volume_make_client_qualified_and_save_history(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=7, name="Иван", phone="+799****0000"))
    ai = FakeAI([AiTurn(reply="Спасибо, передаю менеджеру.")])
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(7, "ivan", "аджика"))
    result = await service.handle(IncomingMessage(7, "ivan", "20 коробок"))

    assert "объём" in first.text.lower()
    assert result.text == "Спасибо, передаю менеджеру."
    client = await repo.get_client(7)
    assert (client.product, client.volume, client.status) == (
        "аджика",
        "20 коробок",
        "квалифицирован",
    )
    history = await repo.get_history(7)
    assert history[-1].user_message == "20 коробок"
    assert history[-1].assistant_message == result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [
        "Цена 120 рублей за банку.",
        "Стоимость — пятьсот рублей.",
        "Этот товар точно есть в наличии.",
        "Оливки имеются.",
        "На складе доступно 500 коробок.",
    ],
)
async def test_unsafe_price_or_stock_claim_is_replaced(now, unsafe):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Пётр",
            phone="+799****0001",
            product="оливки",
            volume="10 коробок",
            status="квалифицирован",
        )
    )
    service = ConversationService(repo, FakeAI([AiTurn(reply=unsafe)]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Сколько стоит и есть ли на складе?"))

    assert result.text == "Я уточню этот вопрос и вернусь к вам."
    assert result.delay is False


@pytest.mark.asyncio
async def test_catalog_price_reply_is_sent(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Пётр",
            phone="+799****0001",
            product="фрукты",
            volume="10 ящиков",
            status="квалифицирован",
        )
    )
    reply = "Яблоки 850 ₽ за ящик, груши 1 100 ₽ за ящик. Какой объём нужен?"
    service = ConversationService(repo, FakeAI([AiTurn(reply=reply)]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Сколько стоят груши и яблоки?"))

    assert "уточню этот вопрос" in result.text.lower()


@pytest.mark.asyncio
async def test_qualitative_stock_reply_is_sent(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Сергей",
            phone="+799****0001",
            product="овощи",
            status="уточнение объёма",
        )
    )
    reply = "Картофель, морковь и лук — много, огурцов мало. Какой объём нужен?"
    service = ConversationService(repo, FakeAI([AiTurn(reply=reply)]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Какие овощи сейчас в наличии?"))

    assert "уточню этот вопрос" in result.text.lower()


@pytest.mark.asyncio
async def test_exact_stock_count_is_blocked(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Сергей",
            phone="+799****0001",
            product="овощи",
            volume="10 сеток",
            status="квалифицирован",
        )
    )
    service = ConversationService(
        repo,
        FakeAI([AiTurn(reply="Картофеля 40 сеток на складе.")]),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(1, None, "Сколько картофеля есть?"))

    assert "40 сеток" not in result.text
    assert "уточню этот вопрос" in result.text.lower() or "750" in result.text


@pytest.mark.asyncio
async def test_ai_gets_profile_and_recent_history_only(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2,
            name="Мария",
            phone="+799****0002",
            product="оливки",
            volume="10 коробок",
            status="квалифицирован",
        )
    )
    for i in range(15):
        await repo.append_history(2, now, f"q{i}", f"a{i}")
    ai = FakeAI([AiTurn(reply="Спасибо, уточню детали.")])
    service = ConversationService(repo, ai, history_limit=10, clock=lambda: now)

    await service.handle(IncomingMessage(2, "maria", "Когда будет ответ?"))

    profile, history, _ = ai.calls[0]
    assert profile.name == "Мария"
    assert len(history) == 10
    assert history[0].user_message == "q5"
