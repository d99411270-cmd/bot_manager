from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .closing import PENZA_PROMO_AMOUNTS, closing_reply, looks_like_ready_to_buy
from .followup import FOLLOWUP_DELAY, apply_followup_rules, reply_quoted_price
from .models import AiTurn, BotReply, ClientProfile, HistoryEntry, IncomingMessage, IntakeAnalysis
from .product_catalog import (
    grounded_search_reply,
    infer_catalog_interest,
    search,
)
from .repositories import CRMRepository

START_TEXT = (
    "Здравствуйте!\n"
    "Меня зовут Иван, я персональный менеджер оптового магазина продуктов «Стокозавр».\n"
    "Помогу подобрать продукцию, узнать актуальные цены и оформить заказ.\n\n"
    "Подскажите, пожалуйста, как я могу к вам обращаться?"
)
FALLBACK = "Я уточню этот вопрос и вернусь к вам."
CATALOG_NO_MATCH_REPLY = "Подходящих товаров по этому запросу сейчас нет."
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
EMAIL_QUESTION = "Тогда оставьте, пожалуйста, почту для связи и закрепления информации о вас."
SKIP_CONTACT = "Хорошо, продолжим без контакта. "
_CAPTURE_INTENTS = {"provide_data", "correction"}
_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
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


def normalize_email(value: str) -> str | None:
    match = _EMAIL_RE.search(value or "")
    if not match:
        return None
    return match.group(0).strip().lower()


def looks_like_refusal(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"не (?:скажу|дам|хочу|буду|оставл)|без (?:телефона|номера|почты|контакта)|"
            r"отказ|не надо номер|не дам номер",
            lowered,
        )
    )


def has_contact(client: ClientProfile) -> bool:
    return bool(client.phone or client.email or client.contact_skipped)


def waiting_email(client: ClientProfile) -> bool:
    return bool(
        client.name
        and not client.phone
        and not client.email
        and not client.contact_skipped
        and client.status == "ожидает почту"
    )


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
    mentions_category = bool(
        re.search(
            r"фрукт|овощ|бакале|консерв|макар|масл|напит|ассортимент|"
            r"огурц|картофел|морков|лук|яблок|груш|банан|апельсин|"
            r"гречк|рис|мук|спагет|рожк|тушён|горош|кукуруз|лимон|сок|вод",
            lowered,
        )
    )
    looks_like_question = bool(
        re.search(
            r"\?|\bкакая\b|\bкакой\b|\bкакие\b|\bчто\b|\bесть\b|\bпрода|\bбудет\b|\bхватит\b",
            lowered,
        )
    )
    if mentions_category and looks_like_question:
        return True
    return bool(
        re.search(
            r"(?:какая|какой|какие).{0,24}(?:есть|ассортимент|продукц|товар)|"
            r"что (?:у вас )?(?:есть|прода|предлага)|чем торгу|что прода",
            lowered,
        )
    )


def prefers_chat_here(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"пишите сюда|сюда просто|здесь пишите|здесь общ|без телефона|только (?:здесь|тут|в телеграм)",
            lowered,
        )
    )


def asks_about_pending_update(text: str) -> bool:
    return bool(
        re.search(r"ну что там|что там\b|ну как там|есть ответ|уточнил", text.strip().lower())
    )


def is_irritated(text: str) -> bool:
    return bool(re.search(r"\bнудн\w*|\bзануд\w*|\bдушн\w*|долго объясня", text.lower()))


def asks_about_manufacturer(text: str) -> bool:
    return bool(re.search(r"производител\w*|кто выпуска\w*|чей бренд", text.lower()))


def _is_catalog_or_price_question(text: str) -> bool:
    return (
        asks_about_assortment(text)
        or asks_for_unverified_info(text)
        or asks_about_pending_update(text)
    )


def is_unsafe_claim(text: str, catalog_result: str | None = None) -> bool:
    """Reject concrete claims not supported by the catalog for this turn."""
    lowered = text.lower()
    if re.search(r"точно есть|всегда в наличии", lowered):
        return True
    if re.search(r"остат\w*\s*:?\s*\d+", lowered):
        return True
    if re.search(
        r"\b\d+\s*(?:сеток|мешков|коробов|коробок|ящиков|канистр|упаковок)\b",
        lowered,
    ):
        return True
    stock_terms = re.search(
        r"(?:\bв наличии\b|\bна складе\b|\bмного\b|\bмало\b|\bнет в наличии\b|\bиме(?:ется|ются)\b)",
        lowered,
    )
    if stock_terms and not _catalog_supports_claim(lowered, catalog_result):
        return True
    if not re.search(r"(?:\bцен\w*|\bстоимост\w*|₽|\bруб\w*)", lowered):
        return False
    claimed = [
        re.sub(r"\s+", "", match) for match in re.findall(r"(\d[\d\s]*)\s*(?:₽|руб)", lowered)
    ]
    if not claimed:
        return bool(re.search(r"(?:₽|\bруб\w*\b)", lowered))
    allowed = {
        re.sub(r"\s+", "", value)
        for value in re.findall(r"(\d[\d\s]*)\s*(?:₽|руб)", catalog_result or "", re.IGNORECASE)
    } | {str(value) for value in PENZA_PROMO_AMOUNTS}
    return any(amount not in allowed for amount in claimed)


def _catalog_supports_claim(reply: str, catalog_result: str | None) -> bool:
    if not catalog_result or not _catalog_has_positions(catalog_result):
        return False
    statuses = {
        status.lower()
        for status in re.findall(r"Статус наличия:\s*([^;\n]+)", catalog_result, re.IGNORECASE)
    }
    has_status = bool(statuses) and (
        any(status in reply for status in statuses if status)
        or bool(re.search(r"\bв наличии\b|\bна складе\b", reply))
    )
    has_product = any(
        word.lower() in reply
        for word in re.findall(r"[а-яёa-z0-9-]{4,}", catalog_result)
        if word.lower()
        not in {"категория", "подкатегория", "производитель", "фасовка", "статус", "наличия"}
    )
    return has_status and has_product


def looks_like_volume(text: str) -> bool:
    return extract_volume(text) is not None and not asks_for_unverified_info(text)


def extract_volume(text: str) -> str | None:
    match = re.search(
        r"(?:пол)?паллет\w*|\d+(?:[.,]\d+)?\s*(?:кг|килограмм\w*|тонн\w*|короб\w*|"
        r"ящик\w*|бан\w*|паллет\w*|упаков\w*|шт\.?|штук\w*|литр\w*)",
        text.lower(),
    )
    if not match:
        return None
    return text[match.start() : match.end()].strip()


_NAME_STOP = {
    "огурцы",
    "овощи",
    "фрукты",
    "картофель",
    "морковь",
    "масло",
    "макароны",
    "привет",
    "здравствуйте",
    "добрый",
    "давай",
    "можно",
    "без",
    "почты",
    "телефона",
    "да",
    "нет",
    "не",
    "ок",
    "ага",
    "скажу",
    "хорошо",
    "понял",
    "номер",
}


def parse_person_name(value: str | None) -> tuple[str, str | None] | None:
    if not value:
        return None
    cleaned = value.strip()
    cleaned = re.sub(r"^(?:меня зовут|меня звать|я\s+|это\s+)\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .!,")
    if not cleaned or any(ch.isdigit() for ch in cleaned):
        return None
    parts = [part for part in re.split(r"\s+", cleaned) if part]
    if not parts or len(parts) > 3:
        return None
    for part in parts:
        letters = re.sub(r"[.\-]", "", part)
        if not 2 <= len(part) <= 40 or not letters.isalpha() or part.lower() in _NAME_STOP:
            return None
    first = parts[0].capitalize() if parts[0].islower() else parts[0]
    last = " ".join(parts[1:]) or None
    return first, last


def is_valid_ai_reply(text: str, catalog_result: str | None = None) -> bool:
    stripped = text.strip()
    return (
        bool(stripped)
        and stripped.count("?") <= 1
        and not is_unsafe_claim(stripped, catalog_result)
    )


def _is_honest_no_match(reply: str) -> bool:
    lowered = reply.lower()
    return bool(re.search(r"нет|не найден|отсутств\w*|подходящ\w* товар\w*", lowered))


def _ai_rejection_reason(turn: AiTurn | None, catalog_result: str | None = None) -> str | None:
    if turn is None:
        return "exception"
    if turn.needs_human:
        return "needs_human"
    if (
        catalog_result
        and "CATALOG_RESULT_EMPTY" in catalog_result
        and not _catalog_has_positions(catalog_result)
        and not _is_honest_no_match(turn.reply)
    ):
        return "invalid_reply"
    if not turn.reply or is_unsafe_claim(turn.reply, catalog_result):
        return "unsafe_reply" if is_unsafe_claim(turn.reply, catalog_result) else "invalid_reply"
    if not is_valid_ai_reply(turn.reply, catalog_result):
        return "invalid_reply"
    return None


def _catalog_has_positions(result: str) -> bool:
    return any("SKU:" in line for line in result.splitlines())


def _repair_reply_is_grounded(reply: str, catalog_result: str) -> bool:
    if not _catalog_has_positions(catalog_result):
        return not asks_for_unverified_info(reply)
    catalog_words = {
        word.lower()
        for word in re.findall(r"[а-яёa-z0-9-]{4,}", catalog_result)
        if word.lower() not in {"категория", "подкатегория", "производитель", "фасовка"}
    }
    return any(word in reply.lower() for word in catalog_words)


_COMPETITOR_MENTION_RE = re.compile(
    r"конкурент\w*|сравн\w*|альтернатив\w*|вариант\w*", re.IGNORECASE
)
_COMPETITOR_SAFE_REPLY = "Актуальную информацию уточню и вернусь к вам."


def limit_competitor_mentions(client: ClientProfile, text: str) -> str:
    """Suppress competitor/comparison language until the feature is re-enabled."""
    if _COMPETITOR_MENTION_RE.search(text):
        client.competitor_mentions = 0
        client.competitor_last_reply = False
        return _COMPETITOR_SAFE_REPLY
    client.competitor_last_reply = False
    return text


def returning_greeting(client: ClientProfile) -> str:
    if client.name and client.product:
        return f"{client.name}, ранее вы интересовались {client.product}. Чем могу помочь?"
    if client.name:
        return f"Здравствуйте, {client.name}. Чем могу помочь?"
    return START_TEXT


def is_qualified(client: ClientProfile) -> bool:
    return bool(client.name and has_contact(client) and client.product and client.volume)


class ConversationService:
    def __init__(
        self,
        repository: CRMRepository,
        ai: SalesAI,
        *,
        history_limit: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        followup_delay: timedelta = FOLLOWUP_DELAY,
    ) -> None:
        self.repository = repository
        self.ai = ai
        self.history_limit = history_limit
        self.clock = clock
        self.followup_delay = followup_delay

    @staticmethod
    def should_use_ai(client: ClientProfile | None, text: str) -> bool:
        """Let Telegram show typing only while an actual AI request is in flight."""
        if text.strip().lower() == "/start":
            return False
        if not client or not client.name:
            return True
        if waiting_email(client):
            return normalize_email(text) is None
        if not client.phone and not client.email and not client.contact_skipped:
            return normalize_phone(text) is None
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
        # A new substantive client turn separates comparison mentions.
        client.competitor_last_reply = False

        if text.lower() in {"/start", "start", "начать"}:
            return await self._handle_start(client, message.text, now)

        if client.name and not client.phone:
            deterministic_phone = normalize_phone(message.contact_phone or text)
            if deterministic_phone:
                client.phone = deterministic_phone
                client.contact_skipped = False
                client.status = "уточнение продукта"
                return await self._finish(
                    client,
                    message.text,
                    BotReply(f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}"),
                    now,
                )
            if waiting_email(client):
                email = normalize_email(text)
                if email:
                    client.email = email
                    client.status = "уточнение продукта"
                    return await self._finish(
                        client,
                        message.text,
                        BotReply(f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}"),
                        now,
                    )

        return await self._handle_manager(client, message, now)

    async def _handle_manager(
        self, client: ClientProfile, message: IncomingMessage, now: datetime
    ) -> BotReply:
        text = message.text.strip()
        history = await self.repository.get_history(client.telegram_id, self.history_limit)
        semantic = await self._safe_analyze(client, history, message.text)
        captured = self._apply_intake_facts(client, semantic, message.text)
        if semantic and semantic.budget is not None:
            client.budget = semantic.budget
            client.comment = self._with_comment(client.comment, f"Бюджет: {semantic.budget} ₽")
            captured = True

        if is_irritated(text):
            return await self._finish(
                client,
                message.text,
                BotReply("Понял, перегнул. Буду короче и по делу. Что нужно уточнить?"),
                now,
            )

        if asks_about_manufacturer(text):
            client.needs_human = True
            client.comment = "Нужен менеджер: подтвердить производителя"
            return await self._finish(
                client,
                message.text,
                BotReply("В каталоге нет подтверждённого производителя. Передам вопрос менеджеру."),
                now,
            )

        if prefers_chat_here(text) and client.name and not has_contact(client):
            client.contact_skipped = True
            client.status = "уточнение продукта"

        refused = (semantic and semantic.intent == "refusal") or looks_like_refusal(text)
        if refused and client.name and not _is_catalog_or_price_question(text):
            reply = await self._handle_contact_refusal(client, history, message.text)
            if reply is not None:
                return await self._finish(client, message.text, reply, now)

        if looks_like_ready_to_buy(text) and client.name:
            return await self._handle_closing(client, history, message.text, now)

        catalog_result = (
            search(client.current_interest or client.product or text)
            if client.current_interest or client.product
            else search(text)
            if _is_catalog_or_price_question(text)
            else None
        )

        catalog_empty_check = bool(
            client.product
            and catalog_result
            and not _catalog_has_positions(catalog_result)
            and (
                asks_about_assortment(text)
                or bool(re.search(r"\b(?:какой|какая|какие|какое)\b.{0,30}\bесть\b", text.lower()))
            )
        )
        if catalog_empty_check:
            catalog_result = (
                "CATALOG_RESULT_EMPTY: deterministic search found no matching positions. "
                "Check the customer's meaning against the available categories; do not invent "
                "products, prices, or availability.\n" + (catalog_result or "")
            )

        if is_qualified(client):
            client.status = "квалифицирован"
            await self.repository.save_client(client)
            return await self._handle_ai(client, message.text, now, catalog_result)

        if (
            captured
            and semantic
            and semantic.intent in _CAPTURE_INTENTS
            and not _is_catalog_or_price_question(text)
        ):
            return await self._finish(
                client, message.text, BotReply(self._next_question_after_capture(client)), now
            )

        if (
            client.name
            and not client.phone
            and semantic
            and semantic.phone
            and not normalize_phone(semantic.phone)
        ):
            return await self._finish(
                client, message.text, BotReply(self._fallback_reply(client, semantic, text)), now
            )

        turn = await self._safe_respond(client, history, message.text, catalog_result)
        rejection_reason = _ai_rejection_reason(turn, catalog_result)
        if rejection_reason is None and turn is not None:
            self._remember_catalog_interest(client, catalog_result, turn.reply)
            return await self._finish(client, message.text, BotReply(turn.reply.strip()), now)
        logger.warning(
            "Rejected AI reply for telegram_id=%s reason=%s needs_human=%s",
            client.telegram_id,
            rejection_reason,
            bool(turn and turn.needs_human),
        )
        repair = await self._safe_repair(
            client, history, message.text, rejection_reason or "invalid_reply", catalog_result or ""
        )
        repair_reason = _ai_rejection_reason(repair, catalog_result)
        if (
            repair_reason is None
            and repair is not None
            and _repair_reply_is_grounded(repair.reply, catalog_result or "")
        ):
            return await self._finish(client, message.text, BotReply(repair.reply.strip()), now)
        if repair is not None:
            logger.warning(
                "Rejected repair reply for telegram_id=%s reason=%s needs_human=%s",
                client.telegram_id,
                repair_reason or "not_grounded",
                repair.needs_human,
            )

        catalog_reply = (
            None
            if asks_for_unverified_info(text) and not client.volume
            else grounded_search_reply(
                catalog_result or "",
                client.name,
                history[-1].assistant_message if history else None,
            )
        )
        if catalog_reply:
            return await self._finish(
                client, message.text, BotReply(catalog_reply, delay=False), now
            )
        return await self._finish(
            client, message.text, BotReply(self._fallback_reply(client, semantic, text)), now
        )

    async def _safe_analyze(
        self, client: ClientProfile, history: list[HistoryEntry], message: str
    ) -> IntakeAnalysis | None:
        try:
            return await self.ai.analyze_intake(client, history, message)
        except Exception:
            logger.exception("DeepSeek intake failed for telegram_id=%s", client.telegram_id)
            return None

    async def _safe_respond(
        self,
        client: ClientProfile,
        history: list[HistoryEntry],
        message: str,
        catalog_result: str | None = None,
    ) -> AiTurn | None:
        try:
            respond_with_catalog = getattr(self.ai, "respond_with_catalog", None)
            if catalog_result is not None and callable(respond_with_catalog):
                return await respond_with_catalog(client, history, message, catalog_result)
            return await self.ai.respond(client, history, message)
        except Exception:
            logger.exception(
                "DeepSeek request failed for telegram_id=%s reason=exception", client.telegram_id
            )
            return None

    async def _safe_repair(
        self,
        client: ClientProfile,
        history: list[HistoryEntry],
        message: str,
        reason: str,
        catalog_result: str,
    ) -> AiTurn | None:
        repair_response = getattr(self.ai, "repair_response", None)
        if not callable(repair_response):
            return None
        try:
            return await repair_response(client, history, message, reason, catalog_result)
        except Exception:
            logger.exception(
                "DeepSeek repair failed for telegram_id=%s reason=repair_exception",
                client.telegram_id,
            )
            return None

    def _apply_intake_facts(
        self, client: ClientProfile, semantic: IntakeAnalysis | None, text: str = ""
    ) -> bool:
        captured = False
        parsed = parse_person_name(text)
        can_read_semantic = bool(
            semantic and (semantic.intent in _CAPTURE_INTENTS or semantic.intent == "question")
        )
        if not parsed and can_read_semantic:
            parsed = parse_person_name(semantic.name)
        if parsed and (not client.name or (can_read_semantic and semantic.intent == "correction")):
            client.name, last = parsed
            if last:
                client.last_name = last
            captured = True
            if not has_contact(client):
                client.status = "ожидает телефон"
        if can_read_semantic and client.name:
            phone = normalize_phone(semantic.phone or text)
            if phone:
                client.phone = phone
                client.contact_skipped = False
                client.status = "уточнение продукта"
                captured = True
            if waiting_email(client):
                email = normalize_email(semantic.reply or "") or normalize_email(text)
                if email:
                    client.email = email
                    client.status = "уточнение продукта"
                    captured = True
            if has_contact(client) and semantic.product:
                if client.product and client.product != semantic.product:
                    client.original_interests = list(client.original_interests or [client.product])
                client.current_interest = semantic.product[:300]
                client.product = semantic.product[:300]
                client.status = "уточнение объёма"
                captured = True
        volume = extract_volume(text)
        if (
            not volume
            and can_read_semantic
            and semantic.volume
            and looks_like_volume(semantic.volume)
        ):
            volume = semantic.volume.strip()[:300]
        if volume and client.product:
            client.volume = volume[:300]
            captured = True
            if has_contact(client):
                client.status = "квалифицирован"
        return captured

    async def _handle_contact_refusal(
        self, client: ClientProfile, history: list[HistoryEntry], message: str
    ) -> BotReply | None:
        if client.phone or client.email or client.contact_skipped:
            return None
        turn = await self._safe_respond(client, history, message)
        ai_text = (
            turn.reply.strip()
            if turn and not turn.needs_human and is_valid_ai_reply(turn.reply)
            else ""
        )
        if client.status != "ожидает почту":
            client.status = "ожидает почту"
            if ai_text and "почт" in ai_text.lower():
                return BotReply(ai_text)
            return BotReply(EMAIL_QUESTION)
        client.contact_skipped = True
        client.status = "уточнение продукта"
        if ai_text and PRODUCT_QUESTION.split()[0].lower() in ai_text.lower():
            return BotReply(ai_text)
        return BotReply(SKIP_CONTACT + PRODUCT_QUESTION)

    @staticmethod
    def _next_question_after_capture(client: ClientProfile) -> str:
        if not has_contact(client):
            if client.status == "ожидает почту":
                return EMAIL_QUESTION
            return f"Очень приятно, {client.name}.\n{PHONE_QUESTION}"
        if not client.product:
            return f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}"
        return VOLUME_QUESTION

    def _fallback_reply(
        self, client: ClientProfile, semantic: IntakeAnalysis | None, text: str
    ) -> str:
        if _is_catalog_or_price_question(text):
            return FALLBACK
        if not client.name:
            prefix = self._intake_prefix(semantic, "name", "")
            return prefix + NAME_QUESTION
        if not has_contact(client) and client.status in {
            "новый",
            "ожидает телефон",
            "ожидает почту",
        }:
            if client.status == "ожидает почту":
                return EMAIL_QUESTION
            prefix = self._intake_prefix(semantic, "phone", "")
            if semantic and semantic.phone and not normalize_phone(semantic.phone):
                prefix = "Не получилось распознать номер. Отправьте корректный телефон. "
            return prefix + PHONE_QUESTION
        if not client.product:
            prefix = self._intake_prefix(semantic, "product", "")
            return prefix + PRODUCT_QUESTION
        if not client.volume:
            prefix = self._intake_prefix(semantic, "volume", "")
            return prefix + VOLUME_QUESTION
        return FALLBACK

    @staticmethod
    def _with_comment(comment: str | None, addition: str) -> str:
        if comment and addition in comment:
            return comment
        return f"{comment}; {addition}" if comment else addition

    @staticmethod
    def _valid_name(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = value.strip()
        if not 2 <= len(cleaned) <= 80 or any(ch.isdigit() for ch in cleaned):
            return None
        return cleaned

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

    async def _handle_start(
        self, client: ClientProfile, user_message: str, now: datetime
    ) -> BotReply:
        if not client.name:
            reply = BotReply(START_TEXT)
        elif waiting_email(client):
            reply = BotReply(EMAIL_QUESTION)
        elif not has_contact(client):
            reply = BotReply(f"Очень приятно, {client.name}.\n{PHONE_QUESTION}")
        elif not client.product:
            reply = BotReply(f"Спасибо, {client.name}.\n{PRODUCT_QUESTION}")
        elif not client.volume:
            reply = BotReply(VOLUME_QUESTION)
        else:
            reply = BotReply(returning_greeting(client))
        return await self._finish(client, user_message, reply, now)

    async def _handle_closing(
        self,
        client: ClientProfile,
        history: list[HistoryEntry],
        user_message: str,
        now: datetime,
    ) -> BotReply:
        client.status = "готов к заказу"
        fallback = closing_reply(client, user_message) or FALLBACK
        turn = await self._safe_respond(client, history, user_message)
        if turn and not turn.needs_human and is_valid_ai_reply(turn.reply):
            lowered = turn.reply.lower()
            if not client.phone and ("телефон" in lowered or "номер" in lowered):
                return await self._finish(client, user_message, BotReply(turn.reply.strip()), now)
            if client.phone and ("удобн" in lowered or "звон" in lowered or "время" in lowered):
                return await self._finish(client, user_message, BotReply(turn.reply.strip()), now)
        return await self._finish(client, user_message, BotReply(fallback), now)

    async def _handle_ai(
        self,
        client: ClientProfile,
        user_message: str,
        now: datetime,
        catalog_result: str | None = None,
    ) -> BotReply:
        history = await self.repository.get_history(client.telegram_id, self.history_limit)
        turn = await self._safe_respond(client, history, user_message, catalog_result)
        self._apply_turn_facts(client, turn or AiTurn(reply="", needs_human=False))
        if turn is not None and _ai_rejection_reason(turn, catalog_result) is None:
            self._remember_catalog_interest(client, catalog_result, turn.reply)
        rejection_reason = _ai_rejection_reason(turn, catalog_result)
        if rejection_reason is not None:
            if turn and turn.needs_human:
                client.needs_human = True
            logger.warning(
                "Rejected AI reply for telegram_id=%s reason=%s needs_human=%s",
                client.telegram_id,
                rejection_reason,
                bool(turn and turn.needs_human),
            )
            repair = await self._safe_repair(
                client, history, user_message, rejection_reason, catalog_result or ""
            )
            repair_reason = _ai_rejection_reason(repair, catalog_result)
            if (
                repair_reason is None
                and repair is not None
                and _repair_reply_is_grounded(repair.reply, catalog_result or "")
            ):
                return await self._finish(
                    client, user_message, BotReply(repair.reply.strip(), delay=False), now
                )
            if repair is not None:
                logger.warning(
                    "Rejected repair reply for telegram_id=%s reason=%s needs_human=%s",
                    client.telegram_id,
                    repair_reason or "not_grounded",
                    repair.needs_human,
                )
            catalog_reply = (
                None
                if rejection_reason in {"unsafe_reply", "invalid_reply"}
                else grounded_search_reply(
                    catalog_result or "",
                    client.name,
                    history[-1].assistant_message if history else None,
                )
            )
            if catalog_reply:
                reply = catalog_reply
            else:
                reply = (
                    CATALOG_NO_MATCH_REPLY
                    if catalog_result and "CATALOG_RESULT_EMPTY" in catalog_result
                    else FALLBACK
                )
                client.comment = "Нужен ответ менеджера"
            return await self._finish(client, user_message, BotReply(reply, delay=False), now)
        return await self._finish(
            client, user_message, BotReply(turn.reply.strip(), delay=True), now
        )

    @staticmethod
    def _remember_catalog_interest(
        client: ClientProfile, catalog_result: str | None, reply: str
    ) -> None:
        if not catalog_result:
            return
        interest = infer_catalog_interest(catalog_result, reply)
        if not interest:
            return
        if client.product and client.product != interest:
            client.original_interests = list(client.original_interests or [client.product])
        client.current_interest = interest[:300]
        if has_contact(client):
            client.product = interest[:300]
            if client.status == "уточнение продукта":
                client.status = "уточнение объёма"

    @staticmethod
    def _apply_turn_facts(client: ClientProfile, turn: AiTurn) -> None:
        if turn.product:
            if client.product and client.product != turn.product:
                client.original_interests = list(client.original_interests or [client.product])
            client.current_interest = turn.product[:300]
            client.product = turn.product[:300]
        if turn.volume and looks_like_volume(turn.volume):
            client.volume = turn.volume[:300]
        if is_qualified(client) and client.status not in {"готов к заказу", "получил предложение"}:
            client.status = "квалифицирован"

    async def _finish(
        self, client: ClientProfile, user_message: str, reply: BotReply, now: datetime
    ) -> BotReply:
        apply_followup_rules(client, user_message, reply.text, now, self.followup_delay)
        reply = BotReply(limit_competitor_mentions(client, reply.text), delay=reply.delay)
        if reply_quoted_price(reply.text) and client.status != "готов к заказу":
            client.status = "получил предложение"
        await self.repository.save_client(client)
        await self.repository.append_history(client.telegram_id, now, user_message, reply.text)
        return reply
