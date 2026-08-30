from datetime import datetime, timezone

import pytest

from stokozavr_bot.closing import (
    CLOSE_ASK_TIME,
    CLOSE_NEED_PHONE,
    looks_like_call_request,
    looks_like_pickup_choice,
    looks_like_ready_to_buy,
)
from stokozavr_bot.handoff import InMemoryManagerHandoff
from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.prompt_bundle import load_prompt_bundle
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.sales_state import infer_deal_stage
from stokozavr_bot.service import (
    CATALOG_NO_MATCH_REPLY,
    EMAIL_QUESTION,
    FALLBACK,
    PRODUCT_QUESTION,
    ConversationService,
)


class SemanticAI:
    def __init__(self, analyses=(), turns=()):
        self.analyses = list(analyses)
        self.turns = list(turns)

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            return self.analyses.pop(0)
        return IntakeAnalysis(intent="offtopic")

    async def respond(self, profile, history, message):
        if self.turns:
            return self.turns.pop(0)
        return AiTurn(reply="Чем могу помочь по поставке?")


def analysis(intent="offtopic"):
    return IntakeAnalysis(intent=intent)


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


def _quoted(
    telegram_id: int,
    *,
    phone: str | None = "+79990001122",
    status: str = "получил предложение",
) -> ClientProfile:
    return ClientProfile(
        telegram_id=telegram_id,
        name="Сергей",
        phone=phone,
        product="огурцы",
        volume="20 кг",
        status=status,
    )


def _service(repo, now, handoff=None):
    return ConversationService(
        repo,
        SemanticAI(),
        clock=lambda: now,
        handoff=handoff,
    )


def _callish(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("созвон", "позвон", "звонк", "перезвон"))


def test_ready_phrases():
    assert looks_like_ready_to_buy("Всё, беру")
    assert looks_like_ready_to_buy("Готов оформлять")
    assert not looks_like_ready_to_buy("сколько огурцы")


def test_call_request_includes_noun_and_rejects_negated_pickup():
    assert looks_like_call_request("тогда звоните, самовывоз не надо")
    assert looks_like_call_request("нужен звонок менеджера")
    assert looks_like_call_request("звонок")
    assert looks_like_call_request("перезвоните завтра после 16:00")
    assert not looks_like_pickup_choice("тогда звоните, самовывоз не надо")
    assert not looks_like_pickup_choice("нет, самовывоз не нужен, звоните мне")
    assert not looks_like_pickup_choice("я на склад не приеду. нужен звонок менеджера, не визит")
    assert looks_like_pickup_choice("самовывоз")
    assert looks_like_pickup_choice("заберу сам")


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
            phone="+799****1122",
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
    assert saved.fulfillment_channel == "call"


@pytest.mark.asyncio
async def test_mobile_after_known_order_asks_time_not_product(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=29,
            name="Игорь",
            original_interests=["картофель продовольственный", "рожки классические"],
            current_interest="сок яблочный",
            status="ожидает телефон",
        )
    )
    service = _service(repo, now)

    result = await service.handle(IncomingMessage(29, None, "89271000006"))
    saved = await repo.get_client(29)

    assert saved.phone is not None
    assert saved.phone.startswith("+7")
    assert PRODUCT_QUESTION not in result.text
    assert EMAIL_QUESTION not in result.text
    assert "какая продукция" not in result.text.lower()
    assert result.text == CLOSE_ASK_TIME
    assert saved.fulfillment_channel == "call"
    assert saved.status == "готов к заказу"


@pytest.mark.asyncio
async def test_price_reply_marks_quote_stage(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=3,
            name="Сергей",
            phone="+799****1122",
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


@pytest.mark.asyncio
async def test_ready_with_mobile_then_afternoon_slot_stays_on_call(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(20))
    service = _service(repo, now)

    ready = await service.handle(IncomingMessage(20, None, "Готов оформлять"))
    result = await service.handle(IncomingMessage(20, None, "завтра после обеда, часов в 15"))
    saved = await repo.get_client(20)

    assert ready.text == CLOSE_ASK_TIME
    assert saved.status == "готов к заказу"
    assert saved.fulfillment_channel == "call"
    assert saved.requested_slot == "15:00"
    assert "15:00" in result.text
    assert result.text != FALLBACK
    assert "уточню" not in result.text.lower()
    assert PRODUCT_QUESTION not in result.text
    assert EMAIL_QUESTION not in result.text
    assert CATALOG_NO_MATCH_REPLY not in result.text
    assert saved.handoff_id is None


@pytest.mark.asyncio
async def test_pickup_after_quote_does_not_switch_to_call(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(21))
    service = _service(repo, now)

    pickup = await service.handle(IncomingMessage(21, None, "самовывоз"))
    visit = await service.handle(IncomingMessage(21, None, "завтра в 15"))
    saved = await repo.get_client(21)

    assert "аустрина" in pickup.text.lower()
    assert "137" in pickup.text
    assert not _callish(pickup.text)
    assert saved.fulfillment_channel == "pickup"
    assert saved.requested_slot == "15:00"
    assert "15:00" in visit.text
    assert not _callish(visit.text)
    assert saved.status == "готов к заказу"


@pytest.mark.asyncio
async def test_call_time_after_pickup_does_not_contradict_channel(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(22))
    service = _service(repo, now)

    await service.handle(IncomingMessage(22, None, "самовывоз"))
    result = await service.handle(IncomingMessage(22, None, "а позвоните завтра в 16:00"))
    saved = await repo.get_client(22)
    lowered = result.text.lower()

    assert saved.fulfillment_channel == "pickup"
    assert lowered.count("?") <= 1
    promised_call = any(token in lowered for token in ("позвон", "созвон", "перезвон"))
    promised_visit = "жду вас" in lowered or "ждём вас" in lowered or "ждем вас" in lowered
    assert not (promised_call and promised_visit)
    assert "самовывоз" in lowered or "канал" in lowered or "что удобнее" in lowered


@pytest.mark.asyncio
async def test_valid_mobile_then_call_slot_is_confirmed_not_product_or_email(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(23, phone=None, status="готов к заказу"))
    service = _service(repo, now)

    phone_reply = await service.handle(IncomingMessage(23, None, "+7 999 123-45-67"))
    slot_reply = await service.handle(IncomingMessage(23, None, "звоните завтра после 16:00"))
    saved = await repo.get_client(23)

    assert saved.phone == "+79991234567"
    assert PRODUCT_QUESTION not in phone_reply.text
    assert EMAIL_QUESTION not in phone_reply.text
    assert PRODUCT_QUESTION not in slot_reply.text
    assert EMAIL_QUESTION not in slot_reply.text
    assert "16:00" in slot_reply.text
    assert saved.requested_slot == "16:00"
    assert saved.fulfillment_channel == "call"
    assert saved.status == "готов к заказу"


@pytest.mark.asyncio
async def test_handoff_adapter_allows_call_promise_and_reads_back_id(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(24))
    handoff = InMemoryManagerHandoff()
    service = _service(repo, now, handoff=handoff)

    await service.handle(IncomingMessage(24, None, "Готов оформлять"))
    result = await service.handle(IncomingMessage(24, None, "завтра после обеда, часов в 15"))
    saved = await repo.get_client(24)

    assert len(handoff.created) == 1
    record = handoff.created[0]
    assert record["kind"] == "call"
    assert record["payload"]["slot"] == "15:00"
    assert saved.handoff_id == record["id"]
    assert saved.handoff_id == handoff.created[0]["id"]
    assert _callish(result.text)
    assert "15:00" in result.text


@pytest.mark.asyncio
async def test_without_handoff_adapter_time_is_undetermined_not_catalog_fallback(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(25))
    service = _service(repo, now)

    result = await service.handle(IncomingMessage(25, None, "звоните завтра после 16:00"))
    saved = await repo.get_client(25)

    assert "16:00" in result.text
    assert saved.requested_slot == "16:00"
    assert saved.handoff_id is None
    assert result.text != FALLBACK
    assert "уточню" not in result.text.lower()
    assert "вернус" not in result.text.lower()
    assert CATALOG_NO_MATCH_REPLY not in result.text
    assert PRODUCT_QUESTION not in result.text
    assert EMAIL_QUESTION not in result.text
    assert saved.pending_manager_question is None


@pytest.mark.asyncio
async def test_explicit_call_with_rejected_pickup_sets_call_not_visit(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(27, phone=None, status="получил предложение"))
    service = _service(repo, now)

    first = await service.handle(IncomingMessage(27, None, "тогда звоните, самовывоз не надо"))
    second = await service.handle(IncomingMessage(27, None, "я на склад не приеду. нужен звонок"))
    saved = await repo.get_client(27)

    assert saved.fulfillment_channel == "call"
    assert "аустрина" not in first.text.lower()
    assert "жду вас" not in first.text.lower()
    assert "жду вас" not in second.text.lower()
    assert "телефон" in first.text.lower() or "номер" in first.text.lower()
    assert first.text.count("?") <= 1
    assert second.text.count("?") <= 1
    assert PRODUCT_QUESTION not in first.text
    assert PRODUCT_QUESTION not in second.text


@pytest.mark.asyncio
async def test_call_channel_does_not_flip_back_to_pickup_on_not_coming(now):
    repo = InMemoryCRMRepository()
    profile = _quoted(28, phone="+799****1122", status="готов к заказу")
    profile.fulfillment_channel = "call"
    await repo.save_client(profile)
    service = _service(repo, now)

    result = await service.handle(
        IncomingMessage(28, None, "не приеду. не самовывоз. перезвоните мне завтра после 16:00")
    )
    saved = await repo.get_client(28)

    assert saved.fulfillment_channel == "call"
    assert saved.requested_slot == "16:00"
    assert "16:00" in result.text
    assert "жду вас" not in result.text.lower()
    assert "аустрина" not in result.text.lower()
    assert PRODUCT_QUESTION not in result.text
    assert result.text.count("?") <= 1


@pytest.mark.asyncio
async def test_pickup_question_allows_address_but_not_invented_tomorrow(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(_quoted(26))
    service = _service(repo, now)

    result = await service.handle(IncomingMessage(26, None, "самовывоз есть? завтра можно?"))
    saved = await repo.get_client(26)
    lowered = result.text.lower()

    assert "аустрина" in lowered
    assert "137" in result.text
    assert "корп" in lowered
    assert "завтра можно" not in lowered
    assert "завтра забрать" not in lowered
    assert saved.handoff_id is None
    assert not _callish(result.text)


class ThanksOnlyAI:
    async def analyze_intake(self, profile, history, message):
        return IntakeAnalysis(intent="offtopic")

    async def respond(self, profile, history, message):
        return AiTurn(reply="Пожалуйста, буду рад помочь с самовывозом.")

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        return AiTurn(reply="Пожалуйста, буду рад помочь с самовывозом.")

    async def repair_response(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply="Пожалуйста, буду рад помочь с самовывозом.")

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        return AiTurn(reply="Пожалуйста, буду рад помочь с самовывозом.")


@pytest.mark.asyncio
@pytest.mark.parametrize("thanks", ["всё, спасибо", "понял, спасибо", "спасибо"])
async def test_acknowledgment_after_quoted_order_does_not_repeat_line_total(now, thanks):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=910010,
            name="Андрей",
            phone="+799****1241",
            product="гречка ядрица",
            current_interest="гречка ядрица",
            volume="5 упаковок",
            status="получил предложение",
            fulfillment_channel="pickup",
        )
    )

    result = await ConversationService(repo, ThanksOnlyAI(), clock=lambda: now).handle(
        IncomingMessage(910010, None, thanks)
    )

    assert "3650" not in result.text
    assert "5 упаковок" not in result.text
    assert "гречка ядрица:" not in result.text.lower()
