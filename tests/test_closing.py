from datetime import datetime, timezone

import pytest

from stokozavr_bot.closing import CLOSE_ASK_TIME, CLOSE_NEED_PHONE, looks_like_ready_to_buy
from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.prompt_bundle import load_prompt_bundle
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.sales_state import infer_deal_stage
from stokozavr_bot.service import ConversationService


class SemanticAI:
    def __init__(self, analyses=(), turns=()):
        self.analyses = list(analyses)
        self.turns = list(turns)

    async def analyze_intake(self, profile, history, message):
        return self.analyses.pop(0)

    async def respond(self, profile, history, message):
        return self.turns.pop(0)


def analysis(intent="offtopic"):
    return IntakeAnalysis(intent=intent)


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


def test_ready_phrases():
    assert looks_like_ready_to_buy("Всё, беру")
    assert looks_like_ready_to_buy("Готов оформлять")
    assert not looks_like_ready_to_buy("сколько огурцы")


def test_quote_and_ready_stages():
    quoted = ClientProfile(1, status="получил предложение", product="огурцы", volume="20 кг")
    ready = ClientProfile(1, status="готов к заказу", product="огурцы", volume="20 кг")
    assert infer_deal_stage(quoted) == "quote_requested"
    assert infer_deal_stage(ready) == "ready_to_order"


def test_prompts_include_penza_promo_and_closing():
    bundle = load_prompt_bundle()
    lowered = bundle.lower()
    assert "50 000" in bundle or "50000" in bundle.replace(" ", "")
    assert "пенз" in lowered
    assert "созвон" in lowered


@pytest.mark.asyncio
async def test_ready_without_phone_insists_on_call(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Сергей",
            product="огурцы",
            volume="20 кг",
            status="получил предложение",
            contact_skipped=True,
        )
    )
    service = ConversationService(repo, SemanticAI([analysis("offtopic")]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Всё, беру"))
    saved = await repo.get_client(1)

    assert saved.status == "готов к заказу"
    assert "телефон" in result.text.lower()
    assert "процедур" in result.text.lower() or "звонок" in result.text.lower()
    assert result.text == CLOSE_NEED_PHONE


@pytest.mark.asyncio
async def test_ready_with_phone_asks_call_time(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2,
            name="Сергей",
            phone="+79990001122",
            product="огурцы",
            volume="20 кг",
            status="получил предложение",
        )
    )
    service = ConversationService(repo, SemanticAI([analysis("offtopic")]), clock=lambda: now)

    result = await service.handle(IncomingMessage(2, None, "Готов оформлять"))
    saved = await repo.get_client(2)

    assert saved.status == "готов к заказу"
    assert result.text == CLOSE_ASK_TIME
    assert "удобн" in result.text.lower()


@pytest.mark.asyncio
async def test_price_reply_marks_quote_stage(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=3,
            name="Сергей",
            phone="+79990001122",
            product="груши",
            volume="10 ящиков",
            status="квалифицирован",
        )
    )
    service = ConversationService(
        repo,
        SemanticAI(
            [analysis("question")],
            [
                AiTurn(
                    reply="Груши 880 ₽ за ящик. Если заказ от 50 000 ₽, доставка по Пензе бесплатная. Когда забрать?"
                )
            ],
        ),
        clock=lambda: now,
    )

    await service.handle(IncomingMessage(3, None, "Сколько груши?"))
    saved = await repo.get_client(3)
    assert saved.status == "получил предложение"
    assert infer_deal_stage(saved) == "quote_requested"
