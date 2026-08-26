from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    FALLBACK,
    VOLUME_QUESTION,
    ConversationService,
    extract_volume,
)


class SemanticAI:
    def __init__(self, analyses=(), turns=()):
        self.analyses = list(analyses)
        self.turns = list(turns)

    async def analyze_intake(self, profile, history, message):
        if not self.analyses:
            return IntakeAnalysis(intent="offtopic")
        return self.analyses.pop(0)

    async def respond(self, profile, history, message):
        if not self.turns:
            raise RuntimeError("no turn")
        return self.turns.pop(0)


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw", ["200 кг", "200кг", "200 кг.", "36 банок", "20 упаковок", "полпаллеты", "50 литров"]
)
def test_extract_volume_from_short_replies(raw):
    assert extract_volume(raw)


@pytest.mark.parametrize("raw", ["36 банок", "20 упаковок", "полпаллеты", "50 литров"])
def test_extract_volume_preserves_explicit_packaging_amount(raw):
    value = extract_volume(raw)
    assert value is not None
    assert value.lower() == raw.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["200 кг", "200кг"])
async def test_numeric_volume_is_saved_and_not_reasked(now, raw):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Дмитрий",
            product="огурцы",
            status="уточнение объёма",
            contact_skipped=True,
        )
    )
    service = ConversationService(
        repo,
        SemanticAI(
            [IntakeAnalysis(intent="offtopic")],
            [AiTurn(reply="За 200 кг посчитаю. Самовывоз или доставка?")],
        ),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(1, None, raw))
    saved = await repo.get_client(1)

    assert saved.volume is not None
    assert "200" in saved.volume
    assert VOLUME_QUESTION not in result.text
    assert "какой объём продукции вам необходим" not in result.text.lower()


@pytest.mark.asyncio
async def test_thirty_six_cans_are_saved_and_next_reply_does_not_ask_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(telegram_id=3, name="Дмитрий", product="огурцы", contact_skipped=True)
    )
    service = ConversationService(
        repo,
        SemanticAI([IntakeAnalysis(intent="offtopic")], [AiTurn(reply="Принял объём.")]),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(3, None, "36 банок"))
    saved = await repo.get_client(3)

    assert saved.volume == "36 банок"
    assert "объём продукции вам необходим" not in result.text.lower()


@pytest.mark.asyncio
async def test_explicit_budget_is_saved_as_semantic_fact_without_code_calculation(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(telegram_id=4, name="Дмитрий", product="сок яблочный", contact_skipped=True)
    )
    service = ConversationService(
        repo,
        SemanticAI(
            [IntakeAnalysis(intent="question", budget=10000)],
            [AiTurn(reply="Уточню цену и вернусь с предложением.")],
        ),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(4, None, "на 10000 рублей"))
    saved = await repo.get_client(4)

    assert saved.budget == 10000
    assert result.text == FALLBACK
    assert "11 упаковок" not in result.text


@pytest.mark.asyncio
async def test_after_volume_broken_ai_still_quotes_catalog_price(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2,
            name="Дмитрий",
            product="огурцы",
            volume="200 кг",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    service = ConversationService(
        repo,
        SemanticAI(
            [IntakeAnalysis(intent="question")],
            [
                AiTurn(
                    reply="Огурцы по 9999 рублей, всегда в наличии. Когда? Сколько ещё?",
                    needs_human=True,
                )
            ],
        ),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(2, None, "200 кг"))

    assert "уточню этот вопрос" not in result.text.lower()
    assert "огурцы" in result.text.lower()
    assert "680 ₽" in result.text


class CatalogAI:
    def __init__(self, analyses=(), turns=()):
        self.analyses = list(analyses)
        self.turns = list(turns)
        self.catalog_calls = []

    async def analyze_intake(self, profile, history, message):
        if not self.analyses:
            return IntakeAnalysis(intent="question")
        result = self.analyses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def respond(self, profile, history, message):
        if not self.turns:
            raise RuntimeError("no turn")
        return self.turns.pop(0)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        if not self.turns:
            raise RuntimeError("no turn")
        return self.turns.pop(0)


@pytest.mark.asyncio
async def test_later_catalog_question_does_not_reask_known_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=10,
            name="Анна",
            product="яблоки",
            current_interest="яблоки",
            volume="20 кг",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = CatalogAI(
        [IntakeAnalysis(intent="question")],
        [AiTurn(reply="Яблоки сезонные сейчас в наличии. Какой объём вам нужен?")],
    )
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(10, None, "яблоки ещё в наличии?")
    )
    saved = await repo.get_client(10)

    assert saved.volume == "20 кг"
    assert "объём" not in result.text.lower()
    assert "нужен" not in result.text.lower() or "объём" not in result.text.lower()


@pytest.mark.asyncio
async def test_client_repeating_known_volume_gets_quote_not_stub(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=11,
            name="Анна",
            product="яблоки",
            current_interest="яблоки",
            volume="20 кг",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = CatalogAI(
        [IntakeAnalysis(intent="question")],
        [AiTurn(reply="Сейчас не могу подтвердить ответ по этому вопросу по каталогу.")],
    )
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(11, None, "я уже сказала 20 кг, не спрашивайте снова")
    )
    saved = await repo.get_client(11)

    assert saved.volume == "20 кг"
    assert "не могу подтвердить" not in result.text.lower()
    assert "20" in saved.volume


@pytest.mark.asyncio
async def test_packaging_fragment_does_not_overwrite_order_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=12,
            name="Олег",
            phone="+799****0012",
            product="кукуруза сладкая",
            current_interest="кукуруза сладкая",
            volume="40 упаковок",
            status="квалифицирован",
        )
    )
    ai = CatalogAI(
        [IntakeAnalysis(intent="question")],
        [AiTurn(reply="Да, фасовка 12 x 340 г, 690 ₽ за упаковку.")],
    )
    await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(
            12,
            None,
            "из консервации кукуруза сладкая Золотой Початок — 690 за упаковку 12х340г, так?",
        )
    )
    saved = await repo.get_client(12)

    assert saved.volume == "40 упаковок"


@pytest.mark.asyncio
async def test_skolko_price_question_still_uses_profile_volume(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=13,
            name="Анна",
            product="яблоки",
            current_interest="яблоки",
            volume="20 кг",
            status="квалифицирован",
            contact_skipped=True,
        )
    )
    ai = CatalogAI(
        [IntakeAnalysis(intent="question")],
        [AiTurn(reply="Яблоки сезонные: 820 ₽ за короб 10 кг.")],
    )
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(13, None, "так сколько будет за 20 кг яблок?")
    )
    saved = await repo.get_client(13)

    assert saved.volume == "20 кг"
    assert VOLUME_QUESTION not in result.text
    assert "какой объём" not in result.text.lower()


@pytest.mark.asyncio
async def test_opening_composite_volume_is_not_dropped(now):
    repo = InMemoryCRMRepository()
    ai = CatalogAI(
        [
            IntakeAnalysis(intent="provide_data", product="картофель", volume="100 кг"),
            IntakeAnalysis(intent="provide_data", volume="10 упаковок"),
        ],
        [
            AiTurn(reply="Зафиксировал картофель 100 кг."),
            AiTurn(reply="По макаронам 10 упаковок тоже учту."),
        ],
    )
    service = ConversationService(repo, ai, clock=lambda: now)

    await service.handle(IncomingMessage(14, None, "нужен картофель 100 кг и макароны"))
    first = await repo.get_client(14)
    await service.handle(IncomingMessage(14, None, "рожки 10 упаковок"))
    saved = await repo.get_client(14)

    assert first.volume is not None and "100" in first.volume
    assert saved.volume is not None and "100" in saved.volume
