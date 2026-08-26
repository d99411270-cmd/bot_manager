from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import ConversationService, normalize_phone


class FakeAI:
    def __init__(self, turns=None):
        self.turns = list(turns or [])
        self.calls = []

    async def respond(self, profile, history, message):
        self.calls.append((profile, history, message))
        return self.turns.pop(0)

    async def analyze_intake(self, profile, history, message):
        values = {
            "Анна": IntakeAnalysis("provide_data", name="Анна"),
            "123": IntakeAnalysis("provide_data", phone="123"),
            "оливки": IntakeAnalysis("provide_data", product="оливки"),
            "аджика": IntakeAnalysis("provide_data", product="аджика"),
            "20 коробок": IntakeAnalysis("provide_data", volume="20 коробок"),
        }
        return values.get(message, IntakeAnalysis("offtopic"))


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_first_contact_requires_name_phone_product_and_volume_before_ai(now):
    repo = InMemoryCRMRepository()
    ai = FakeAI([AiTurn(reply="Спасибо, всё зафиксировал.")])
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(10, "buyer", "/start"))
    assert "как я могу к вам обращаться" in first.text.lower()
    assert not ai.calls

    second = await service.handle(IncomingMessage(10, "buyer", "Анна"))
    assert "телефон" in second.text.lower()
    assert second.request_contact is False
    assert not ai.calls

    invalid = await service.handle(IncomingMessage(10, "buyer", "123"))
    assert "коррект" in invalid.text.lower()
    assert not ai.calls

    accepted = await service.handle(IncomingMessage(10, "buyer", "+7 999 123-45-67"))
    assert "продукц" in accepted.text.lower()
    assert not ai.calls

    product = await service.handle(IncomingMessage(10, "buyer", "оливки"))
    assert "объём" in product.text.lower()
    assert not ai.calls

    answer = await service.handle(IncomingMessage(10, "buyer", "20 коробок"))
    assert answer.text == "Спасибо, всё зафиксировал."
    assert len(ai.calls) == 1
    profile = await repo.get_client(10)
    assert profile.name == "Анна"
    assert profile.phone == normalize_phone("+7 999 123-45-67")
    assert profile.product == "оливки"
    assert profile.volume == "20 коробок"
    assert profile.status == "квалифицирован"


@pytest.mark.asyncio
async def test_product_and_volume_make_client_qualified_and_save_history(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=7, name="Иван", phone="+799****0000"))
    ai = FakeAI([AiTurn(reply="Спасибо, передаю менеджеру.")])
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(7, "ivan", "аджика"))
    result = await service.handle(IncomingMessage(7, "ivan", "20 коробок"))

    assert "объём" in first.text.lower()
    assert result.text == "Спасибо, передаю менеджеру."
    client = await repo.get_client(7)
    assert (client.product, client.volume, client.status) == (
        "аджика",
        "20 коробок",
        "квалифицирован",
    )
    history = await repo.get_history(7)
    assert history[-1].user_message == "20 коробок"
    assert history[-1].assistant_message == result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [
        "Цена 120 рублей за банку.",
        "Стоимость — пятьсот рублей.",
        "Этот товар точно есть в наличии.",
        "Оливки имеются.",
        "На складе доступно 500 коробок.",
    ],
)
async def test_unsafe_price_or_stock_claim_is_replaced(now, unsafe):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Пётр",
            phone="+799****0001",
            product="оливки",
            volume="10 коробок",
            status="квалифицирован",
        )
    )
    service = ConversationService(repo, FakeAI([AiTurn(reply=unsafe)]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Сколько стоит и есть ли на складе?"))

    assert result.text == "Я уточню этот вопрос и вернусь к вам."
    assert result.delay is False


@pytest.mark.asyncio
async def test_catalog_price_reply_is_sent(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Пётр",
            phone="+799****0001",
            product="фрукты",
            volume="10 ящиков",
            status="квалифицирован",
        )
    )
    reply = "Яблоки 820 ₽ за ящик, груши 880 ₽ за ящик. Какой объём нужен?"
    service = ConversationService(repo, FakeAI([AiTurn(reply=reply)]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Сколько стоят груши и яблоки?"))

    assert "груши 880 ₽" in result.text.lower()
    assert "яблоки 820 ₽" in result.text.lower()


@pytest.mark.asyncio
async def test_open_dialog_recovers_order_question_after_rejected_ai(now):
    class RecoveryAI(FakeAI):
        def __init__(self):
            super().__init__([AiTurn(reply="", needs_human=True)])
            self.recovery_calls = []

        async def open_dialog(self, profile, history, message, reason, catalog_result):
            self.recovery_calls.append((profile, history, message, reason, catalog_result))
            return AiTurn(reply="Да, заказать можно. Уточним объём макарон?", needs_human=False)

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            1,
            name="Пётр",
            phone="+799****0001",
            product="картофель",
            volume="100 кг",
            status="квалифицирован",
        )
    )
    ai = RecoveryAI()
    result = await ConversationService(repo, ai, clock=lambda: now).handle(
        IncomingMessage(1, None, "Заказать можно?")
    )

    assert "заказать можно" in result.text.lower()
    assert "сейчас не могу подтвердить" not in result.text.lower()
    assert len(ai.recovery_calls) == 1
    assert ai.recovery_calls[0][3] in {"needs_human", "unsafe_reply", "exception"}


@pytest.mark.asyncio
async def test_composite_order_keeps_confirmed_potato_calculation_and_asks_one_question(now):
    class CompositeAI(FakeAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            assert "Подтверждённый расчёт: 4 сетки" in catalog_result
            assert "VEG-POTATO-001" in catalog_result
            assert "макарон" in catalog_result.lower()
            return AiTurn(
                reply="Картофель: 4 сетки по 25 кг — 3000 ₽. По макаронам уточните, пожалуйста, какую фасовку выбрать?"
            )

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(2, name="Пётр", phone="+799****0002", status="уточнение продукта")
    )
    result = await ConversationService(repo, CompositeAI(), clock=lambda: now).handle(
        IncomingMessage(2, None, "Давайте картошку 100 кг, макарон тысяч на 10")
    )

    assert "3000 ₽" in result.text
    assert "макарон" in result.text.lower()
    assert result.text.count("?") == 1


@pytest.mark.asyncio
async def test_qualitative_stock_reply_is_sent(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Сергей",
            phone="+799****0001",
            product="овощи",
            status="уточнение объёма",
        )
    )
    reply = "Картофель, морковь и лук — много, огурцов мало. Какой объём нужен?"
    service = ConversationService(repo, FakeAI([AiTurn(reply=reply)]), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Какие овощи сейчас в наличии?"))

    assert "картофель" in result.text.lower()
    assert "много" in result.text.lower()
    assert "объём нужен" in result.text.lower()


@pytest.mark.asyncio
async def test_exact_stock_count_is_blocked(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=1,
            name="Сергей",
            phone="+799****0001",
            product="овощи",
            volume="10 сеток",
            status="квалифицирован",
        )
    )
    service = ConversationService(
        repo,
        FakeAI([AiTurn(reply="Картофеля 40 сеток на складе.")]),
        clock=lambda: now,
    )

    result = await service.handle(IncomingMessage(1, None, "Сколько картофеля есть?"))

    assert "40 сеток" not in result.text
    assert "уточню этот вопрос" in result.text.lower() or "750" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_mode", ["needs_human", "error"])
async def test_conservation_question_uses_catalog_when_ai_rejects_or_fails(now, ai_mode, caplog):
    class CatalogFailAI(FakeAI):
        async def respond(self, profile, history, message):
            if ai_mode == "error":
                raise RuntimeError("simulated DeepSeek failure")
            return AiTurn(reply="", needs_human=True)

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=11,
            name="Пётр",
            phone="+799****0011",
            status="уточнение продукта",
        )
    )
    service = ConversationService(repo, CatalogFailAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(11, None, "Какая консервация"))

    assert result.text != "Я уточню этот вопрос и вернусь к вам."
    assert "огородная банка" in result.text.lower()
    assert "720 ₽" in result.text
    assert "росинка" not in result.text.lower()
    expected_reason = "needs_human" if ai_mode == "needs_human" else "exception"
    assert f"reason={expected_reason}" in caplog.text


@pytest.mark.asyncio
async def test_catalog_repair_turns_needs_human_into_grounded_answer(now):
    class RepairAI(FakeAI):
        def __init__(self):
            super().__init__([AiTurn(reply="", needs_human=True)])
            self.repair_calls = 0

        async def repair_response(self, profile, history, message, reason, catalog_result):
            self.repair_calls += 1
            assert message == "Какая консервация"
            assert reason == "needs_human"
            assert "CAN-PEAS-001" in catalog_result
            return AiTurn(reply="Есть горошек зелёный от Огородной Банки за 720 ₽.")

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=12, name="Пётр", phone="+799****0012", status="уточнение продукта"
        )
    )
    ai = RepairAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(12, None, "Какая консервация"))

    assert "огородной банки" in result.text.lower()
    assert ai.repair_calls == 1


@pytest.mark.asyncio
async def test_invalid_repair_with_empty_catalog_cannot_invent_price(now, monkeypatch):
    class EmptyCatalogAI(FakeAI):
        def __init__(self):
            super().__init__([AiTurn(reply="", needs_human=True)])
            self.repair_calls = 0

        async def repair_response(self, profile, history, message, reason, catalog_result):
            self.repair_calls += 1
            assert catalog_result == "Каталог пуст."
            return AiTurn(reply="Консервация стоит 890 ₽.")

    monkeypatch.setattr("stokozavr_bot.service.search", lambda _query: "Каталог пуст.")
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=13, name="Пётр", phone="+799****0013", status="уточнение продукта"
        )
    )
    ai = EmptyCatalogAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(13, None, "Какая консервация"))

    assert "890" not in result.text
    assert ai.repair_calls == 1


@pytest.mark.asyncio
async def test_empty_catalog_search_sends_semantic_catalog_check_and_accepts_no_match(now):
    class CatalogCheckAI(FakeAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.calls.append((profile, history, message, catalog_result))
            assert "CATALOG_RESULT_EMPTY" in catalog_result
            assert "Доступные категории:" in catalog_result
            return AiTurn(reply="Подходящих товаров по этому запросу сейчас нет.")

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=66,
            name="Ирина",
            phone="+799****0066",
            product="молоко",
            status="уточнение продукта",
        )
    )
    ai = CatalogCheckAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(66, None, "Какое молоко есть?"))

    assert "подходящих товаров" in result.text.lower()
    assert "цена" not in result.text.lower()
    assert "в наличии" not in result.text.lower()
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_conserves_profile_passes_normalized_catalog_to_deepseek_without_volume_question(now):
    class CatalogAI(FakeAI):
        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.calls.append((profile, history, message, catalog_result))
            assert "CAN-PEAS-001" in catalog_result
            assert "CAN-TOMATO-001" in catalog_result
            return AiTurn(reply="В каталоге есть горошек зелёный и кукуруза сладкая.")

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=67,
            name="Ирина",
            phone="+799****0067",
            product="консервы",
            status="уточнение объёма",
        )
    )
    ai = CatalogAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(67, None, "Какие консервы?"))

    assert "горошек" in result.text.lower()
    assert "объём продукции вам необходим" not in result.text.lower()
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_price_followup_reuses_previous_vegetable_and_fruit_interest(now):
    class ContextAI(FakeAI):
        def __init__(self):
            super().__init__(
                [
                    AiTurn(reply="Есть картофель 750 ₽ за сетку и яблоки 820 ₽ за короб."),
                    AiTurn(reply="Картофель — 750 ₽ за сетку, яблоки — 820 ₽ за короб."),
                ]
            )
            self.catalog_calls = []

        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append((profile, history, message, catalog_result))
            return self.turns.pop(0)

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=68,
            name="Ирина",
            status="уточнение продукта",
        )
    )
    ai = ContextAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    first = await service.handle(IncomingMessage(68, None, "Какие овощи фруктв?"))
    result = await service.handle(IncomingMessage(68, None, "А какие цены?"))

    assert "750 ₽" in first.text
    assert "820 ₽" in first.text
    assert "750 ₽" in result.text
    assert "820 ₽" in result.text
    assert len(ai.catalog_calls) == 2
    assert "VEG-POTATO-001" in ai.catalog_calls[0][3]
    assert "FRU-APPLE-001" in ai.catalog_calls[0][3]
    assert "VEG-POTATO-001" in ai.catalog_calls[1][3]
    assert "FRU-APPLE-001" in ai.catalog_calls[1][3]
    assert ai.catalog_calls[1][2] == "А какие цены?"
    saved = await repo.get_client(68)
    assert saved.product is None
    assert set(saved.current_interest.split(" и ")) == {"овощи", "фрукты"}


async def test_ai_gets_profile_and_recent_history_only(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=2,
            name="Мария",
            phone="+799****0002",
            product="оливки",
            volume="10 коробок",
            status="квалифицирован",
        )
    )
    for i in range(15):
        await repo.append_history(2, now, f"q{i}", f"a{i}")
    ai = FakeAI([AiTurn(reply="Спасибо, уточню детали.")])
    service = ConversationService(repo, ai, history_limit=10, clock=lambda: now)

    await service.handle(IncomingMessage(2, "maria", "Когда будет ответ?"))

    profile, history, _ = ai.calls[0]
    assert profile.name == "Мария"
    assert len(history) == 10
    assert history[0].user_message == "q5"


@pytest.mark.asyncio
async def test_price_hesitation_keeps_known_volume_and_uses_catalog_grounded_ai(now):
    class PriceHesitationAI(FakeAI):
        def __init__(self):
            super().__init__(
                [
                    AiTurn(
                        reply="Горошек зелёный — 720 ₽ за упаковку. Подойдёт такая цена?",
                        needs_human=False,
                    )
                ]
            )
            self.catalog_calls = []

        async def respond_with_catalog(self, profile, history, message, catalog_result):
            self.catalog_calls.append((profile, history, message, catalog_result))
            return self.turns.pop(0)

    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=44,
            name="Дмитрий",
            phone="+799****0044",
            product="горошек",
            volume="36 банок",
            status="квалифицирован",
            contact_skipped=False,
        )
    )
    ai = PriceHesitationAI()
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(44, None, "Зависит от цены"))

    assert "720 ₽" in result.text
    assert "объём продукции вам необходим" not in result.text.lower()
    assert len(ai.catalog_calls) == 1
    profile, _history, message, catalog_result = ai.catalog_calls[0]
    assert profile.volume == "36 банок"
    assert message == "Зависит от цены"
    assert "CAN-PEAS-001" in catalog_result
    assert "36 банок" not in result.text or "объём" not in result.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phone", "wording"),
    [("+7 999 123 45 6", "не хватает"), ("+7 999 123 45 678", "лишняя")],
)
async def test_invalid_russian_phone_length_is_not_saved_and_gets_direction(now, phone, wording):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=300, name="Анна", status="ожидает телефон"))
    ai = FakeAI([AiTurn(reply="Кажется, в номере ошибка: пришлите ещё раз.")])
    service = ConversationService(repo, ai, clock=lambda: now)

    result = await service.handle(IncomingMessage(300, None, phone))
    saved = await repo.get_client(300)

    assert saved.phone is None
    assert wording in result.text.lower() or "ошибка" in result.text.lower()
    assert len(ai.calls) == 1
    assert "invalid_phone_length" in ai.calls[0][2]


@pytest.mark.asyncio
async def test_valid_russian_phone_is_saved_and_advances_to_product(now):
    repo = InMemoryCRMRepository()
    await repo.save_client(ClientProfile(telegram_id=301, name="Анна", status="ожидает телефон"))
    service = ConversationService(repo, FakeAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(301, None, "+7 999 123 45 67"))
    saved = await repo.get_client(301)

    assert saved.phone == "+79991234567"
    assert "какая продукция" in result.text.lower()
