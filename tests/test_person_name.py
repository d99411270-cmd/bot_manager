from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import PHONE_QUESTION, ConversationService, parse_person_name


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


class SemanticAI:
    def __init__(self, analyses=(), turns=()):
        self.analyses = list(analyses)
        self.turns = list(turns)
        self.catalog_calls = []

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            return self.analyses.pop(0)
        return IntakeAnalysis(intent="provide_data")

    async def respond(self, profile, history, message):
        if self.turns:
            return self.turns.pop(0)
        raise RuntimeError("no respond")

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        if self.turns:
            return self.turns.pop(0)
        raise RuntimeError("no respond")


def test_parse_full_name():
    assert parse_person_name("Сергей Иванов") == ("Сергей", "Иванов")
    assert parse_person_name("Меня зовут Анна Петрова") == ("Анна", "Петрова")
    assert parse_person_name("Дмитрий") == ("Дмитрий", None)
    assert parse_person_name("Огурцы много") is None


@pytest.mark.parametrize(
    ("raw", "official"),
    [
        ("Ванёк", "Иван"),
        ("Ванек", "Иван"),
        ("ваня", "Иван"),
        ("Ванька", "Иван"),
        ("Диман", "Дмитрий"),
        ("Димон", "Дмитрий"),
        ("дима", "Дмитрий"),
        ("Димка", "Дмитрий"),
        ("Сергей", "Сергей"),
    ],
)
def test_parse_person_name_maps_diminutives_to_official(raw, official):
    parsed = parse_person_name(raw)
    assert parsed is not None
    assert parsed[0] == official
    assert parsed[1] is None


@pytest.mark.parametrize(
    ("raw", "official"),
    [
        ("прив я ванек", "Иван"),
        ("привет я ванек", "Иван"),
        ("я ванек", "Иван"),
        ("здрасте я ванек", "Иван"),
        ("здравствуйте я ванек", "Иван"),
        ("добрый день я ванек", "Иван"),
        ("добрый я ванек", "Иван"),
        ("Прив я Ванёк", "Иван"),
    ],
)
def test_parse_person_name_strips_leading_greeting_then_maps_nickname(raw, official):
    parsed = parse_person_name(raw)
    assert parsed is not None
    assert parsed[0] == official
    assert parsed[1] is None


@pytest.mark.parametrize(
    "raw",
    ["прив", "привет", "здрасте", "здравствуйте", "добрый день", "добрый"],
)
def test_parse_person_name_rejects_bare_greetings(raw):
    assert parse_person_name(raw) is None


@pytest.mark.asyncio
async def test_bot_addresses_first_name_and_keeps_last_name(now):
    repo = InMemoryCRMRepository()
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Сергей Иванов"))
    saved = await repo.get_client(1)

    assert saved.name == "Сергей"
    assert saved.last_name == "Иванов"
    assert "Сергей" in result.text
    assert "Иванов" not in result.text


@pytest.mark.asyncio
async def test_short_greeting_priv_then_full_name_keeps_sergey_ivanov(now):
    repo = InMemoryCRMRepository()
    ai = SemanticAI(
        [
            IntakeAnalysis(intent="greeting"),
            IntakeAnalysis(intent="provide_data", name="Сергей Иванов"),
        ]
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    greeting = await service.handle(IncomingMessage(2, None, "Прив"))
    after_greeting = await repo.get_client(2)
    named = await service.handle(IncomingMessage(2, None, "Сергей Иванов"))
    saved = await repo.get_client(2)

    assert after_greeting.name is None
    assert after_greeting.last_name is None
    assert "очень приятно" not in greeting.text.lower()
    assert saved.name == "Сергей"
    assert saved.last_name == "Иванов"
    assert "Сергей" in named.text
    assert "Иванов" not in named.text


@pytest.mark.asyncio
async def test_catalog_token_is_not_a_name_even_if_parse_person_name_would_accept(now):
    assert parse_person_name("яблоки") == ("Яблоки", None)
    repo = InMemoryCRMRepository()
    ai = SemanticAI(
        [IntakeAnalysis(intent="provide_data")],
        [AiTurn(reply="Яблоки сезонные есть, фасовка короб 10 кг.")],
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(3, None, "яблоки"))
    saved = await repo.get_client(3)

    assert saved.name is None
    assert saved.last_name is None
    assert "очень приятно" not in result.text.lower()
    assert PHONE_QUESTION not in result.text
    assert "яблок" in result.text.lower()


@pytest.mark.asyncio
async def test_inflected_rice_is_not_captured_as_a_person_name(now):
    assert parse_person_name("риса") == ("Риса", None)
    repo = InMemoryCRMRepository()
    ai = SemanticAI(
        [IntakeAnalysis(intent="provide_data")],
        [AiTurn(reply="Рис длиннозёрный есть, фасовка 10 x 800 г.")],
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(31, None, "риса"))
    saved = await repo.get_client(31)

    assert saved.name is None
    assert saved.last_name is None
    assert "очень приятно" not in result.text.lower()
    assert PHONE_QUESTION not in result.text
    assert "рис" in result.text.lower()


@pytest.mark.asyncio
async def test_real_person_name_still_captured_after_catalog_normalization(now):
    repo = InMemoryCRMRepository()
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(32, None, "Анна"))
    saved = await repo.get_client(32)

    assert saved.name == "Анна"
    assert "Анна" in result.text
    assert PHONE_QUESTION in result.text


@pytest.mark.asyncio
async def test_catalog_token_in_product_stage_does_not_overwrite_name(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=4, name="Настя", status="уточнение продукта"))
    ai = SemanticAI(
        [IntakeAnalysis(intent="provide_data")],
        [AiTurn(reply="Яблоки сезонные есть, фасовка короб 10 кг.")],
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(4, None, "яблоки"))
    saved = await repo.get_client(4)

    assert saved.name == "Настя"
    assert saved.last_name is None
    assert "очень приятно" not in result.text.lower()
    assert PHONE_QUESTION not in result.text


@pytest.mark.asyncio
async def test_ready_to_buy_phrase_does_not_become_name_or_last_name(now):
    repo = InMemoryCRMRepository()
    ai = SemanticAI(
        [IntakeAnalysis(intent="provide_data")],
        [AiTurn(reply="Чтобы оформить заказ, нужен номер телефона.")],
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    await service.handle(IncomingMessage(5, None, "всё беру"))
    saved = await repo.get_client(5)

    assert saved.name is None
    assert saved.last_name is None


@pytest.mark.asyncio
async def test_ready_to_buy_does_not_write_last_name_when_name_already_set(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=6, name="Игорь", status="ожидает телефон"))
    service = ConversationService(
        repo,
        SemanticAI([IntakeAnalysis(intent="provide_data")]),
        clock=lambda: now,
    )

    await service.handle(IncomingMessage(6, None, "всё беру"))
    saved = await repo.get_client(6)

    assert saved.name == "Игорь"
    assert saved.last_name is None


@pytest.mark.asyncio
async def test_name_correction_replaces_first_name(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=7, name="Яблоки", status="ожидает телефон"))
    service = ConversationService(
        repo,
        SemanticAI([IntakeAnalysis(intent="correction", name="Настя")]),
        clock=lambda: now,
    )

    await service.handle(IncomingMessage(7, None, "меня Настя зовут, не Яблоки"))
    saved = await repo.get_client(7)

    assert saved.name == "Настя"
    assert saved.last_name is None


@pytest.mark.asyncio
async def test_vanek_is_stored_as_ivan(now):
    repo = InMemoryCRMRepository()
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(80, None, "Ванёк"))
    saved = await repo.get_client(80)

    assert saved is not None
    assert saved.name == "Иван"
    assert "Иван" in result.text
    assert "Ванёк" not in result.text
    assert PHONE_QUESTION in result.text


@pytest.mark.asyncio
async def test_diman_is_stored_as_dmitry(now):
    repo = InMemoryCRMRepository()
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(81, None, "Диман"))
    saved = await repo.get_client(81)

    assert saved is not None
    assert saved.name == "Дмитрий"
    assert "Дмитрий" in result.text
    assert "Диман" not in result.text


@pytest.mark.asyncio
async def test_sergey_is_unchanged_on_capture(now):
    repo = InMemoryCRMRepository()
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    await service.handle(IncomingMessage(82, None, "Сергей"))
    saved = await repo.get_client(82)

    assert saved is not None
    assert saved.name == "Сергей"


@pytest.mark.asyncio
async def test_priv_ya_vanek_first_turn_stores_ivan_not_vanek(now):
    repo = InMemoryCRMRepository()
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(83, None, "прив я ванек"))
    saved = await repo.get_client(83)

    assert saved is not None
    assert saved.name == "Иван"
    assert "Иван" in result.text
    assert "Ванек" not in result.text
    assert "Ванёк" not in result.text
    assert PHONE_QUESTION in result.text


@pytest.mark.asyncio
async def test_sveta_persists_on_name_slot_even_if_intent_is_greeting(now):
    repo = InMemoryCRMRepository()
    ai = SemanticAI([IntakeAnalysis(intent="greeting")])
    service = ConversationService(repo, ai, clock=lambda: now)

    await service.handle(IncomingMessage(90, None, "света"))
    saved = await repo.get_client(90)

    assert saved is not None
    assert saved.name == "Света"
