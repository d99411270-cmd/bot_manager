from datetime import datetime, timezone

import pytest

from stokozavr_bot.catalog_quotes import line_total_quote
from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import (
    grounded_quote_reply,
    grounded_search_reply,
    search,
    unit_price_quote,
)
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    FALLBACK,
    ConversationService,
    _format_grounded_quote_reply,
    _format_line_total_reply,
)


class _AlwaysFallbackAI:
    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(
            intent="question",
            target_product="морс",
            unit_price_request="л",
        )

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply=FALLBACK)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=FALLBACK)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply=FALLBACK)


def test_carrot_50kg_line_total_does_not_repeat_za_meshok():
    quote = line_total_quote("морковь", "50 кг")
    reply = _format_line_total_reply(quote)

    assert "2050" in reply
    assert "5 мешков" in reply
    assert "10 кг" in reply
    assert "410 ₽ за мешок 10 кг" in reply
    assert "за мешок за мешок" not in reply
    assert quote.record.price == "410 ₽ за мешок"
    assert quote.record.packaging == "мешок 10 кг"


def test_carrot_nearest_pack_does_not_repeat_za_meshok():
    quote = line_total_quote("морковь", "45 кг")
    reply = _format_grounded_quote_reply(quote)

    assert "2050" in reply
    assert "410 ₽ за мешок 10 кг" in reply
    assert "за мешок за мешок" not in reply


def test_grounded_quote_carrot_does_not_repeat_za_meshok():
    reply = grounded_quote_reply("морковь")

    assert reply is not None
    assert "410 ₽ за мешок 10 кг" in reply
    assert "за мешок за мешок" not in reply


def test_grounded_search_carrot_does_not_repeat_za_meshok():
    reply = grounded_search_reply(search("морковь"))

    assert reply is not None
    assert "410 ₽ за мешок 10 кг" in reply
    assert "за мешок за мешок" not in reply


def test_potato_line_total_does_not_repeat_inflected_pack_unit():
    quote = line_total_quote("картофель", "100 кг")
    reply = _format_line_total_reply(quote)

    assert "3000" in reply
    assert "750 ₽ за сетку 25 кг" in reply
    assert "за сетку за сетка" not in reply


def test_mors_unit_price_quote_has_pack_and_price_without_double_za():
    quote = unit_price_quote("морс", "л")

    assert quote is not None
    assert quote.unit_price == "106.67 ₽/л"
    assert quote.record.price == "640 ₽ за упаковку"
    assert quote.record.packaging == "6 x 1 л"

    from stokozavr_bot.service import _format_unit_price_reply

    reply = _format_unit_price_reply(quote)
    assert "106.67 ₽/л" in reply
    assert "6 x 1 л" in reply
    assert "640 ₽ за упаковку" in reply
    assert reply.count("за упаковку") == 1
    assert "за упаковку за упаковку" not in reply


@pytest.mark.asyncio
async def test_mors_unit_recovery_does_not_repeat_za_upakovku():
    now = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=910001,
            name="Василий",
            phone="+799****0005",
            product="морс",
            current_interest="морс клюквенный",
            status="квалифицирован",
        )
    )

    result = await ConversationService(repo, _AlwaysFallbackAI(), clock=lambda: now).handle(
        IncomingMessage(910001, None, "а морс за литр")
    )

    assert "106.67" in result.text
    assert "6 x 1 л" in result.text
    assert "640 ₽ за упаковку" in result.text
    assert "за упаковку за упаковку" not in result.text
    assert result.text.count("за упаковку") == 1
