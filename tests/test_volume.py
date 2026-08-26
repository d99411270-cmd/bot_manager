from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
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
    assert result.text == "Уточню цену и вернусь с предложением."
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
