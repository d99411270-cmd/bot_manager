from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    PHONE_QUESTION,
    PRICE_CONSULT_FORK,
    PRICE_CONSULT_OFFERED_MARKER,
    VOLUME_QUESTION,
    ConversationService,
    _is_consult_chat_choice,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
VOLUME_ONLY = "Какой объём вам нужен?"
UNSAFE_PRICE = "Цена 99999 ₽ точно есть"
CONSULT_CHAT_UTTERANCE = "прайс на почту не надо, в чате подскажи"
BRAND_OWNERSHIP_UTTERANCE = "крупяной берег ваще ваш?"


class VolumeOnlyAI:
    def __init__(self, analyses=None):
        self.analyses = list(analyses or [])

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            return self.analyses.pop(0)
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=VOLUME_ONLY)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply=VOLUME_ONLY)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=VOLUME_ONLY)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=VOLUME_ONLY)


class RejectedAI:
    """Every provider turn is something `_reject_turn` kills."""

    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=UNSAFE_PRICE)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply=UNSAFE_PRICE)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply="Цена 88888 ₽ точно есть")

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply="", needs_human=True)


def _awaiting_phone_client(
    telegram_id: int,
    *,
    with_grechka: bool = True,
    comment: str | None = PRICE_CONSULT_OFFERED_MARKER,
) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Олег",
        phone=None,
        contact_skipped=False,
        status="ожидает телефон",
        product="гречка ядрица" if with_grechka else None,
        current_interest="гречка ядрица" if with_grechka else None,
        comment=comment,
    )


def _is_phone_questionnaire(text: str) -> bool:
    lowered = (text or "").lower()
    return PHONE_QUESTION in (text or "") or "номер телефона" in lowered


@pytest.mark.asyncio
async def test_email_reject_with_chat_is_consult_chat_not_phone_questionnaire():
    utterance = CONSULT_CHAT_UTTERANCE
    assert _is_consult_chat_choice(utterance) is True
    assert _is_consult_chat_choice("на почту не надо") is False

    repo = InMemoryCRMRepository()
    await repo.save_client(_awaiting_phone_client(3900000101))
    await repo.append_history(
        3900000101,
        NOW,
        "гречка есть какая фасовка и почём в магзах",
        "По гречке ядрице Золотое Поле 10 x 800 г, 730 ₽. " + PRICE_CONSULT_FORK,
    )
    service = ConversationService(repo, RejectedAI(), clock=lambda: NOW)

    result = await service.handle(IncomingMessage(3900000101, None, utterance))
    lowered = result.text.lower()

    assert not _is_phone_questionnaire(result.text)
    assert result.attachment_content is None
    assert result.attachment_filename is None
    assert (
        ("подскаж" in lowered and "чат" in lowered)
        or "730" in result.text
        or "фасовк" in lowered
        or "золотое поле" in lowered
    )


@pytest.mark.asyncio
async def test_brand_ownership_while_awaiting_phone_answers_not_ours():
    repo = InMemoryCRMRepository()
    await repo.save_client(_awaiting_phone_client(3900000102))
    ai = VolumeOnlyAI(analyses=[IntakeAnalysis(intent="provide_data", product="крупяной берег")])
    service = ConversationService(repo, ai, clock=lambda: NOW)

    brand = await service.handle(IncomingMessage(3900000102, None, BRAND_OWNERSHIP_UTTERANCE))
    saved = await repo.get_client(3900000102)
    lowered = brand.text.lower()

    assert not _is_phone_questionnaire(brand.text)
    assert brand.text.strip() not in {VOLUME_ONLY, VOLUME_QUESTION}
    assert any(token in lowered for token in ("марки нет", "не возим", "не наш"))
    assert "золотое поле" in lowered
    assert saved.catalog_no_match_query is None
    assert saved.current_interest and "гречк" in saved.current_interest.lower()


@pytest.mark.asyncio
async def test_rejected_ai_on_brand_or_consult_chat_does_not_ask_phone():
    repo = InMemoryCRMRepository()
    await repo.save_client(_awaiting_phone_client(3900000103, with_grechka=False, comment=None))
    service = ConversationService(repo, RejectedAI(), clock=lambda: NOW)

    consult = await service.handle(IncomingMessage(3900000103, None, CONSULT_CHAT_UTTERANCE))
    assert consult.text.strip() != PHONE_QUESTION
    assert not _is_phone_questionnaire(consult.text)

    repo = InMemoryCRMRepository()
    await repo.save_client(_awaiting_phone_client(3900000104, with_grechka=False, comment=None))
    service = ConversationService(repo, RejectedAI(), clock=lambda: NOW)

    brand = await service.handle(IncomingMessage(3900000104, None, BRAND_OWNERSHIP_UTTERANCE))
    assert brand.text.strip() != PHONE_QUESTION
    assert not _is_phone_questionnaire(brand.text)
