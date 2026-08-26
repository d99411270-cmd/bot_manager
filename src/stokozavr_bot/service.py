from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .catalog_quotes import (
    CompositeLineTotals,
    LineTotalQuote,
    NearestPackQuote,
    _product_terms,
    derived_allowed_amounts,
    parse_requested_quantity,
)
from .catalog_tokens import expand_query_terms, term_matches_haystack
from .closing import (
    CHANNEL_CALL,
    CHANNEL_PICKUP,
    CLOSE_ASK_TIME,
    CLOSE_NEED_PHONE,
    PENZA_PROMO_AMOUNTS,
    call_slot_reply,
    channel_clarify_reply,
    closing_reply,
    looks_like_call_request,
    looks_like_call_time,
    looks_like_pickup_choice,
    looks_like_pickup_question,
    looks_like_pickup_rejection,
    looks_like_ready_to_buy,
    parse_time_slot,
    pickup_choice_reply,
    pickup_info_reply,
    visit_slot_reply,
)
from .followup import FOLLOWUP_DELAY, apply_followup_rules, reply_quoted_price
from .handoff import ManagerHandoff
from .models import AiTurn, BotReply, ClientProfile, HistoryEntry, IncomingMessage, IntakeAnalysis
from .product_catalog import (
    UnitPriceQuote,
    _all_records,
    catalog_categories_in_result,
    composite_line_total_catalog_result,
    generated_price_list,
    grounded_quote_reply,
    grounded_search_reply,
    infer_catalog_interest,
    line_total_catalog_result,
    named_catalog_item,
    recover_product_from_history,
    search,
    unit_price_catalog_result,
    unit_price_quote,
)
from .repositories import CRMRepository

START_TEXT = (
    "Здравствуйте!\n"
    "Меня зовут Иван, я персональный менеджер оптового магазина продуктов «Стокозавр».\n"
    "Помогу подобрать продукцию, узнать актуальные цены и оформить заказ.\n\n"
    "Подскажите, пожалуйста, как я могу к вам обращаться?"
)
FALLBACK = "Я уточню этот вопрос и вернусь к вам."
CATALOG_NO_MATCH_REPLY = "Такого товара сейчас нет в каталоге."
CATALOG_RESULT_EMPTY_PREFIX = (
    "CATALOG_RESULT_EMPTY: deterministic search found no matching positions. "
    "Do not invent products, prices, or availability."
)
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

    async def open_dialog(
        self,
        profile: ClientProfile,
        history: list[HistoryEntry],
        message: str,
        reason: str,
        catalog_result: str,
    ) -> AiTurn: ...


def normalize_phone(value: str) -> str | None:
    compact = re.sub(r"[\s().-]", "", value or "")
    digits = re.sub(r"\D", "", compact)
    if len(digits) != 11:
        return None
    if compact.startswith("8"):
        return "+7" + digits[1:]
    if compact.startswith("+7"):
        return "+" + digits
    return None


def normalize_landline(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 6 else None


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


def _has_known_order(client: ClientProfile) -> bool:
    return bool(
        client.product or client.current_interest or client.original_interests or client.volume
    )


def requested_identity_slot(client: ClientProfile) -> str | None:
    """Return the identity field currently requested, if any."""
    if not client.name:
        return "name"
    if has_contact(client):
        return None
    if client.status == "ожидает почту":
        return "email"
    return "phone"


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


_BROAD_ASSORTMENT_PHRASES = frozenset(
    {
        "вся",
        "все",
        "все что есть",
        "что есть",
        "все что имеется",
        "что имеется",
    }
)


def _normalize_utterance(text: str) -> str:
    return re.sub(r"[^а-яёa-z0-9]+", " ", (text or "").lower()).strip().replace("ё", "е")


def _is_broad_assortment_utterance(text: str) -> bool:
    """«Вся/все/всё что есть» is assortment browse, not an SKU."""
    return _normalize_utterance(text) in _BROAD_ASSORTMENT_PHRASES


def asks_about_assortment(text: str) -> bool:
    if _is_broad_assortment_utterance(text):
        return True
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
            r"пишите сюда|сюда просто|здесь пишите|здесь общ|без телефона|"
            r"только (?:здесь|тут|в телеграм)|пишите в (?:этот\s+)?чат|"
            r"в этот чат",
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


def asks_for_competitor(text: str) -> bool:
    return bool(re.search(r"подешев\w*|есть вариант\w*|сравн\w*|альтернатив\w*", text.lower()))


def asks_for_price_list(text: str) -> bool:
    if cancels_price_list(text):
        return False
    return bool(re.search(r"\bпрайс(?:-?лист)?\b|\bкаталог\b", text.lower()))


def cancels_price_list(text: str) -> bool:
    lowered = text.lower()
    mentions_list = bool(re.search(r"\bпрайс(?:-?лист)?\b|\bкаталог\b|\bфайл\b", lowered))
    if not mentions_list:
        return False
    already_sent = bool(re.search(r"уже (?:прислал|отправил|выслал)|повторно не", lowered))
    refused = bool(re.search(r"не надо|не нужн", lowered))
    return already_sent or refused


def asks_for_price_list_in_chat(text: str) -> bool:
    return asks_for_price_list(text) and _mentions_chat_here(text)


def _mentions_chat_here(text: str) -> bool:
    return bool(
        re.search(
            r"прямо\s+(?:в\s+)?(?:чат|сюда|тут|здесь)|"
            r"\b(?:сюда|тут|здесь)\b|\bв\s+(?:этот\s+)?чат\b|"
            r"\bв\s+(?:телеграм(?:е|м)?|telegram)\b",
            text.lower(),
        )
    )


def _price_list_pending_from_history(history: list[HistoryEntry]) -> bool:
    if not history:
        return False
    latest = history[-1]
    return asks_for_price_list(latest.user_message) and "почт" in latest.assistant_message.lower()


PRICE_LIST_CHAT_REPLY = "Отправляю актуальный прайс прямо сюда."
PRICE_LIST_FILENAME = "stokozavr-price-list.md"


def _composite_order_catalog(text: str) -> str | None:
    lowered = text.lower()
    if not re.search(r"картоф|картош", lowered) or "макарон" not in lowered:
        return None
    quantity = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:кг|килограмм\w*)", lowered)
    potato = search("картофель")
    if not quantity or not _catalog_has_positions(potato):
        return None
    potato_line = next(line for line in potato.splitlines() if "SKU:" in line)
    pack_match = re.search(r"(\d+(?:[.,]\d+)?)\s*кг", potato_line.lower())
    price_match = re.search(r"(\d[\d\s]*)\s*₽", potato_line)
    if not pack_match or not price_match:
        return None
    kg = float(quantity.group(1).replace(",", "."))
    pack_kg = float(pack_match.group(1).replace(",", "."))
    if kg % pack_kg:
        return None
    packs = int(kg / pack_kg)
    total = packs * int(price_match.group(1).replace(" ", ""))
    return (
        potato_line
        + f"; Подтверждённый расчёт: {packs} сетки по {int(pack_kg)} кг; {total} ₽ за картофель"
        + "\n"
        + search("макароны")
    )


def _deterministic_recovery_reply(text: str, catalog_result: str) -> str | None:
    lowered = text.strip().lower()
    if re.search(r"закаж\w*|заказ\w*\s+можно", lowered):
        return "Да, заказать можно. Уточним, какой объём или фасовку нужно добавить к заказу?"
    if lowered in {"что?", "что дальше?", "и что дальше?", "не понял", "не поняла"}:
        return "Да, продолжим. Я могу помочь собрать заказ; что уточним первым?"
    calculation = re.search(
        r"Подтверждённый расчёт:\s*([^;]+);\s*(\d[\d\s]*)\s*₽\s*за картофель",
        catalog_result,
        re.IGNORECASE,
    )
    if calculation and "макарон" in lowered:
        return f"По картофелю подтверждено: {calculation.group(1)} — {calculation.group(2)} ₽. По макаронам какую фасовку выбрать?"
    return None


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
        _normalize_claim_amount(match)
        for match in re.findall(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб)", lowered)
    ]
    if not claimed:
        return bool(re.search(r"(?:₽|\bруб\w*\b)", lowered))
    allowed = {
        _normalize_claim_amount(value)
        for value in re.findall(
            r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб)", catalog_result or "", re.IGNORECASE
        )
    } | {str(value) for value in PENZA_PROMO_AMOUNTS}
    allowed.update(_structured_allowed_amounts(catalog_result))
    return any(amount not in allowed for amount in claimed)


def _structured_allowed_amounts(catalog_result: str | None) -> set[str]:
    """Allow pack prices and derived ₽/кг / ₽/л / ₽/шт from records in this turn."""
    if not catalog_result:
        return set()
    allowed: set[str] = set()
    skus = {
        sku.upper() for sku in re.findall(r"SKU:\s*([A-Z0-9-]+)", catalog_result, re.IGNORECASE)
    }
    for record in _all_records():
        if record.sku.upper() not in skus:
            continue
        for amount in derived_allowed_amounts(record.packaging, record.price):
            allowed.add(_normalize_claim_amount(amount))
        quote = unit_price_quote(record.subcategory, "кг") or unit_price_quote(
            record.subcategory, "л"
        )
        if quote:
            match = re.search(r"(\d+(?:[.,]\d+)?)", quote.unit_price)
            if match:
                allowed.add(_normalize_claim_amount(match.group(1)))
    return allowed


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


def _normalize_claim_amount(raw: str) -> str:
    compact = re.sub(r"\s+", "", raw).replace(",", ".")
    if "." in compact:
        whole, fraction = compact.split(".", 1)
        fraction = fraction.rstrip("0")
        return f"{whole}.{fraction}" if fraction else whole
    return compact.lstrip("0") or "0"


def looks_like_volume(text: str) -> bool:
    return extract_volume(text) is not None and not asks_for_unverified_info(text)


def invalid_phone_length(text: str) -> tuple[int, str] | None:
    digits = re.sub(r"\D", "", text)
    if not digits or len(digits) not in {10, 12}:
        return None
    if not re.search(r"(?:\+?7|8|9)", text.replace(" ", "")):
        return None
    return len(digits), "short" if len(digits) == 10 else "long"


def phone_digit_attempt(text: str) -> bool:
    """Return whether a phone-stage message is a numeric phone attempt."""
    stripped = text.strip()
    return bool(re.fullmatch(r"[\d\s()+./-]+", stripped) and re.search(r"\d", stripped))


def _infer_unit_price_request(text: str) -> str | None:
    lowered = text.lower().replace("ё", "е")
    if re.search(
        r"за\s+(?:каждую\s+)?(?:\d+\s*)?(?:единиц\w*|еден[ие]ц\w*|штук\w*|штуку|шт\b|бутылк\w*|банк\w*)|"
        r"\b(?:отдельно|поштучно)\b",
        lowered,
    ):
        return "шт"
    if re.search(
        r"за\s+(?:каждый\s+)?(?:1\s*)?(?:кг|килограмм\w*)|на\s+килограмм|цена\s+за\s+(?:1\s*)?(?:кг|килограмм\w*)",
        lowered,
    ):
        return "кг"
    if re.search(
        r"за\s+(?:каждый\s+)?(?:1\s*)?(?:л|литр\w*)|на\s+литр|цена\s+за\s+(?:1\s*)?(?:л|литр\w*)",
        lowered,
    ):
        return "л"
    return None


def _volume_is_unit_price_probe(text: str, volume: str | None, unit: str | None) -> bool:
    if not unit or not volume:
        return False
    parsed = parse_requested_quantity(volume)
    if parsed is None or parsed.unit != unit or parsed.amount != 1:
        return False
    return _infer_unit_price_request(text) == unit


def _is_generic_fallback_reply(reply: str) -> bool:
    normalized = re.sub(r"[^а-яёa-z]+", " ", reply.lower()).strip()
    if normalized in {
        re.sub(r"[^а-яёa-z]+", " ", FALLBACK.lower()).strip(),
        "актуальную информацию уточню и вернусь к вам",
    }:
        return True
    return bool(
        re.search(
            r"\b(?:сейчас\s+)?(?:уточн\w*|провер\w*|выясн\w*|разбер\w*)\b"
            r"[^?]{0,100}\b(?:верн\w*|напиш\w*|сообщ\w*|свяж\w*)\b",
            reply,
            re.IGNORECASE,
        )
    )


def _asks_about_delivery(text: str) -> bool:
    return bool(re.search(r"достав\w*|привез\w*|привезти", text.lower()))


def extract_volume(text: str) -> str | None:
    match = re.search(
        r"(?:пол)?паллет\w*|\d+(?:[.,]\d+)?\s*(?:кг|килограмм\w*|тонн\w*|короб\w*|"
        r"ящик\w*|бан\w*|паллет\w*|упаков\w*|сет(?:ок|к\w*)|шт\.?|штук\w*|литр\w*|"
        r"г(?:р(?:амм\w*)?)?\.?|грамм\w*)|(?:литр\w*|килограмм\w*)\s+\d+(?:[.,]\d+)?",
        text.lower(),
    )
    if not match:
        return None
    return text[match.start() : match.end()].strip()


def _looks_like_packaging_fragment(text: str) -> bool:
    compact = (text or "").lower().replace("×", "x").replace("х", "x").replace("*", "x")
    return bool(re.search(r"\d+\s*x\s*\d+\s*(?:г|гр|грамм|кг|мл|л)", compact))


def _reply_reasks_volume(reply: str) -> bool:
    return bool(
        re.search(
            r"какой объём|какой объем|объём продукции вам необходим|какой объем продукции",
            (reply or "").lower(),
        )
    )


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


def _is_person_name_value(text: str) -> bool:
    """A name slot accepts a person name, not a greeting, order, volume, or catalog item."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if looks_like_ready_to_buy(stripped) or looks_like_refusal(stripped):
        return False
    if extract_volume(stripped) or _is_catalog_or_price_question(stripped):
        return False
    if phone_digit_attempt(stripped) or normalize_phone(stripped):
        return False
    return not _catalog_has_positions(search(stripped))


def _may_write_commercial_facts(client: ClientProfile) -> bool:
    """Product/volume may persist without contact, but must not skip a live phone/email slot."""
    return requested_identity_slot(client) not in {"phone", "email"}


def is_valid_ai_reply(text: str, catalog_result: str | None = None) -> bool:
    stripped = text.strip()
    return (
        bool(stripped)
        and stripped.count("?") <= 1
        and not is_unsafe_claim(stripped, catalog_result)
    )


def _is_honest_no_match(reply: str) -> bool:
    lowered = reply.lower()
    if "?" in reply or re.search(r"альтернатив|похож|замен|предлож|друг(?:ой|ие)", lowered):
        return False
    return bool(re.search(r"нет|не найден|отсутств\w*|подходящ\w* товар\w*", lowered))


def _ai_rejection_reason(turn: AiTurn | None, catalog_result: str | None = None) -> str | None:
    if turn is None:
        return "exception"
    if turn.needs_human:
        return "needs_human"
    if _is_generic_fallback_reply(turn.reply):
        return "invalid_reply"
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


def _reject_turn(
    turn: AiTurn | None,
    catalog_result: str | None,
    client: ClientProfile,
) -> str | None:
    reason = _ai_rejection_reason(turn, catalog_result)
    if reason or turn is None:
        return reason
    if client.volume and _reply_reasks_volume(turn.reply):
        claimed = re.findall(r"(\d[\d\s]*)\s*(?:₽|руб)", turn.reply.lower())
        if not claimed or not _catalog_has_positions(catalog_result or ""):
            return "invalid_reply"
    if _catalog_has_positions(catalog_result or "") and "не могу подтвердить" in turn.reply.lower():
        return "invalid_reply"
    return None


def _format_line_total_reply(quote: LineTotalQuote, name: str | None = None) -> str:
    prefix = f"{name}, " if name else ""
    record = quote.record
    reply = (
        f"{prefix}{quote.human_line} "
        f"({record.price} за {record.packaging}; производитель — {record.manufacturer})"
    )
    piece_unit = {"банка": "шт", "бутылка": "шт", "шт": "шт"}.get(quote.requested_unit)
    if piece_unit:
        unit = unit_price_quote(record.subcategory, piece_unit)
        if unit:
            reply += f". {unit.unit_price}"
    return reply + "."


def _format_nearest_pack_reply(quote: NearestPackQuote, name: str | None = None) -> str:
    prefix = f"{name}, " if name else ""
    record = quote.record
    return (
        f"{prefix}{quote.human_line} "
        f"({record.price} за {record.packaging}; производитель — {record.manufacturer})."
    )


def _format_grounded_quote_reply(
    quote: LineTotalQuote | NearestPackQuote, name: str | None = None
) -> str:
    if isinstance(quote, NearestPackQuote):
        return _format_nearest_pack_reply(quote, name)
    return _format_line_total_reply(quote, name)


def _format_composite_reply(combined: CompositeLineTotals, name: str | None = None) -> str:
    prefix = f"{name}, " if name else ""
    lines = "; ".join(quote.human_line for quote in combined.lines)
    return f"{prefix}{lines}. Итого {combined.total_amount} ₽."


def _requested_quote_quantity(
    text: str,
    semantic: IntakeAnalysis | None,
    client: ClientProfile,
):
    quantity = parse_requested_quantity(text)
    if quantity is not None:
        return quantity
    if semantic and semantic.volume:
        quantity = parse_requested_quantity(semantic.volume)
        if quantity is not None:
            return quantity
    utterance = _utterance_search_key(text)
    current = client.current_interest or client.product
    if utterance and current and not _topics_overlap(utterance, current):
        return None
    if client.volume:
        return parse_requested_quantity(client.volume)
    return None


def _topics_overlap(left: str, right: str) -> bool:
    first = (left or "").strip().lower()
    second = (right or "").strip().lower()
    if not first or not second:
        return False
    if first in second or second in first:
        return True
    left_terms = set(_product_terms(first))
    right_terms = set(_product_terms(second))
    return bool(left_terms & right_terms)


def _resolve_line_total_catalog(
    text: str,
    semantic: IntakeAnalysis | None,
    client: ClientProfile,
    catalog_query: str | None = None,
) -> tuple[str, LineTotalQuote | NearestPackQuote] | None:
    quantity = _requested_quote_quantity(text, semantic, client)
    if quantity is None:
        return None
    seen: set[str] = set()
    for target in (
        text,
        _utterance_search_key(text),
        semantic.target_product if semantic else None,
        semantic.product if semantic else None,
        catalog_query,
        client.current_interest,
        client.product,
    ):
        cleaned = (target or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        calculated = line_total_catalog_result(cleaned, quantity.raw)
        if calculated:
            return calculated
    return None


def _volume_grounded_reply(
    client: ClientProfile,
    catalog_result: str,
    previous_reply: str | None = None,
    line_quote: LineTotalQuote | NearestPackQuote | None = None,
) -> str | None:
    if line_quote:
        return _format_grounded_quote_reply(line_quote, client.name)
    if client.volume:
        topic = client.current_interest or client.product or ""
        calculated = line_total_catalog_result(topic, client.volume) if topic else None
        if calculated:
            return _format_grounded_quote_reply(calculated[1], client.name)
        quote = grounded_quote_reply(topic, client.name, client.volume, previous_reply)
        if quote:
            return quote
    if _catalog_has_positions(catalog_result):
        return grounded_search_reply(catalog_result, client.name, previous_reply)
    return None


def _catalog_has_positions(result: str) -> bool:
    return any("SKU:" in line for line in result.splitlines())


def _category_listing_reply(result: str, text: str = "") -> str | None:
    if not result or _catalog_has_positions(result):
        return None
    if "Подтверждённых позиций по запросу" in result:
        return None
    if "Доступные категории" not in result:
        return None
    if text and not _is_broad_assortment_utterance(text):
        return None
    headings = [
        line.strip()[2:].strip() for line in result.splitlines() if line.strip().startswith("- ")
    ]
    prefix = PRODUCT_ASSORTMENT.rstrip()
    if headings:
        return f"{prefix} Сейчас в каталоге: {', '.join(headings)}. {PRODUCT_CATEGORY_QUESTION}"
    return f"{prefix} {PRODUCT_CATEGORY_QUESTION}"


def _catalog_no_match_context(result: str | None) -> bool:
    return bool(result and "CATALOG_RESULT_EMPTY" in result and not _catalog_has_positions(result))


def _mark_catalog_result_empty(result: str | None) -> str:
    if _catalog_no_match_context(result):
        return result or CATALOG_RESULT_EMPTY_PREFIX
    return CATALOG_RESULT_EMPTY_PREFIX + "\n" + (result or "Каталог пуст.")


def _remember_catalog_no_match(client: ClientProfile, query: str | None) -> None:
    if query:
        client.catalog_no_match_query = query[:300]
        if client.product == query:
            client.product = None
        if client.current_interest == query:
            client.current_interest = None
    client.status = "уточнение продукта"
    client.needs_human = False
    client.pending_manager_question = None


def _clear_catalog_no_match(client: ClientProfile) -> None:
    client.catalog_no_match_query = None


_NON_PRODUCT_INTAKE_PHRASES = frozenset(
    {
        "ага",
        "благодарю",
        "благодарю вас",
        "большое спасибо",
        "буду ждать",
        "все понятно",
        "да",
        "договорились",
        "жду",
        "жду ответа",
        "ладно",
        "не знаю",
        "нет",
        "ок",
        "окей",
        "понял",
        "понял вас",
        "поняла",
        "поняла вас",
        "понятно",
        "принял",
        "приняла",
        "принято",
        "продолжим",
        "спасибо",
        "спасибо большое",
        "спасибо понял",
        "спасибо поняла",
        "хорошо",
        "хорошо жду",
        "ясно",
        "ясно спасибо",
    }
)


def _is_non_product_intake_text(text: str) -> bool:
    normalized = re.sub(r"[^а-яёa-z0-9]+", " ", text.lower()).strip().replace("ё", "е")
    return normalized in _NON_PRODUCT_INTAKE_PHRASES or any(
        (
            looks_like_refusal(text),
            _is_generic_fallback_reply(text),
            _is_catalog_or_price_question(text),
            asks_for_price_list(text),
            cancels_price_list(text),
            prefers_chat_here(text),
            asks_about_pending_update(text),
            is_irritated(text),
            looks_like_ready_to_buy(text),
        )
    )


def _intake_exception_product_query(client: ClientProfile, text: str) -> str | None:
    """Return only a new product statement when intake did not produce JSON."""
    if (
        not text.strip()
        or not has_contact(client)
        or client.current_interest
        or client.product
        or "?" in text
        or extract_volume(text)
        or asks_about_manufacturer(text)
        or _is_non_product_intake_text(text)
    ):
        return None
    return text.strip()


def _utterance_names_catalog_topic(text: str, result: str) -> bool:
    """True when the phrase names a category or item, not a packaging/price fragment."""
    terms = expand_query_terms(text)
    if not terms:
        return False
    for line in result.splitlines():
        if "SKU:" not in line:
            continue
        category = re.search(r"Категория:\s*([^;]+)", line)
        subcategory = re.search(r"Подкатегория:\s*([^;]+)", line)
        hay = (
            f"{category.group(1) if category else ''} {subcategory.group(1) if subcategory else ''}"
        )
        if any(term_matches_haystack(term, hay) for term in terms):
            return True
    return False


def _utterance_search_key(text: str) -> str | None:
    """Return the current phrase when it names catalog positions — not a denylist of SKUs."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    normalized = re.sub(r"[^а-яёa-z0-9]+", " ", cleaned.lower()).strip().replace("ё", "е")
    if normalized in _NON_PRODUCT_INTAKE_PHRASES:
        return None
    result = search(cleaned)
    if _catalog_has_positions(result) and _utterance_names_catalog_topic(cleaned, result):
        return cleaned
    return None


def _is_volume_only_followup(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned or not looks_like_volume(cleaned):
        return False
    if _is_catalog_or_price_question(cleaned):
        return False
    return _utterance_search_key(cleaned) is None


def _is_referential_followup(text: str) -> bool:
    """True for anaphora that points at the current topic without naming a new one."""
    cleaned = (text or "").strip()
    if not cleaned or _utterance_search_key(cleaned):
        return False
    tokens = [
        token for token in re.findall(r"[а-яёa-z0-9-]+", cleaned.lower().replace("ё", "е")) if token
    ]
    if not tokens:
        return False
    closed = {
        "а",
        "и",
        "ну",
        "же",
        "то",
        "вот",
        "сам",
        "сама",
        "само",
        "самый",
        "самая",
        "самое",
        "этот",
        "эта",
        "это",
        "эти",
        "тот",
        "та",
        "те",
        "он",
        "она",
        "оно",
        "они",
        "него",
        "нее",
        "них",
        "такой",
        "такая",
        "такое",
        "такие",
        "товар",
        "продукт",
        "позиция",
        "вариант",
    }
    return all(token in closed for token in tokens)


def _is_anaphoric_followup(text: str) -> bool:
    """Price/availability/packaging follow-up that does not name a new catalog topic."""
    cleaned = (text or "").strip()
    if not cleaned or _utterance_search_key(cleaned):
        return False
    if _is_referential_followup(cleaned):
        return True
    if asks_for_unverified_info(cleaned) or asks_about_pending_update(cleaned):
        return True
    return bool(re.search(r"\bфасовк\w*|\bупаковк\w*", cleaned.lower()))


def resolve_catalog_query(
    text: str,
    semantic: IntakeAnalysis | None,
    client: ClientProfile,
) -> tuple[str | None, str | None]:
    """Choose the catalog search key: current utterance > same-entity sticky > interest.

    The second value is the owner: utterance, semantic, sticky, interest, or None.
    Sticky no-match is only recorded for a newly named unknown entity.
    """
    if _is_broad_assortment_utterance(text):
        return "", "assortment"
    utterance = _utterance_search_key(text)
    if utterance:
        return utterance, "utterance"
    semantic_product = semantic.product.strip() if semantic and semantic.product else None
    if semantic_product and (
        _is_referential_followup(semantic_product)
        or _is_broad_assortment_utterance(semantic_product)
    ):
        semantic_product = None
    if (
        semantic_product
        and requested_identity_slot(client) not in {"phone", "email"}
        and not _is_referential_followup(text)
    ):
        return semantic_product, "semantic"
    if _is_volume_only_followup(text):
        sticky = client.catalog_no_match_query or client.current_interest or client.product
        return (sticky, "sticky") if sticky else (None, None)
    if _is_anaphoric_followup(text):
        topic = client.catalog_no_match_query or client.current_interest or client.product
        if client.catalog_no_match_query:
            return client.catalog_no_match_query, "sticky"
        return topic, "interest" if topic else None
    if client.catalog_no_match_query:
        if _is_non_product_intake_text(text) or not text.strip():
            return None, None
        return text.strip(), "utterance"
    if semantic is None:
        exception_query = _intake_exception_product_query(client, text)
        return exception_query, "semantic" if exception_query else None
    return None, None


def _repair_reply_is_grounded(reply: str, catalog_result: str) -> bool:
    if not _catalog_has_positions(catalog_result):
        return not asks_for_unverified_info(reply)
    catalog_words = {
        word.lower()
        for word in re.findall(r"[а-яёa-z0-9-]{4,}", catalog_result)
        if word.lower() not in {"категория", "подкатегория", "производитель", "фасовка"}
    }
    return any(word in reply.lower() for word in catalog_words)


def _price_token(price: str) -> str | None:
    match = re.search(r"(\d[\d\s]*)(?:[.,]\d+)?\s*(?:₽|руб)", (price or "").lower())
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)).lstrip("0") or "0"


def _competitor_price_sets() -> tuple[set[str], set[str]]:
    primary: set[str] = set()
    competitor: set[str] = set()
    for record in _all_records():
        token = _price_token(record.price)
        if not token:
            continue
        if record.is_competitor:
            competitor.add(token)
        else:
            primary.add(token)
    return primary, competitor - primary


def _text_has_price_amount(text: str, token: str) -> bool:
    return bool(re.search(rf"\b{re.escape(token)}\b\s*(?:₽|руб)", text, re.IGNORECASE))


def _reply_shows_competitor(text: str) -> bool:
    """True when the client can see a named competitor or a competitor-only price."""
    lowered = text.lower()
    _primary_prices, competitor_only = _competitor_price_sets()
    for record in _all_records():
        if not record.is_competitor:
            continue
        if record.manufacturer.lower() in lowered or record.sku.lower() in lowered:
            return True
        token = _price_token(record.price)
        if token and token in competitor_only and _text_has_price_amount(text, token):
            return True
    return False


def _line_has_primary_facts(text: str) -> bool:
    lowered = text.lower()
    primary_prices, competitor_only = _competitor_price_sets()
    for record in _all_records():
        if record.is_competitor:
            continue
        if record.manufacturer.lower() in lowered or record.sku.lower() in lowered:
            return True
        token = _price_token(record.price)
        if (
            token
            and token in primary_prices
            and token not in competitor_only
            and _text_has_price_amount(text, token)
        ):
            return True
    return False


def _erase_competitor_tokens(text: str) -> str:
    _primary_prices, competitor_only = _competitor_price_sets()
    result = text
    for record in _all_records():
        if not record.is_competitor:
            continue
        result = re.sub(re.escape(record.manufacturer), "", result, flags=re.IGNORECASE)
        result = re.sub(re.escape(record.sku), "", result, flags=re.IGNORECASE)
        token = _price_token(record.price)
        if token and token in competitor_only:
            result = re.sub(
                rf"\b{re.escape(token)}\b\s*(?:₽|руб\w*)",
                "",
                result,
                flags=re.IGNORECASE,
            )
    result = re.sub(r"(?i)производитель\s*[—\-:]?\s*(?=[,;.]|$)", "", result)
    result = re.sub(r"(?<!\d)\s*₽", "", result)
    result = re.sub(r"\s{2,}", " ", result)
    result = re.sub(r"\s+([,.;:])", r"\1", result)
    result = re.sub(r"([,;:]){2,}", r"\1", result)
    return result.strip(" ,;:-")


def _strip_competitor_exposure(text: str) -> str:
    """Remove visible competitor facts but keep grounded primary details."""
    kept: list[str] = []
    for line in text.splitlines():
        if _reply_shows_competitor(line) and not _line_has_primary_facts(line):
            continue
        cleaned = _erase_competitor_tokens(line) if _reply_shows_competitor(line) else line
        cleaned = cleaned.strip()
        if cleaned and cleaned not in {"-", "—"}:
            kept.append(cleaned)
    return "\n".join(kept).strip()


def limit_competitor_mentions(client: ClientProfile, text: str, *, allowed: bool = False) -> str:
    """Count visible competitor facts; at most two mentions, never consecutive.

    Compare-language in a primary answer is not a mention. A named competitor
    brand/SKU or a competitor-only price is. Blocked turns keep primary facts
    instead of replacing the whole reply.
    """
    if not _reply_shows_competitor(text):
        client.competitor_last_reply = False
        return text
    if not allowed or client.competitor_mentions >= 2 or client.competitor_last_reply:
        client.competitor_last_reply = False
        return _strip_competitor_exposure(text)
    client.competitor_mentions += 1
    client.competitor_last_reply = True
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
        handoff: ManagerHandoff | None = None,
    ) -> None:
        self.repository = repository
        self.ai = ai
        self.history_limit = history_limit
        self.clock = clock
        self.followup_delay = followup_delay
        self.handoff = handoff

    @staticmethod
    def should_use_ai(client: ClientProfile | None, text: str) -> bool:
        """Let Telegram show typing only while an actual AI request is in flight."""
        if text.strip().lower() == "/start":
            return False
        if cancels_price_list(text):
            return False
        if asks_for_price_list(text):
            return False
        if (
            client
            and client.price_list_requested
            and (normalize_email(text) or _mentions_chat_here(text))
        ):
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
        # The limiter clears this flag only after a substantive non-competitor reply.

        if text.lower() in {"/start", "start", "начать"}:
            return await self._handle_start(client, message.text, now)

        invalid_phone = invalid_phone_length(message.text)
        phone_attempt = phone_digit_attempt(message.contact_phone or message.text)
        landline = normalize_landline(message.contact_phone or text)
        if client.price_list_requested and not asks_for_price_list_in_chat(text):
            email = normalize_email(text)
            if email:
                client.email = email
                reply_text = (
                    f"Спасибо, {client.name}. Почту записал." if client.name else NAME_QUESTION
                )
                return await self._finish(
                    client,
                    message.text,
                    BotReply(reply_text),
                    now,
                )
        if (
            requested_identity_slot(client) == "phone"
            and phone_attempt
            and landline
            and not message.contact_phone
            and not looks_like_volume(text)
        ):
            client.landline = landline
            client.phone_correction_pending = True
            client.status = "ожидает телефон"
            return await self._finish(
                client,
                message.text,
                BotReply("Городской номер сохранил. Теперь пришлите, пожалуйста, мобильный номер."),
                now,
            )
        if client.name and not client.phone and phone_attempt and not invalid_phone:
            if normalize_phone(message.contact_phone or text):
                invalid_phone = None
            else:
                client.phone_correction_pending = True
                return await self._finish(
                    client,
                    message.text,
                    BotReply("Не получилось распознать номер, проверьте.", delay=False),
                    now,
                )
        if client.name and not client.phone and invalid_phone:
            client.phone_correction_pending = True
            history = await self.repository.get_history(client.telegram_id, self.history_limit)
            signal = f"[STRUCTURED_SIGNAL invalid_phone_length={invalid_phone[0]} direction={'short' if invalid_phone[1] == 'short' else 'long'}]"
            await self._safe_respond(client, history, f"{message.text} {signal}")
            wording = (
                "Похоже, в номере не хватает одной цифры, проверьте и отправьте ещё раз."
                if invalid_phone[1] == "short"
                else "Похоже, в номере лишняя цифра, проверьте и отправьте ещё раз."
            )
            return await self._finish(client, message.text, BotReply(wording), now)

        if client.name and not client.phone:
            deterministic_phone = normalize_phone(message.contact_phone or text)
            if deterministic_phone:
                client.phone_correction_pending = False
                client.phone = deterministic_phone
                client.contact_skipped = False
                if client.status == "готов к заказу" or _has_known_order(client):
                    client.status = "готов к заказу"
                    if not client.fulfillment_channel:
                        client.fulfillment_channel = CHANNEL_CALL
                    if client.requested_slot:
                        handoff_id = await self._create_handoff("call", client)
                        client.handoff_id = handoff_id
                        return await self._finish(
                            client,
                            message.text,
                            BotReply(call_slot_reply(client.requested_slot, handoff_id=handoff_id)),
                            now,
                        )
                    return await self._finish(client, message.text, BotReply(CLOSE_ASK_TIME), now)
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
        pending_price_list = client.price_list_requested or (
            not client.price_list_sent_at and _price_list_pending_from_history(history)
        )
        if cancels_price_list(text):
            client.price_list_requested = False
            return await self._finish(
                client,
                message.text,
                BotReply("Хорошо, без прайса."),
                now,
            )
        if asks_for_price_list_in_chat(text) or (pending_price_list and _mentions_chat_here(text)):
            client.price_list_requested = True
            return await self._finish(
                client,
                message.text,
                BotReply(
                    PRICE_LIST_CHAT_REPLY,
                    delay=False,
                    attachment_content=generated_price_list(),
                    attachment_filename=PRICE_LIST_FILENAME,
                ),
                now,
            )
        if asks_for_price_list(text):
            client.price_list_requested = True
            if client.name and not has_contact(client):
                client.status = "ожидает почту"
            return await self._finish(
                client,
                message.text,
                BotReply(EMAIL_QUESTION, delay=False),
                now,
            )
        if self._is_fulfillment_turn(client, text):
            return await self._handle_fulfillment(client, text, now)
        semantic = await self._safe_analyze(client, history, message.text)
        invalid_phone = invalid_phone_length(message.text)
        if invalid_phone and semantic is not None:
            semantic = IntakeAnalysis(
                intent=semantic.intent,
                name=semantic.name,
                phone=None,
                product=semantic.product,
                volume=semantic.volume,
                budget=semantic.budget,
                unit_price_request=semantic.unit_price_request,
                target_product=semantic.target_product,
                invalid_phone_length=invalid_phone[0],
                invalid_phone_direction=invalid_phone[1],
                reply=semantic.reply,
            )
        if invalid_phone:
            direction = invalid_phone[1]
            signal = (
                f"[STRUCTURED_SIGNAL invalid_phone_length={invalid_phone[0]} direction={direction}]"
            )
            turn = await self._safe_respond(client, history, f"{message.text} {signal}")
            if turn and is_valid_ai_reply(turn.reply):
                return await self._finish(client, message.text, BotReply(turn.reply.strip()), now)
            wording = "не хватает цифры" if direction == "short" else "лишняя цифра"
            return await self._finish(
                client,
                message.text,
                BotReply(f"Кажется, {wording}. Пришлите, пожалуйста, номер ещё раз."),
                now,
            )
        catalog_query, query_owner = resolve_catalog_query(text, semantic, client)
        preliminary_catalog_result = None
        catalog_no_match = False
        if catalog_query is not None:
            preliminary_catalog_result = (
                search(catalog_query, include_competitors=True)
                if asks_for_competitor(text)
                else search(catalog_query)
            )
            if not _catalog_has_positions(preliminary_catalog_result):
                if query_owner in {"utterance", "semantic", "sticky"}:
                    preliminary_catalog_result = _mark_catalog_result_empty(
                        preliminary_catalog_result
                    )
                    catalog_no_match = True
                    _remember_catalog_no_match(client, catalog_query)
            elif client.catalog_no_match_query:
                _clear_catalog_no_match(client)

        captured = self._apply_intake_facts(
            client,
            semantic,
            message.text,
            allow_catalog_facts=not catalog_no_match,
        )
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

        if asks_about_assortment(text) and _asks_about_delivery(text):
            return await self._finish(
                client,
                message.text,
                BotReply(
                    PRODUCT_ASSORTMENT
                    + "Да, доставка есть. По Пензе она бесплатная при заказе от 50 000 ₽."
                ),
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

        calculated_composite = composite_line_total_catalog_result(text)
        composite_quote = calculated_composite[1] if calculated_composite else None
        composite_catalog = (
            calculated_composite[0] if calculated_composite else _composite_order_catalog(text)
        )
        competitor_request = asks_for_competitor(text)
        if catalog_query is not None:
            search_key = catalog_query
        else:
            search_key = _utterance_search_key(text)
        if not search_key and catalog_query is None:
            if _is_anaphoric_followup(text) or _is_volume_only_followup(text):
                search_key = client.current_interest or client.product
            elif _is_catalog_or_price_question(text):
                search_key = client.current_interest or client.product or text
            elif client.current_interest or client.product:
                search_key = client.current_interest or client.product
        catalog_result = (
            composite_catalog
            if composite_catalog
            else preliminary_catalog_result
            if preliminary_catalog_result is not None
            else (
                search(search_key, include_competitors=True)
                if competitor_request
                else search(search_key)
            )
            if search_key is not None
            else None
        )
        unit_quote = None
        requested_unit = (
            semantic.unit_price_request if semantic else None
        ) or _infer_unit_price_request(text)
        text_quantity = parse_requested_quantity(text)
        order_in_text = bool(
            text_quantity
            and not _volume_is_unit_price_probe(
                text, text_quantity.raw, requested_unit or text_quantity.unit
            )
        )
        if requested_unit and not order_in_text:
            utterance_target = _utterance_search_key(text)
            target = (
                (semantic.target_product if semantic else None)
                or (semantic.product if semantic else None)
                or utterance_target
                or catalog_query
            )
            if not target or (
                target in {client.product, client.current_interest}
                and not (semantic and (semantic.target_product or semantic.product))
                and not utterance_target
            ):
                recovered = recover_product_from_history(
                    "\n".join(f"{row.user_message}\n{row.assistant_message}" for row in history),
                    client.product,
                )
                if recovered:
                    target = recovered
            if not target:
                target = client.current_interest or client.product
            if target:
                calculated = unit_price_catalog_result(target, requested_unit)
                if calculated:
                    catalog_result, unit_quote = calculated
                    client.current_interest = unit_quote.record.subcategory[:300]

        line_quote = None
        if unit_quote is None and composite_quote is None:
            calculated_line = _resolve_line_total_catalog(text, semantic, client, catalog_query)
            if calculated_line:
                catalog_result, line_quote = calculated_line
                client.current_interest = line_quote.record.subcategory[:300]

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

        if composite_catalog:
            return await self._handle_ai(
                client,
                message.text,
                now,
                composite_catalog,
                line_quote=line_quote,
                composite_quote=composite_quote,
            )

        if catalog_no_match:
            return await self._handle_ai(client, message.text, now, catalog_result)

        if is_qualified(client):
            if client.status not in {"готов к заказу", "получил предложение"}:
                client.status = "квалифицирован"
            await self.repository.save_client(client)
            return await self._handle_ai(
                client,
                message.text,
                now,
                catalog_result,
                unit_quote,
                line_quote,
                composite_quote,
            )

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
        rejection_reason = _reject_turn(turn, catalog_result, client)
        if (unit_quote or line_quote) and turn and _is_generic_fallback_reply(turn.reply):
            rejection_reason = "invalid_reply"
        if rejection_reason is None and turn is not None:
            self._remember_catalog_interest(client, catalog_result, turn.reply, message.text)
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
        repair_reason = _reject_turn(repair, catalog_result, client)
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
        open_turn = await self._safe_open_dialog(
            client, history, message.text, rejection_reason or "invalid_reply", catalog_result or ""
        )
        if open_turn and _reject_turn(open_turn, catalog_result, client) is None:
            self._apply_turn_facts(client, open_turn, message.text)
            return await self._finish(client, message.text, BotReply(open_turn.reply.strip()), now)
        deterministic_recovery = _deterministic_recovery_reply(text, catalog_result or "")
        if deterministic_recovery:
            return await self._finish(client, message.text, BotReply(deterministic_recovery), now)

        if unit_quote:
            record = unit_quote.record
            reply = (
                f"{record.subcategory}: {unit_quote.unit_price} "
                f"({record.packaging}, {record.price} за упаковку)."
            )
            return await self._finish(client, message.text, BotReply(reply), now)
        if composite_quote:
            return await self._finish(
                client,
                message.text,
                BotReply(_format_composite_reply(composite_quote, client.name)),
                now,
            )
        if line_quote:
            return await self._finish(
                client,
                message.text,
                BotReply(_format_grounded_quote_reply(line_quote, client.name)),
                now,
            )

        listing = _category_listing_reply(catalog_result or "", text)
        if listing:
            return await self._finish(client, message.text, BotReply(listing, delay=False), now)

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
        client.needs_human = True
        client.pending_manager_question = message.text[:500]
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

    async def _safe_open_dialog(
        self,
        client: ClientProfile,
        history: list[HistoryEntry],
        message: str,
        reason: str,
        catalog_result: str,
    ) -> AiTurn | None:
        open_dialog = getattr(self.ai, "open_dialog", None)
        if not callable(open_dialog):
            return None
        try:
            return await open_dialog(client, history, message, reason, catalog_result)
        except Exception:
            logger.exception(
                "DeepSeek open-dialog failed for telegram_id=%s reason=open_dialog_exception",
                client.telegram_id,
            )
            return None

    def _apply_intake_facts(
        self,
        client: ClientProfile,
        semantic: IntakeAnalysis | None,
        text: str = "",
        *,
        allow_catalog_facts: bool = True,
    ) -> bool:
        captured = False
        intent = semantic.intent if semantic else None
        parsed = None
        if intent in _CAPTURE_INTENTS:
            name_source = semantic.name if semantic and semantic.name else None
            if name_source:
                parsed = parse_person_name(name_source)
            elif requested_identity_slot(client) == "name":
                parsed = parse_person_name(text)
            candidate = name_source or text
            if parsed and not _is_person_name_value(candidate):
                parsed = None
        can_read_semantic = bool(semantic and (intent in _CAPTURE_INTENTS or intent == "question"))
        if parsed and (not client.name or intent == "correction"):
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
        if (
            allow_catalog_facts
            and can_read_semantic
            and _may_write_commercial_facts(client)
            and semantic
            and semantic.product
            and not _is_broad_assortment_utterance(semantic.product)
            and not _is_broad_assortment_utterance(text)
        ):
            if client.product and client.product != semantic.product:
                client.original_interests = list(client.original_interests or [client.product])
            client.current_interest = semantic.product[:300]
            client.product = semantic.product[:300]
            if client.name:
                client.status = "уточнение объёма"
            captured = True
        volume = extract_volume(text)
        if (
            not volume
            and can_read_semantic
            and semantic.volume
            and looks_like_volume(semantic.volume)
            and _may_write_commercial_facts(client)
        ):
            volume = semantic.volume.strip()[:300]
        if _looks_like_packaging_fragment(text) or (
            volume is not None and _looks_like_packaging_fragment(volume)
        ):
            volume = None
        unit_request = (
            semantic.unit_price_request if semantic else None
        ) or _infer_unit_price_request(text)
        if _volume_is_unit_price_probe(text, volume, unit_request):
            volume = None
        if allow_catalog_facts and volume and not client.volume:
            client.volume = volume[:300]
            captured = True
            if (
                has_contact(client)
                and requested_identity_slot(client) is None
                and client.status not in {"готов к заказу", "получил предложение"}
            ):
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
        if not client.name:
            return NAME_QUESTION
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
            if client.phone_correction_pending:
                return "Хорошо, жду корректный номер, когда будете готовы."
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

    @staticmethod
    def _is_fulfillment_turn(client: ClientProfile, text: str) -> bool:
        if looks_like_pickup_choice(text) or looks_like_pickup_question(text):
            return True
        if looks_like_call_request(text):
            return True
        closing = client.status == "готов к заказу" or bool(client.fulfillment_channel)
        return closing and bool(parse_time_slot(text) or looks_like_call_time(text))

    async def _handle_fulfillment(
        self, client: ClientProfile, user_message: str, now: datetime
    ) -> BotReply:
        text = user_message.strip()
        if looks_like_pickup_question(text):
            return await self._finish(client, user_message, BotReply(pickup_info_reply()), now)
        if looks_like_pickup_choice(text):
            client.fulfillment_channel = CHANNEL_PICKUP
            client.status = "готов к заказу"
            return await self._finish(client, user_message, BotReply(pickup_choice_reply()), now)
        if (
            client.fulfillment_channel == CHANNEL_PICKUP
            and looks_like_call_request(text)
            and not looks_like_pickup_rejection(text)
        ):
            return await self._finish(client, user_message, BotReply(channel_clarify_reply()), now)
        if looks_like_call_request(text) or looks_like_pickup_rejection(text):
            client.fulfillment_channel = CHANNEL_CALL
        slot = parse_time_slot(text)
        if client.fulfillment_channel == CHANNEL_PICKUP:
            if slot:
                client.requested_slot = slot
                client.status = "готов к заказу"
                reply = visit_slot_reply(slot)
            else:
                reply = pickup_choice_reply()
            return await self._finish(client, user_message, BotReply(reply), now)
        if not client.phone:
            client.status = "готов к заказу"
            if not client.fulfillment_channel:
                client.fulfillment_channel = CHANNEL_CALL
            return await self._finish(client, user_message, BotReply(CLOSE_NEED_PHONE), now)
        client.fulfillment_channel = client.fulfillment_channel or CHANNEL_CALL
        client.status = "готов к заказу"
        if not slot:
            return await self._finish(client, user_message, BotReply(CLOSE_ASK_TIME), now)
        client.requested_slot = slot
        handoff_id = await self._create_handoff("call", client)
        client.handoff_id = handoff_id
        return await self._finish(
            client,
            user_message,
            BotReply(call_slot_reply(slot, handoff_id=handoff_id)),
            now,
        )

    async def _create_handoff(self, kind: str, client: ClientProfile) -> str | None:
        if self.handoff is None:
            return None
        payload = {
            "telegram_id": client.telegram_id,
            "channel": client.fulfillment_channel,
            "slot": client.requested_slot,
            "phone": client.phone,
            "product": client.product,
        }
        try:
            return await self.handoff.create(kind, payload)
        except Exception:
            logger.exception("Manager handoff failed for telegram_id=%s", client.telegram_id)
            return None

    async def _handle_closing(
        self,
        client: ClientProfile,
        history: list[HistoryEntry],
        user_message: str,
        now: datetime,
    ) -> BotReply:
        client.status = "готов к заказу"
        if client.phone and not client.fulfillment_channel:
            client.fulfillment_channel = CHANNEL_CALL
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
        unit_quote: UnitPriceQuote | None = None,
        line_quote: LineTotalQuote | NearestPackQuote | None = None,
        composite_quote: CompositeLineTotals | None = None,
    ) -> BotReply:
        history = await self.repository.get_history(client.telegram_id, self.history_limit)
        catalog_no_match = _catalog_no_match_context(catalog_result)
        turn = await self._safe_respond(client, history, user_message, catalog_result)
        if not catalog_no_match:
            self._apply_turn_facts(
                client, turn or AiTurn(reply="", needs_human=False), user_message
            )
        if (
            turn is not None
            and _reject_turn(turn, catalog_result, client) is None
            and not catalog_no_match
        ):
            self._remember_catalog_interest(client, catalog_result, turn.reply, user_message)
        rejection_reason = _reject_turn(turn, catalog_result, client)
        if (unit_quote or line_quote) and turn and _is_generic_fallback_reply(turn.reply):
            rejection_reason = "invalid_reply"
        if rejection_reason is not None:
            if (
                turn
                and turn.needs_human
                and not unit_quote
                and not line_quote
                and not catalog_no_match
            ):
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
            repair_reason = _reject_turn(repair, catalog_result, client)
            if (
                repair_reason is None
                and repair is not None
                and _repair_reply_is_grounded(repair.reply, catalog_result or "")
            ):
                return await self._finish(
                    client,
                    user_message,
                    BotReply(repair.reply.strip(), delay=False),
                    now,
                )
            if repair is not None:
                logger.warning(
                    "Rejected repair reply for telegram_id=%s reason=%s needs_human=%s",
                    client.telegram_id,
                    repair_reason or "not_grounded",
                    repair.needs_human,
                )
            open_turn = await self._safe_open_dialog(
                client, history, user_message, rejection_reason, catalog_result or ""
            )
            if open_turn and _reject_turn(open_turn, catalog_result, client) is None:
                if not catalog_no_match:
                    self._apply_turn_facts(client, open_turn, user_message)
                return await self._finish(
                    client,
                    user_message,
                    BotReply(open_turn.reply.strip(), delay=False),
                    now,
                )
            if catalog_no_match:
                _remember_catalog_no_match(client, client.catalog_no_match_query)
                return await self._finish(
                    client,
                    user_message,
                    BotReply(CATALOG_NO_MATCH_REPLY, delay=False),
                    now,
                )
            deterministic_recovery = _deterministic_recovery_reply(
                user_message, catalog_result or ""
            )
            if deterministic_recovery:
                return await self._finish(
                    client, user_message, BotReply(deterministic_recovery, delay=False), now
                )
            if unit_quote:
                record = unit_quote.record
                reply = (
                    f"{record.subcategory}: {unit_quote.unit_price} "
                    f"({record.packaging}, {record.price} за упаковку)."
                )
                return await self._finish(client, user_message, BotReply(reply), now)
            if composite_quote:
                return await self._finish(
                    client,
                    user_message,
                    BotReply(_format_composite_reply(composite_quote, client.name), delay=False),
                    now,
                )
            if line_quote:
                return await self._finish(
                    client,
                    user_message,
                    BotReply(_format_grounded_quote_reply(line_quote, client.name), delay=False),
                    now,
                )
            listing = _category_listing_reply(catalog_result or "", user_message)
            if listing:
                return await self._finish(client, user_message, BotReply(listing, delay=False), now)
            if _catalog_has_positions(catalog_result or ""):
                recovery = await self._safe_repair(
                    client, history, user_message, "recovery_attempt_2", catalog_result or ""
                )
                if (
                    recovery
                    and _reject_turn(recovery, catalog_result, client) is None
                    and _repair_reply_is_grounded(recovery.reply, catalog_result or "")
                ):
                    return await self._finish(
                        client, user_message, BotReply(recovery.reply.strip(), delay=False), now
                    )
                if unit_quote:
                    record = unit_quote.record
                    reply = (
                        f"{record.subcategory}: {unit_quote.unit_price} "
                        f"({record.packaging}, {record.price} за упаковку)."
                    )
                    return await self._finish(client, user_message, BotReply(reply), now)
                client.needs_human = True
                client.pending_manager_question = user_message[:500]
                if not callable(
                    getattr(self.ai, "repair_response", None)
                ) and rejection_reason not in {"unsafe_reply", "invalid_reply"}:
                    catalog_reply = grounded_search_reply(
                        catalog_result or "",
                        client.name,
                        history[-1].assistant_message if history else None,
                    )
                    if catalog_reply:
                        return await self._finish(
                            client, user_message, BotReply(catalog_reply, delay=False), now
                        )
                if not callable(getattr(self.ai, "repair_response", None)):
                    if rejection_reason == "invalid_reply":
                        quote = _volume_grounded_reply(
                            client,
                            catalog_result or "",
                            history[-1].assistant_message if history else None,
                        )
                        if quote:
                            return await self._finish(
                                client, user_message, BotReply(quote, delay=False), now
                            )
                    return await self._finish(
                        client,
                        user_message,
                        BotReply(
                            "Сейчас не могу подтвердить этот факт; вопрос зафиксирован для менеджера."
                        ),
                        now,
                    )
                if rejection_reason == "invalid_reply":
                    quote = _volume_grounded_reply(
                        client,
                        catalog_result or "",
                        history[-1].assistant_message if history else None,
                    )
                    if quote:
                        return await self._finish(
                            client, user_message, BotReply(quote, delay=False), now
                        )
                return await self._finish(
                    client,
                    user_message,
                    BotReply("Сейчас не могу подтвердить ответ по этому вопросу по каталогу."),
                    now,
                )
            reply = (
                CATALOG_NO_MATCH_REPLY
                if catalog_result and "CATALOG_RESULT_EMPTY" in catalog_result
                else "Сейчас не могу подтвердить этот факт; вопрос зафиксирован для менеджера."
            )
            client.comment = "Нужен ответ менеджера"
            client.needs_human = True
            client.pending_manager_question = user_message[:500]
            return await self._finish(client, user_message, BotReply(reply, delay=False), now)
        if catalog_no_match:
            return await self._finish(
                client,
                user_message,
                BotReply((turn.reply if turn else CATALOG_NO_MATCH_REPLY).strip(), delay=True),
                now,
            )
        return await self._finish(
            client, user_message, BotReply(turn.reply.strip(), delay=True), now
        )

    @staticmethod
    def _snapshot_original_topic(
        client: ClientProfile, previous: str | None, incoming: str
    ) -> None:
        if not previous or previous == incoming:
            return
        originals = list(client.original_interests or [])
        if previous not in originals:
            originals.append(previous)
        if client.product and client.product != incoming and client.product not in originals:
            originals.append(client.product)
        client.original_interests = originals

    @staticmethod
    def _remember_catalog_interest(
        client: ClientProfile,
        catalog_result: str | None,
        reply: str,
        query: str | None = None,
    ) -> None:
        if not catalog_result:
            return
        interest = (
            named_catalog_item(catalog_result, query or "")
            or named_catalog_item(catalog_result, reply)
            or infer_catalog_interest(catalog_result, reply)
        )
        if not interest:
            return
        current = client.current_interest
        if current and current.lower() != interest.lower() and current.lower() in interest.lower():
            interest = current
        previous = current or client.product
        ConversationService._snapshot_original_topic(client, previous, interest)
        client.current_interest = interest[:300]
        if not has_contact(client):
            return
        categories = catalog_categories_in_result(catalog_result)
        product_is_category = bool(client.product) and client.product.lower() in categories
        if not client.product or product_is_category:
            client.product = interest[:300]
            if client.status == "уточнение продукта":
                client.status = "уточнение объёма"

    @staticmethod
    def _apply_turn_facts(client: ClientProfile, turn: AiTurn, user_message: str = "") -> None:
        if turn.product and not _is_broad_assortment_utterance(turn.product):
            ConversationService._snapshot_original_topic(client, client.product, turn.product)
            client.current_interest = turn.product[:300]
            client.product = turn.product[:300]
        if (
            turn.volume
            and looks_like_volume(turn.volume)
            and not _looks_like_packaging_fragment(turn.volume)
            and not _volume_is_unit_price_probe(
                user_message, turn.volume, _infer_unit_price_request(user_message)
            )
        ):
            client.volume = turn.volume[:300]
        if is_qualified(client) and client.status not in {"готов к заказу", "получил предложение"}:
            client.status = "квалифицирован"

    async def mark_price_list_sent(self, telegram_id: int, sent_at: datetime | None = None) -> None:
        client = await self.repository.get_client(telegram_id)
        if client is None:
            return
        client.price_list_requested = False
        client.price_list_sent_at = sent_at or self.clock()
        await self.repository.save_client(client)

    async def _finish(
        self,
        client: ClientProfile,
        user_message: str,
        reply: BotReply,
        now: datetime,
    ) -> BotReply:
        apply_followup_rules(client, user_message, reply.text, now, self.followup_delay)
        safe_text = limit_competitor_mentions(
            client, reply.text, allowed=asks_for_competitor(user_message)
        )
        reply = BotReply(
            safe_text,
            request_contact=reply.request_contact,
            delay=reply.delay,
            attachment_content=reply.attachment_content,
            attachment_filename=reply.attachment_filename,
        )
        if reply_quoted_price(reply.text) and client.status != "готов к заказу":
            client.status = "получил предложение"
        await self.repository.save_client(client)
        await self.repository.append_history(client.telegram_id, now, user_message, reply.text)
        return reply
