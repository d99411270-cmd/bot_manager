from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from .models import AiTurn, BotReply, ClientProfile, HistoryEntry, IncomingMessage, IntakeAnalysis
from .repositories import CRMRepository

START_TEXT = (
    "Здравствуйте!\n"
    "Меня зовут Иван, я персональный менеджер оптового магазина продуктов «Стокозавр».\n"
    "Помогу подобрать продукцию, узнать актуальные цены и оформить заказ.\n\n"
    "Подскажите, пожалуйста, как я могу к вам обращаться?"
)
FALLBACK = "Я уточню этот вопрос и вернусь к вам."
PRODUCT_QUESTION = "Подскажите, какая продукция вас сейчас интересует?"
PRODUCT_ASSORTMENT = (
    "В Стокозавре представлены основные категории продуктов для оптовых закупок: "
    "бакалея, напитки, консервация и другие товары. "
)
PRODUCT_CATEGORY_QUESTION = "Подскажите, какая категория вам интересна?"
VOLUME_QUESTION = "Подскажите, пожалуйста, какой объём продукции вам необходим?"
INFO_ACKNOWLEDGEMENT = "Актуальную цену и наличие я уточню. "
NAME_QUESTION = "Подскажите, пожалуйста, как я могу к вам обращаться?"
PHONE_QUESTION = (
    "Подскажите, пожалуйста, ваш номер телефона для связи и закрепления информации о вас."
)
NAME_REFUSAL = "Имя поможет мне обращаться к вам удобнее. "
PHONE_REFUSAL = (
    "Номер нужен, чтобы закрепить за вами информацию и при необходимости быстро "
    "связаться по вопросам заказа. "
)
logger = logging.getLogger(__name__)


class SalesAI(Protocol):
    async def analyze_intake(
        self, profile: ClientProfile, history: list[HistoryEntry], message: str
    ) -> IntakeAnalysis: ...

    async def respond(
        self, profile: ClientProfile, history: list[HistoryEntry], message: str
    ) -> AiTurn: ...


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        return None
    return "+" + digits


def asks_for_unverified_info(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(
            r"(?:\bцен\w*|\bсто\w*|\bскид\w*|\bналич\w*|\bна складе\b|"
            r"\bесть ли\b|₽|\bруб\w*)",
            lowered,
        )
    )


def asks_about_assortment(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"(?:какая|какой|какие).{0,24}(?:есть|ассортимент|продукц|товар)|"
            r"что (?:у вас )?(?:есть|прода|предлага)|чем торгу|что прода",
            lowered,
        )
    )


def looks_like_volume(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"(?:\d|\b(?:кг|килограмм\w*|тонн\w*|короб\w*|ящик\w*|паллет\w*|"
            r"упаков\w*|банк\w*|штук\w*|шт\.?|литр\w*|л)\b)",
            lowered,
        )
    ) and not asks_for_unverified_info(lowered)


def is_unsafe_claim(text: str) -> bool:
    """Reject prices or affirmative stock language without a trusted catalog source."""
    lowered = text.lower()
    price_terms = re.search(r"(?:\bцен\w*|\bстоимост\w*|₽|\bруб\w*)", lowered)
    stock_terms = re.search(
        r"(?:\bв наличии\b|\bна складе\b|\bиме(?:ется|ются)\b|"
        r"\bдоступ(?:ен|на|но|ны)\b|\bточно есть\b)",
        lowered,
    )
    return bool(price_terms or stock_terms)


def is_valid_ai_reply(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped.count("?") <= 1 and not is_unsafe_claim(stripped)


def returning_greeting(client: ClientProfile) -> str:
    if client.name and client.product:
        return f"{client.name}, ранее вы интересовались {client.product}. Чем могу помочь?"
    if client.name:
        return f"Здравствуйте, {client.name}. Чем могу помочь?"
    return START_TEXT


class ConversationService:
    def __init__(
        self,
        repository: CRMRepository,
        ai: SalesAI,
        *,
        history_limit: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.repository = repository
        self.ai = ai
        self.history_limit = history_limit
        self.clock = clock

    @staticmethod
    def should_use_ai(client: ClientProfile | None, text: str) -> bool:
        """Let Telegram show typing only while an actual AI request is in flight."""
        if text.strip().lower() == "/start":
            return False
        if not client or not client.name:
            return True
        if not client.phone:
            return normalize_phone(text) is None
        if not client.product or not client.volume:
            return True
        return True

    async def handle(self, message: IncomingMessage) -> BotReply:
        now = self.clock()
        client = await self.repository.get_client(message.telegram_id)
        if client is None:
            client = ClientProfile(
                telegram_id=message.telegram_id,
                username=message.username,
                first_contact_at=now,
                last_contact_at=now,
            )
        client.username = message.username or client.username
        client.last_contact_at = now
        text = message.text.strip()

        if text.lower() in {"/start", "start", "начать"}:
            return await self._handle_start(client, message.text, now)

        if not client.name or not client.phone or not client.product or not client.volume:
            return await self._handle_intake(client, message, now)

        return await self._handle_ai(client, message.text, now)

    async def _handle_intake(
        self, client: ClientProfile, message: IncomingMessage, now: datetime
    ) -> BotReply:
        text = message.text.strip()

        # Telegram contacts and an explicitly valid phone at the phone gate need no AI.
        if client.name and not client.phone:
            deterministic_phone = normalize_phone(message.contact_phone or text)
            if deterministic_phone:
                client.phone = deterministic_phone
                client.status = "уточнение продукта"
                return await self._finish(
                    client,
                    message.text,
                    BotReply(f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}"),
                    now,
                )

        if (
            client.name
            and client.phone
            and not client.product
            and asks_about_assortment(text)
            and not asks_for_unverified_info(text)
        ):
            return await self._finish(
                client,
                message.text,
                BotReply(PRODUCT_ASSORTMENT + PRODUCT_CATEGORY_QUESTION),
                now,
            )

        history = await self.repository.get_history(client.telegram_id, self.history_limit)
        try:
            semantic = await self.ai.analyze_intake(client, history, message.text)
        except Exception:
            logger.exception("DeepSeek intake failed for telegram_id=%s", client.telegram_id)
            semantic = None

        price_prefix = INFO_ACKNOWLEDGEMENT if asks_for_unverified_info(text) else ""

        if not client.name:
            if semantic and semantic.intent in {"provide_data", "correction"}:
                name = self._valid_name(semantic.name)
                if name:
                    client.name = name
                    client.status = "ожидает телефон"
                    phone = normalize_phone(semantic.phone or "")
                    if phone:
                        client.phone = phone
                        client.status = "уточнение продукта"
                        return await self._finish(
                            client,
                            message.text,
                            BotReply(f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}"),
                            now,
                        )
                    return await self._finish(
                        client,
                        message.text,
                        BotReply(f"Очень приятно, {client.name}.\n{PHONE_QUESTION}"),
                        now,
                    )
            prefix = self._intake_prefix(semantic, "name", price_prefix)
            return await self._finish(client, message.text, BotReply(prefix + NAME_QUESTION), now)

        if not client.phone:
            if semantic and semantic.intent == "correction" and self._valid_name(semantic.name):
                client.name = self._valid_name(semantic.name)
            if semantic and semantic.intent in {"provide_data", "correction"}:
                phone = normalize_phone(semantic.phone or "")
                if phone:
                    client.phone = phone
                    client.status = "уточнение продукта"
                    return await self._finish(
                        client,
                        message.text,
                        BotReply(f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}"),
                        now,
                    )
            prefix = self._intake_prefix(semantic, "phone", price_prefix)
            if semantic and semantic.phone and not normalize_phone(semantic.phone):
                prefix = "Не получилось распознать номер. Отправьте корректный телефон. "
            return await self._finish(
                client,
                message.text,
                BotReply(prefix + PHONE_QUESTION),
                now,
            )

        if not client.product:
            if semantic and semantic.intent == "correction":
                self._apply_prior_corrections(client, semantic)
            if semantic and semantic.intent in {"provide_data", "correction"} and semantic.product:
                client.product = semantic.product[:300]
                client.status = "уточнение объёма"
                if semantic.volume and looks_like_volume(semantic.volume):
                    client.volume = semantic.volume[:300]
                    client.status = "квалифицирован"
                    await self.repository.save_client(client)
                    return await self._handle_ai(client, message.text, now)
                return await self._finish(client, message.text, BotReply(VOLUME_QUESTION), now)
            if semantic and semantic.intent == "question" and not price_prefix:
                prefix = self._product_business_prefix(semantic.reply)
                return await self._finish(
                    client,
                    message.text,
                    BotReply(prefix + PRODUCT_CATEGORY_QUESTION),
                    now,
                )
            prefix = self._intake_prefix(semantic, "product", price_prefix)
            return await self._finish(
                client, message.text, BotReply(prefix + PRODUCT_QUESTION), now
            )

        if semantic and semantic.intent == "correction":
            self._apply_prior_corrections(client, semantic)
        if (
            semantic
            and semantic.intent in {"provide_data", "correction"}
            and semantic.volume
            and looks_like_volume(semantic.volume)
        ):
            client.volume = semantic.volume[:300]
            client.status = "квалифицирован"
            await self.repository.save_client(client)
            return await self._handle_ai(client, message.text, now)
        prefix = self._intake_prefix(semantic, "volume", price_prefix)
        return await self._finish(client, message.text, BotReply(prefix + VOLUME_QUESTION), now)

    @staticmethod
    def _valid_name(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.strip()
        if not 2 <= len(cleaned) <= 80 or any(ch.isdigit() for ch in cleaned):
            return None
        return cleaned

    @staticmethod
    def _apply_prior_corrections(client: ClientProfile, semantic: IntakeAnalysis) -> None:
        corrected_name = ConversationService._valid_name(semantic.name)
        if corrected_name:
            client.name = corrected_name
        corrected_phone = normalize_phone(semantic.phone or "")
        if corrected_phone:
            client.phone = corrected_phone
        if semantic.product:
            client.product = semantic.product[:300]

    @staticmethod
    def _intake_prefix(semantic: IntakeAnalysis | None, field: str, forced_prefix: str) -> str:
        if forced_prefix:
            return forced_prefix
        if semantic and semantic.intent == "refusal":
            return NAME_REFUSAL if field == "name" else PHONE_REFUSAL if field == "phone" else ""
        if semantic and semantic.intent == "greeting":
            return "Здравствуйте! "
        if semantic and semantic.reply:
            reply = semantic.reply.strip()
            if "?" not in reply and not is_unsafe_claim(reply):
                if reply[-1] not in ".!":
                    reply += "."
                return reply + " "
        return ""

    @staticmethod
    def _product_business_prefix(ai_reply: str | None) -> str:
        if ai_reply:
            reply = ai_reply.strip()
            lowered = reply.lower()
            if (
                "бакалея" in lowered
                and "напитки" in lowered
                and "консервация" in lowered
                and "?" not in reply
                and not is_unsafe_claim(reply)
            ):
                if reply[-1] not in ".!":
                    reply += "."
                return reply + " "
        return PRODUCT_ASSORTMENT

    async def _handle_start(
        self, client: ClientProfile, user_message: str, now: datetime
    ) -> BotReply:
        if not client.name:
            reply = BotReply(START_TEXT)
        elif not client.phone:
            reply = BotReply(f"Очень приятно, {client.name}.\n{PHONE_QUESTION}")
        elif not client.product:
            reply = BotReply(f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}")
        elif not client.volume:
            reply = BotReply(VOLUME_QUESTION)
        else:
            reply = BotReply(returning_greeting(client))
        return await self._finish(client, user_message, reply, now)

    async def _handle_ai(self, client: ClientProfile, user_message: str, now: datetime) -> BotReply:
        history = await self.repository.get_history(client.telegram_id, self.history_limit)
        try:
            turn = await self.ai.respond(client, history, user_message)
        except Exception:
            logger.exception("DeepSeek request failed for telegram_id=%s", client.telegram_id)
            turn = AiTurn(reply=FALLBACK, needs_human=True)

        if turn.needs_human or not is_valid_ai_reply(turn.reply):
            reply = FALLBACK
            client.comment = "Нужен ответ менеджера"
            delay = False
        else:
            reply = turn.reply.strip()
            delay = True
        await self.repository.save_client(client)
        await self.repository.append_history(client.telegram_id, now, user_message, reply)
        return BotReply(reply, delay=delay)

    async def _finish(
        self, client: ClientProfile, user_message: str, reply: BotReply, now: datetime
    ) -> BotReply:
        await self.repository.save_client(client)
        await self.repository.append_history(client.telegram_id, now, user_message, reply.text)
        return reply
