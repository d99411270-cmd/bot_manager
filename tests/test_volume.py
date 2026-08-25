from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import VOLUME_QUESTION, ConversationService, extract_volume


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


@pytest.mark.parametrize("raw", ["200 кг", "200кг", "200 кг."])
def test_extract_volume_from_short_replies(raw):
    assert extract_volume(raw)
    assert "200" in extract_volume(raw)


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
