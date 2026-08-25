from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import budget_quote
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    VOLUME_QUESTION,
    ConversationService,
    extract_budget,
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


@pytest.mark.parametrize("raw", ["на 10000 рублей", "бюджетом 10 000 ₽", "до 10000 руб."])
def test_extract_budget_accepts_common_russian_forms(raw):
    assert extract_budget(raw) == 10000


def test_budget_quote_uses_only_confirmed_primary_price():
    assert budget_quote("сок яблочный", 10000) == (11, 210, "890 ₽ за упаковку")


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
async def test_budget_is_saved_and_calculated_only_for_confirmed_catalog_price(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(telegram_id=4, name="Дмитрий", product="сок яблочный", contact_skipped=True)
    )
    service = ConversationService(
        repo,
        SemanticAI([IntakeAnalysis(intent="offtopic")], []),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(4, None, "на 10000 рублей"))
    saved = await repo.get_client(4)

    assert saved.budget == 10000
    assert "11 упаковок" in result.text
    assert "210 ₽" in result.text
    assert result.text.count("?") == 1


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

    assert "уточню этот вопрос" in result.text.lower()
    assert "640" not in result.text
    assert "пенз" not in result.text.lower()
