from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import VOLUME_QUESTION, ConversationService

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
VOLUME_ONLY = "Какой объём вам нужен?"


class VolumeOnlyAI:
    """Scripted AI that only asks volume — the live screenshot failure mode."""

    def __init__(self, analyses=None):
        self.analyses = list(analyses or [])
        self.catalog_calls = []

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            return self.analyses.pop(0)
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=VOLUME_ONLY)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return AiTurn(reply=VOLUME_ONLY)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=VOLUME_ONLY)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=VOLUME_ONLY)


def _grechka_client(telegram_id: int, *, volume: str | None = None) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Олег",
        product="гречка ядрица",
        current_interest="гречка ядрица",
        contact_skipped=True,
        volume=volume,
        status="квалифицирован" if volume else "уточнение объёма",
    )


def _is_volume_only_reply(text: str) -> bool:
    stripped = text.strip()
    if stripped in {VOLUME_ONLY, VOLUME_QUESTION}:
        return True
    lowered = stripped.lower()
    return bool("объём" in lowered or "объем" in lowered) and not any(
        token in lowered for token in ("фасовк", "800", "марки нет", "золотое поле", "₽")
    )


@pytest.mark.asyncio
async def test_unknown_brand_then_packing_answered_despite_volume_only_ai():
    repo = InMemoryCRMRepository()
    await repo.save_client(_grechka_client(3100000001))
    ai = VolumeOnlyAI()
    service = ConversationService(repo, ai, clock=lambda: NOW)

    brand = await service.handle(IncomingMessage(3100000001, None, "крупяной берег у вас есть?"))
    saved = await repo.get_client(3100000001)

    assert not _is_volume_only_reply(brand.text)
    assert "марки нет" in brand.text.lower() or "не возим" in brand.text.lower()
    assert "золотое поле" in brand.text.lower()
    assert saved.catalog_no_match_query is None
    assert saved.current_interest and "гречк" in saved.current_interest.lower()

    packing = await service.handle(IncomingMessage(3100000001, None, "а какая фасовка?"))
    saved = await repo.get_client(3100000001)

    assert not _is_volume_only_reply(packing.text)
    assert "800" in packing.text
    assert "10" in packing.text
    assert saved.catalog_no_match_query is None


@pytest.mark.asyncio
async def test_unknown_brand_intake_does_not_sticky_no_match_current_grechka():
    repo = InMemoryCRMRepository()
    await repo.save_client(_grechka_client(3100000002))
    ai = VolumeOnlyAI(analyses=[IntakeAnalysis(intent="provide_data", product="крупяной берег")])
    service = ConversationService(repo, ai, clock=lambda: NOW)

    brand = await service.handle(IncomingMessage(3100000002, None, "крупяной берег у вас есть?"))
    saved = await repo.get_client(3100000002)

    assert not _is_volume_only_reply(brand.text)
    assert "марки нет" in brand.text.lower() or "не возим" in brand.text.lower()
    assert saved.catalog_no_match_query is None
    assert saved.current_interest and "гречк" in saved.current_interest.lower()
    assert saved.product and "гречк" in saved.product.lower()


@pytest.mark.asyncio
async def test_packing_followup_answers_even_when_ai_only_asks_volume():
    repo = InMemoryCRMRepository()
    await repo.save_client(_grechka_client(3100000003, volume="2 упаковки"))
    ai = VolumeOnlyAI()
    service = ConversationService(repo, ai, clock=lambda: NOW)

    packing = await service.handle(IncomingMessage(3100000003, None, "а какая фасовка?"))

    assert not _is_volume_only_reply(packing.text)
    assert "800" in packing.text
    assert "10" in packing.text
