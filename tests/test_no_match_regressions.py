from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import CATALOG_NO_MATCH_REPLY, ConversationService, _ai_rejection_reason

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
UNKNOWN_PRODUCT = "наполнитель для кошачьего туалета турецкий"


class NoMatchAI:
    def __init__(self, analyses=(), replies=()):
        self.analyses = list(analyses)
        self.replies = list(replies)
        self.catalog_calls = []
        self.repair_calls = []
        self.open_dialog_calls = []

    async def analyze_intake(self, profile, history, message):
        result = self.analyses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def respond(self, profile, history, message):
        return self.replies.pop(0)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return self.replies.pop(0)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        self.repair_calls.append((reason, catalog_result))
        return self.replies.pop(0)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        self.open_dialog_calls.append((reason, catalog_result))
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_unknown_explicit_product_is_checked_before_volume_question():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1, name="Энрике", phone="+799****0001", status="уточнение продукта"
        )
    )
    ai = NoMatchAI(
        analyses=[IntakeAnalysis(intent="provide_data", product=UNKNOWN_PRODUCT)],
        replies=[AiTurn(reply=CATALOG_NO_MATCH_REPLY)],
    )

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(1, None, UNKNOWN_PRODUCT)
    )
    saved = await repo.get_client(1)

    assert "нет в каталоге" in result.text.lower()
    assert "объём" not in result.text.lower()
    assert saved.product is None
    assert saved.current_interest is None
    assert saved.status == "уточнение продукта"
    assert saved.pending_manager_question is None
    assert saved.needs_human is False
    assert len(ai.catalog_calls) == 1
    assert "CATALOG_RESULT_EMPTY" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_intake_exception_still_checks_unknown_explicit_product_and_all_recovery_paths_get_no_match():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2, name="Энрике", phone="+799****0002", status="уточнение продукта"
        )
    )
    generic = "Сейчас уточню информацию и вернусь к вам."
    ai = NoMatchAI(
        analyses=[ValueError("JSONDecodeError")],
        replies=[AiTurn(reply=generic), AiTurn(reply=generic), AiTurn(reply=generic)],
    )

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(2, None, UNKNOWN_PRODUCT)
    )
    saved = await repo.get_client(2)

    assert "нет в каталоге" in result.text.lower()
    assert "уточню" not in result.text.lower()
    assert "вернусь" not in result.text.lower()
    assert saved.product is None
    assert saved.current_interest is None
    assert saved.status == "уточнение продукта"
    assert saved.pending_manager_question is None
    assert saved.needs_human is False
    assert len(ai.catalog_calls) == 1
    assert len(ai.repair_calls) == 1
    assert len(ai.open_dialog_calls) == 1
    all_catalog = (
        ai.catalog_calls
        + [item[1] for item in ai.repair_calls]
        + [item[1] for item in ai.open_dialog_calls]
    )
    assert all("CATALOG_RESULT_EMPTY" in value for value in all_catalog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "reply"),
    [
        ("спасибо", "Понял, спасибо."),
        ("хорошо", "Хорошо, продолжим."),
    ],
)
async def test_intake_exception_acknowledgement_does_not_start_no_match(message, reply):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=20, name="Энрике", phone="+799****0020", status="уточнение продукта"
        )
    )
    ai = NoMatchAI(analyses=[ValueError("JSONDecodeError")], replies=[AiTurn(reply=reply)])

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(20, None, message)
    )
    saved = await repo.get_client(20)

    assert result.text == reply
    assert ai.catalog_calls == []
    assert saved.catalog_no_match_query is None
    assert saved.product is None
    assert saved.current_interest is None


@pytest.mark.asyncio
async def test_no_match_is_repeated_for_volume_followup_without_saving_fake_volume():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=3, name="Энрике", phone="+799****0003", status="уточнение продукта"
        )
    )
    ai = NoMatchAI(
        analyses=[
            IntakeAnalysis(intent="provide_data", product=UNKNOWN_PRODUCT),
            IntakeAnalysis(intent="offtopic"),
        ],
        replies=[AiTurn(reply=CATALOG_NO_MATCH_REPLY), AiTurn(reply=CATALOG_NO_MATCH_REPLY)],
    )
    service = ConversationService(repo, ai, clock=lambda: NOW)

    first = await service.handle(IncomingMessage(3, None, UNKNOWN_PRODUCT))
    second = await service.handle(IncomingMessage(3, None, "200гр"))
    saved = await repo.get_client(3)

    assert "нет в каталоге" in first.text.lower()
    assert "нет в каталоге" in second.text.lower()
    assert "объём" not in second.text.lower()
    assert saved.product is None
    assert saved.current_interest is None
    assert saved.volume is None
    assert saved.status == "уточнение продукта"
    assert saved.pending_manager_question is None
    assert saved.needs_human is False
    assert len(ai.catalog_calls) == 2
    assert all("CATALOG_RESULT_EMPTY" in value for value in ai.catalog_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", ["а этот?", "а этот", "этот?", "а эта?"])
async def test_referential_followup_after_unknown_keeps_same_entity_sticky(followup):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=21, name="Энрике", phone="+799****0021", status="уточнение продукта"
        )
    )
    ai = NoMatchAI(
        analyses=[
            IntakeAnalysis(intent="provide_data", product=UNKNOWN_PRODUCT),
            IntakeAnalysis(intent="question", product=followup),
        ],
        replies=[AiTurn(reply=CATALOG_NO_MATCH_REPLY), AiTurn(reply=CATALOG_NO_MATCH_REPLY)],
    )
    service = ConversationService(repo, ai, clock=lambda: NOW)

    first = await service.handle(IncomingMessage(21, None, UNKNOWN_PRODUCT))
    second = await service.handle(IncomingMessage(21, None, followup))
    saved = await repo.get_client(21)

    assert "нет в каталоге" in first.text.lower()
    assert "нет в каталоге" in second.text.lower()
    assert saved.catalog_no_match_query == UNKNOWN_PRODUCT
    assert saved.product is None
    assert saved.current_interest is None
    assert followup not in (saved.catalog_no_match_query or "")
    assert all(
        UNKNOWN_PRODUCT in value or "CATALOG_RESULT_EMPTY" in value for value in ai.catalog_calls
    )
    assert not any(
        followup == value.strip() or f"«{followup}»" in value for value in ai.catalog_calls
    )


@pytest.mark.asyncio
async def test_valid_catalog_product_still_advances_to_volume_and_accepts_grams_followup():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=4, name="Энрике", phone="+799****0004", status="уточнение продукта"
        )
    )
    ai = NoMatchAI(
        analyses=[
            IntakeAnalysis(intent="provide_data", product="макароны"),
            IntakeAnalysis("offtopic"),
        ],
        replies=[AiTurn(reply="Зафиксировал объём.")],
    )
    service = ConversationService(repo, ai, clock=lambda: NOW)

    product_reply = await service.handle(IncomingMessage(4, None, "макароны"))
    volume_reply = await service.handle(IncomingMessage(4, None, "200гр"))
    saved = await repo.get_client(4)

    assert "объём" in product_reply.text.lower()
    assert volume_reply.text == "Зафиксировал объём."
    assert saved.product == "макароны"
    assert saved.volume == "200гр"
    assert saved.status == "квалифицирован"
    assert len(ai.catalog_calls) == 1
    assert "SKU:" in ai.catalog_calls[0]


@pytest.mark.asyncio
async def test_existing_unknown_product_is_cleared_before_volume_followup():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=6,
            name="Энрике",
            phone="+799****0006",
            product=UNKNOWN_PRODUCT,
            current_interest=UNKNOWN_PRODUCT,
            status="уточнение объёма",
        )
    )
    ai = NoMatchAI(
        analyses=[IntakeAnalysis(intent="offtopic")],
        replies=[AiTurn(reply=CATALOG_NO_MATCH_REPLY)],
    )

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(6, None, "200гр")
    )
    saved = await repo.get_client(6)

    assert result.text == CATALOG_NO_MATCH_REPLY
    assert saved.product is None
    assert saved.current_interest is None
    assert saved.catalog_no_match_query == UNKNOWN_PRODUCT
    assert saved.volume is None
    assert saved.status == "уточнение продукта"
    assert saved.pending_manager_question is None


@pytest.mark.asyncio
async def test_no_match_rejects_ai_alternatives_and_uses_deterministic_reply():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=5, name="Энрике", phone="+799****0005", status="уточнение продукта"
        )
    )
    ai = NoMatchAI(
        analyses=[IntakeAnalysis(intent="provide_data", product=UNKNOWN_PRODUCT)],
        replies=[AiTurn(reply="Такого товара нет, могу предложить похожий вариант.")],
    )

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(5, None, UNKNOWN_PRODUCT)
    )

    assert result.text == CATALOG_NO_MATCH_REPLY
    assert "похож" not in result.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "intent", "product", "expected_sku"),
    [
        ("тогда макароны", "provide_data", None, "PASTA-HORNS-001"),
        ("тогда макароны", "question", None, "PASTA-SPAGHETTI-001"),
        ("тогда макароны", "offtopic", None, "PASTA-HORNS-001"),
        ("нужен рис", "provide_data", None, "GRC-RICE-001"),
        ("масло подсолнечное есть?", "question", None, "OIL-SUNFLOWER-001"),
    ],
)
async def test_new_category_after_unknown_clears_sticky_and_searches_utterance(
    message, intent, product, expected_sku
):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=7,
            name="Энрике",
            phone="+799****0007",
            status="уточнение продукта",
            catalog_no_match_query=UNKNOWN_PRODUCT,
        )
    )
    ai = NoMatchAI(
        analyses=[IntakeAnalysis(intent=intent, product=product)],
        replies=[AiTurn(reply="В каталоге есть подходящие позиции.")],
    )

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(7, None, message)
    )
    saved = await repo.get_client(7)

    assert result.text != CATALOG_NO_MATCH_REPLY
    assert "нет в каталоге" not in result.text.lower()
    assert saved.catalog_no_match_query is None
    assert ai.catalog_calls
    catalog = "\n".join(ai.catalog_calls)
    assert expected_sku in catalog
    assert UNKNOWN_PRODUCT not in catalog


@pytest.mark.asyncio
async def test_new_assortment_question_overrides_sticky_even_if_intake_omits_product():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=8,
            name="Энрике",
            phone="+799****0008",
            status="уточнение продукта",
            catalog_no_match_query=UNKNOWN_PRODUCT,
        )
    )
    ai = NoMatchAI(
        analyses=[IntakeAnalysis(intent="question", product=None)],
        replies=[AiTurn(reply="В каталоге есть овощи.")],
    )

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(8, None, "а какие овощи есть?")
    )
    saved = await repo.get_client(8)

    assert result.text != CATALOG_NO_MATCH_REPLY
    assert saved.catalog_no_match_query is None
    assert ai.catalog_calls
    catalog = "\n".join(ai.catalog_calls)
    assert "VEG-POTATO-001" in catalog
    assert UNKNOWN_PRODUCT not in catalog


@pytest.mark.asyncio
async def test_valid_product_not_saved_together_with_no_match_reply():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=9,
            name="Энрике",
            phone="+799****0009",
            status="уточнение продукта",
            catalog_no_match_query=UNKNOWN_PRODUCT,
        )
    )
    ai = NoMatchAI(
        analyses=[IntakeAnalysis(intent="provide_data", product="макароны")],
        replies=[AiTurn(reply="Зафиксировал макароны.")],
    )

    result = await ConversationService(repo, ai, clock=lambda: NOW).handle(
        IncomingMessage(9, None, "тогда макароны")
    )
    saved = await repo.get_client(9)

    assert not (saved.product == "макароны" and CATALOG_NO_MATCH_REPLY in (result.text or ""))
    assert result.text != CATALOG_NO_MATCH_REPLY
    assert saved.catalog_no_match_query is None
    assert "нет в каталоге" not in result.text.lower()


@pytest.mark.parametrize(
    "reply",
    [
        "Я уточню этот вопрос и вернусь к вам.",
        "Актуальную информацию уточню и вернусь к вам",
        "Сейчас уточню информацию и вернусь к вам.",
        "Я сейчас уточню и потом вернусь с ответом.",
    ],
)
def test_extended_generic_promises_are_rejected(reply):
    assert _ai_rejection_reason(AiTurn(reply=reply)) == "invalid_reply"
