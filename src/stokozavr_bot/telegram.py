from __future__ import annotations

import asyncio
import random

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.utils.chat_action import ChatActionSender

from .models import IncomingMessage
from .repositories import CRMRepository
from .service import ConversationService


class ClientJourney(StatesGroup):
    waiting_name = State()
    waiting_phone = State()
    ai_dialog = State()


def delay_range_for_text(text: str) -> tuple[float, float]:
    length = len(text.strip())
    if length <= 80:
        return (1.0, 2.0)
    if length <= 200:
        return (2.0, 4.0)
    return (3.0, 6.0)


def reply_markup_for_response() -> ReplyKeyboardRemove:
    """Remove the obsolete contact keyboard on every response."""
    return ReplyKeyboardRemove()


def create_router(
    service: ConversationService,
    repository: CRMRepository,
    *,
    delay_probability: float = 0.7,
) -> Router:
    if not 0 <= delay_probability <= 1:
        raise ValueError("delay_probability должен быть от 0 до 1")
    router = Router(name="client_journey")

    async def process(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        if message.contact and message.contact.user_id not in (None, message.from_user.id):
            await message.answer(
                "Пожалуйста, отправьте свой номер телефона.",
                reply_markup=reply_markup_for_response(),
            )
            return
        text = message.text or message.caption or ""
        contact_phone = message.contact.phone_number if message.contact else None
        incoming = IncomingMessage(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            text=text or contact_phone or "",
            contact_phone=contact_phone,
        )

        profile_before = await repository.get_client(message.from_user.id)
        if service.should_use_ai(profile_before, incoming.text):
            async with ChatActionSender.typing(chat_id=message.chat.id, bot=message.bot):
                reply = await service.handle(incoming)
        else:
            reply = await service.handle(incoming)

        if reply.delay and random.random() < delay_probability:
            delay_min, delay_max = delay_range_for_text(reply.text)
            await asyncio.sleep(random.uniform(delay_min, delay_max))

        profile = await repository.get_client(message.from_user.id)
        if profile and profile.phone:
            await state.set_state(ClientJourney.ai_dialog)
        elif profile and profile.name:
            await state.set_state(ClientJourney.waiting_phone)
        else:
            await state.set_state(ClientJourney.waiting_name)

        await message.answer(reply.text, reply_markup=reply_markup_for_response())

    router.message.register(process, CommandStart())
    router.message.register(process, F.text | F.contact)
    return router
