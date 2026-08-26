from types import SimpleNamespace

import pytest
from aiogram.types import BufferedInputFile, ReplyKeyboardRemove

from stokozavr_bot.models import BotReply, ClientProfile
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import ConversationService
from stokozavr_bot.telegram import create_router, delay_range_for_text, reply_markup_for_response


def test_delay_ranges_follow_ai_reply_length():
    assert delay_range_for_text("короткий ответ") == (1.0, 2.0)
    assert delay_range_for_text("с" * 81) == (2.0, 4.0)
    assert delay_range_for_text("д" * 201) == (3.0, 6.0)


def test_every_response_removes_legacy_contact_keyboard():
    markup = reply_markup_for_response()

    assert isinstance(markup, ReplyKeyboardRemove)


def test_telegram_module_does_not_create_keyboard_button():
    import inspect

    from stokozavr_bot import telegram

    assert "KeyboardButton" not in inspect.getsource(telegram)


class FakeRepository:
    async def get_client(self, telegram_id):
        return ClientProfile(telegram_id)


class FakeService:
    @staticmethod
    def should_use_ai(profile, text):
        return False

    async def handle(self, incoming):
        return BotReply("Ответ")


class FakeState:
    async def set_state(self, state):
        self.state = state


class FakeMessage:
    def __init__(self, *, contact=None):
        self.from_user = SimpleNamespace(id=1, username="buyer")
        self.contact = contact
        self.text = "текст"
        self.caption = None
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contact",
    [None, SimpleNamespace(user_id=999, phone_number="+79991234567")],
)
async def test_adapter_removes_legacy_keyboard_on_normal_and_rejected_contact_replies(contact):
    repository = FakeRepository()
    callback = create_router(FakeService(), repository).message.handlers[0].callback
    message = FakeMessage(contact=contact)

    await callback(message, FakeState())

    assert isinstance(message.answers[0][1]["reply_markup"], ReplyKeyboardRemove)


class AttachmentService(FakeService):
    async def handle(self, incoming):
        return BotReply(
            "Отправляю актуальный прайс прямо сюда.",
            attachment_content="# price",
            attachment_filename="stokozavr-price-list.md",
        )


class TrackingAttachmentService(AttachmentService):
    def __init__(self):
        self.marked = []

    async def mark_price_list_sent(self, telegram_id):
        self.marked.append(telegram_id)


class DocumentMessage(FakeMessage):
    def __init__(self, text="текст"):
        super().__init__()
        self.text = text
        self.documents = []
        self.chat = SimpleNamespace(id=1)

    async def answer_document(self, document, **kwargs):
        self.documents.append((document, kwargs))


@pytest.mark.asyncio
async def test_router_sends_reply_attachment_as_buffered_telegram_document():
    repository = FakeRepository()
    callback = create_router(AttachmentService(), repository).message.handlers[0].callback
    message = DocumentMessage()

    await callback(message, FakeState())

    assert len(message.documents) == 1
    document, kwargs = message.documents[0]
    assert isinstance(document, BufferedInputFile)
    assert document.data == b"# price"
    assert document.filename == "stokozavr-price-list.md"
    assert kwargs["caption"] == "Отправляю актуальный прайс прямо сюда."
    assert kwargs["reply_markup"] == ReplyKeyboardRemove()


@pytest.mark.asyncio
async def test_router_marks_price_list_sent_only_after_document_success():
    service = TrackingAttachmentService()
    repository = FakeRepository()
    callback = create_router(service, repository).message.handlers[0].callback
    message = DocumentMessage()

    await callback(message, FakeState())

    assert service.marked == [1]


class FailingDocumentMessage(DocumentMessage):
    async def answer_document(self, document, **kwargs):
        raise RuntimeError("transport failure")


@pytest.mark.asyncio
async def test_router_does_not_mark_or_claim_success_when_document_send_fails():
    service = TrackingAttachmentService()
    repository = FakeRepository()
    callback = create_router(service, repository).message.handlers[0].callback
    message = FailingDocumentMessage()

    await callback(message, FakeState())

    assert service.marked == []
    assert message.answers == []


class DirectPriceListAI:
    async def analyze_intake(self, profile, history, message):
        raise AssertionError("explicit price-list requests must not call AI")

    async def respond(self, profile, history, message):
        raise AssertionError("explicit price-list requests must not call AI")


@pytest.mark.asyncio
async def test_real_service_records_price_list_success_after_router_document_send():
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=1, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, DirectPriceListAI())
    callback = create_router(service, repository).message.handlers[0].callback

    await callback(DocumentMessage(text="Прайс в чат"), FakeState())
    saved = await repository.get_client(1)

    assert saved.price_list_requested is False
    assert saved.price_list_sent_at is not None


@pytest.mark.asyncio
async def test_real_service_keeps_price_list_pending_when_router_document_send_fails():
    repository = InMemoryCRMRepository()
    await repository.save_client(ClientProfile(telegram_id=1, name="Анна", phone="+799****4567"))
    service = ConversationService(repository, DirectPriceListAI())
    callback = create_router(service, repository).message.handlers[0].callback

    await callback(FailingDocumentMessage(text="Прайс в чат"), FakeState())
    saved = await repository.get_client(1)

    assert saved.price_list_requested is True
    assert saved.price_list_sent_at is None
