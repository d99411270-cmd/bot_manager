from types import SimpleNamespace

import pytest
from aiogram.types import ReplyKeyboardRemove

from stokozavr_bot.models import BotReply, ClientProfile
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
