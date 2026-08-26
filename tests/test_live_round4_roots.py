from datetime import datetime, timezone

import pytest

from stokozavr_bot.catalog_quotes import QuoteFailure, line_total_quote, parse_requested_quantity
from stokozavr_bot.closing import CLOSE_ASK_TIME
from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.product_catalog import line_total_catalog_result
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import (
    CATALOG_NO_MATCH_REPLY,
    FALLBACK,
    ConversationService,
    extract_volume,
    resolve_catalog_query,
)


@pytest.fixture
def now():
    return datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


class CatalogSpeakingAI:
    def __init__(self, analyses=(), reply=None):
        self.analyses = list(analyses)
        self.reply = reply
        self.catalog_calls = []
        self.repairs = 0
        self.open_dialogs = 0

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            result = self.analyses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return IntakeAnalysis(intent="question")

    async def respond(self, profile, history, message):
        return AiTurn(reply=self.reply or FALLBACK)

    async def respond_with_catalog(self, profile, history, message, catalog_result):
        self.catalog_calls.append(catalog_result)
        return AiTurn(reply=self.reply or FALLBACK)

    async def repair_response(self, profile, history, message, reason, catalog_result):
        self.repairs += 1
        return AiTurn(reply=self.reply or FALLBACK)

    async def open_dialog(self, profile, history, message, reason, catalog_result):
        self.open_dialogs += 1
        return AiTurn(reply=self.reply or FALLBACK)


OCCUPATION_QUESTION = (
    "Понимаю. Давайте подберём под вашу точку — вы магазин или кафе? "
    "Подскажите, чем занимаетесь, и я сориентирую по ассортименту."
)

CLIENT_TYPE_ANSWERS = (
    "магазин",
    "кафе",
    "ресторан",
    "столовая",
    "сеть",
    "у меня магазин",
    "из столовой",
)


def _assert_not_muzzle(text: str) -> None:
    lowered = text.lower()
    assert FALLBACK.lower() not in lowered
    assert "не могу подтвердить" not in lowered
    assert CATALOG_NO_MATCH_REPLY.lower() not in lowered


def test_shop_is_client_type_not_catalog_query():
    client = ClientProfile(
        telegram_id=2500000003,
        name="Игорь",
        phone="+790****2233",
        status="уточнение продукта",
    )

    query, owner = resolve_catalog_query(
        "магазин",
        IntakeAnalysis(intent="provide_data", product="магазин"),
        client,
        last_question=OCCUPATION_QUESTION,
    )

    assert query != "магазин"
    assert owner != "semantic"
    assert owner != "utterance"


@pytest.mark.parametrize("answer", CLIENT_TYPE_ANSWERS)
def test_occupation_answers_are_not_sku_queries(answer):
    client = ClientProfile(
        telegram_id=2500000100,
        name="Игорь",
        phone="+790****2233",
        status="уточнение продукта",
    )

    query, owner = resolve_catalog_query(
        answer,
        IntakeAnalysis(intent="provide_data", product=answer),
        client,
        last_question=OCCUPATION_QUESTION,
    )

    assert owner not in {"semantic", "utterance"}
    assert query not in {answer, answer.split()[-1]}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["магазин", "кафе", "ресторан", "столовая", "сеть"])
async def test_live_igor_shop_is_not_no_match(now, answer):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2500000003,
            name="Игорь",
            phone="+790****2233",
            status="уточнение продукта",
        )
    )
    await repo.append_history(
        2500000003,
        now,
        "не знаю",
        OCCUPATION_QUESTION,
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", product=answer)],
        reply="Тогда сориентирую по ходовым позициям для розницы. Что смотрите в первую очередь?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2500000003, None, answer)
    )
    saved = await repo.get_client(2500000003)

    _assert_not_muzzle(result.text)
    assert saved.catalog_no_match_query is None
    assert saved.product != answer
    assert "нет в каталоге" not in result.text.lower()
    assert not any("CATALOG_RESULT_EMPTY" in (call or "") for call in ai.catalog_calls)


def test_paru_banok_is_two_jars_not_invalid_quantity():
    parsed = parse_requested_quantity("пару банок")

    assert parsed is not None
    assert parsed.amount == 2
    assert parsed.unit == "банка"


@pytest.mark.parametrize("raw", ["пару банок", "пара банок", "две банки"])
def test_pair_of_jars_quotes_nearest_pickle_pack(raw):
    quote = line_total_quote("огурцы маринованные", raw)

    assert not isinstance(quote, QuoteFailure)
    assert type(quote).__name__ == "NearestPackQuote"
    assert quote.record.sku == "CAN-PICKLES-001"
    assert quote.upper is not None
    assert quote.upper.pack_count == 1
    assert quote.upper.total == "860 ₽"
    assert "860" in quote.allowed_amounts


def test_twelve_pickle_jars_catalog_result_exposes_1720():
    calculated = line_total_catalog_result("огурцы маринованные", "12 банок")

    assert calculated is not None
    rendered, quote = calculated
    assert quote.total == "1720 ₽"
    assert "1720" in rendered


@pytest.mark.asyncio
async def test_pavel_empty_ustroivaet_still_shows_both_mors_edges(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2500000004,
            name="Павел",
            current_interest="морс клюквенный",
            volume="литров 200",
            status="получил предложение",
            contact_skipped=True,
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", product="морс", volume="200 литров")],
        reply="Устраивает такой расчёт?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(
            2500000004,
            None,
            "вы цифру так и не назвали. какая сумма за 200 литров морса?",
        )
    )

    _assert_not_muzzle(result.text)
    assert "21120" in result.text
    assert "21760" in result.text
    assert result.text.strip() != "Устраивает такой расчёт?"
    assert ai.catalog_calls
    catalog = ai.catalog_calls[0]
    assert "21120" in catalog
    assert "21760" in catalog


@pytest.mark.asyncio
async def test_igor_twelve_jars_after_pickled_is_1720_not_stub(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2500000003,
            name="Игорь",
            phone="+790****2233",
            product="овощи",
            current_interest="овощи",
            status="получил предложение",
        )
    )
    await repo.append_history(
        2500000003,
        now,
        "огурцы в банках",
        "Игорь, по огурцам в банках есть позиция: огурцы маринованные "
        "«Бочковая История», фасовка 6 x 720 мл, 860 ₽ за упаковку.",
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", volume="12 банок")],
        reply="Сейчас не могу подтвердить ответ по этому вопросу по каталогу.",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2500000003, None, "12 банок")
    )
    saved = await repo.get_client(2500000003)

    _assert_not_muzzle(result.text)
    assert "1720" in result.text
    assert saved.volume and "12" in saved.volume
    assert saved.needs_human is False
    assert ai.catalog_calls
    assert "1720" in ai.catalog_calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["пару банок", "пара банок", "две банки"])
async def test_paru_banok_after_pickled_is_pack_quote_not_fresh_dump(now, message):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2500000003,
            name="Игорь",
            phone="+790****2233",
            product="овощи",
            current_interest="огурцы маринованные",
            status="получил предложение",
        )
    )
    await repo.append_history(
        2500000003,
        now,
        "огурцы в банках",
        "Игорь, огурцы маринованные «Бочковая История», 860 ₽ за упаковку 6 x 720 мл.",
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question", volume=message)],
        reply="Сейчас не могу подтвердить ответ по этому вопросу по каталогу.",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2500000003, None, message)
    )

    _assert_not_muzzle(result.text)
    lowered = result.text.lower()
    assert "860" in result.text
    assert "картофель" not in lowered
    assert "морковь" not in lowered
    assert "лук" not in lowered


@pytest.mark.asyncio
async def test_confirmed_slot_is_not_reasked_on_price_check(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2500000005,
            name="Наталья",
            phone="+790****0005",
            current_interest="картофель продовольственный",
            volume="100 кг",
            status="готов к заказу",
            fulfillment_channel="call",
            requested_slot="15:00",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="question")],
        reply="Отлично. Чтобы оформить заказ, нужен созвон с менеджером. Во сколько вам удобно?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(
            2500000005,
            None,
            "ок, только звонок, на склад не приеду. итого 4 сетки это 3000?",
        )
    )
    saved = await repo.get_client(2500000005)

    assert saved.requested_slot == "15:00"
    assert saved.fulfillment_channel == "call"
    assert "3000" in result.text
    assert "во сколько" not in result.text.lower()
    assert result.text != CLOSE_ASK_TIME


@pytest.mark.parametrize("raw", ["3 мешка", "бери 3 мешка"])
def test_extract_volume_reads_bags(raw):
    value = extract_volume(raw)

    assert value is not None
    assert "3" in value
    assert "мешк" in value.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["3 мешка", "бери 3 мешка", "да, бери 3 мешка"])
async def test_three_bags_persist_volume_without_inventing_delivery_channel(now, message):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2500000006,
            name="Дима",
            phone="+790****0006",
            product="морковь",
            current_interest="морковь",
            status="получил предложение",
        )
    )
    ai = CatalogSpeakingAI(
        analyses=[IntakeAnalysis(intent="provide_data", product="морковь")],
        reply="Дима, 3 мешка моркови — 1230 ₽. Устраивает такая цена?",
    )

    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(2500000006, None, message)
    )
    saved = await repo.get_client(2500000006)

    assert saved.volume is not None
    assert "3" in saved.volume
    assert "мешк" in saved.volume.lower()
    assert saved.fulfillment_channel in {None, "pickup", "call"}
    assert saved.fulfillment_channel != "доставка"
    assert "1230" in result.text or saved.volume
