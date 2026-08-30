from __future__ import annotations

import re

from .models import ClientProfile

CLOSE_NEED_PHONE = (
    "Чтобы оформить заказ, нужен короткий звонок с менеджером. "
    "Это наша процедура. Оставьте, пожалуйста, номер телефона."
)
CLOSE_ASK_TIME = "Отлично. Чтобы оформить заказ, нужен созвон с менеджером. Во сколько вам удобно?"
PENZA_DELIVERY_PROMO = "Если заказ от 50 000 ₽, доставка по Пензе бесплатная."
PENZA_PROMO_AMOUNTS = {"50000"}

CHANNEL_CALL = "call"
CHANNEL_PICKUP = "pickup"
PICKUP_ADDRESS = "г. Пенза, ул. Аустрина, 137, корп. 2"
CLOSE_CONFIRM_PICKUP = f"Самовывоз есть: {PICKUP_ADDRESS}. Во сколько вам удобно приехать?"
CLOSE_PICKUP_INFO = (
    f"Самовывоз есть, адрес: {PICKUP_ADDRESS}. "
    "Подтверждённого окна на завтра нет — напишите удобное время визита."
)
CLOSE_ASK_CHANNEL = "Оставим самовывоз или лучше короткий звонок? Напишите, что удобнее."


def looks_like_ready_to_buy(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"готов(?:лю|ы)?\s+(?:брать|купить|забрать|оформ)|"
            r"\bберу\b|\bпокупаю\b|оформляем|давайте заказ|давайте оформим|"
            r"все беру|всё беру|всё оформл|все оформл|можно оформл",
            lowered,
        )
    )


_ACK_TOKENS = frozenset(
    {
        "ага",
        "благодарю",
        "все",
        "договорились",
        "ладно",
        "ок",
        "окей",
        "понял",
        "поняла",
        "понятно",
        "принял",
        "приняла",
        "принято",
        "спасибо",
        "хорошо",
        "ясно",
    }
)


def looks_like_acknowledgment(text: str) -> bool:
    """Thanks/understood/done with no new product, volume, or channel."""
    tokens = re.findall(r"[а-яёa-z]+", (text or "").lower().replace("ё", "е"))
    return bool(tokens) and all(token in _ACK_TOKENS for token in tokens)


def looks_like_call_time(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"\b(?:час|утра|дня|вечер|завтра|после|в\s+\d{1,2})\b|\d{1,2}\s*:\s*\d{2}",
            lowered,
        )
    )


def asks_about_time_slot(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(re.search(r"во сколько|на какое время|когда (?:звон|приез|удобн)", lowered))


def looks_like_pickup_question(text: str) -> bool:
    lowered = text.strip().lower()
    if "самовывоз" not in lowered:
        return False
    if looks_like_pickup_rejection(text):
        return False
    return bool(re.search(r"\?|\bесть\b|\bможно\b", lowered))


def looks_like_pickup_rejection(text: str) -> bool:
    lowered = text.strip().lower().replace("ё", "е")
    return bool(
        re.search(
            r"самовывоз\s+не\s+(?:надо|нужен|нужно|хочу)|"
            r"не\s+(?:надо|нужен|нужно)\s+самовывоз|"
            r"без\s+самовывоз|"
            r"не\s+самовывоз|"
            r"не\s+приеду|"
            r"на\s+склад\s+не|"
            r"не\s+визит",
            lowered,
        )
    )


def looks_like_pickup_choice(text: str) -> bool:
    lowered = text.strip().lower()
    if looks_like_pickup_question(text) or looks_like_pickup_rejection(text):
        return False
    return bool(re.search(r"самовывоз|заберу сам|сам заберу|приеду сам", lowered))


def looks_like_call_request(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(re.search(r"\b(?:звоните|позвоните|перезвоните|созвон(?:имся)?|звонок)\b", lowered))


def parse_time_slot(text: str) -> str | None:
    lowered = text.lower().replace("ё", "е")
    match = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", lowered)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    match = re.search(r"(?:часов?\s+в|в\s+|после\s+)(\d{1,2})\b", lowered)
    if match:
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            return f"{hour:02d}:00"
    return None


def closing_reply(client: ClientProfile, user_text: str) -> str | None:
    if not looks_like_ready_to_buy(user_text):
        return None
    if not client.phone:
        return CLOSE_NEED_PHONE
    return CLOSE_ASK_TIME


def pickup_info_reply() -> str:
    return CLOSE_PICKUP_INFO


def pickup_choice_reply() -> str:
    return CLOSE_CONFIRM_PICKUP


def visit_slot_reply(slot: str) -> str:
    return f"Хорошо, жду вас в {slot} по адресу {PICKUP_ADDRESS}."


def channel_clarify_reply() -> str:
    return CLOSE_ASK_CHANNEL


def call_slot_reply(slot: str, *, handoff_id: str | None) -> str:
    if handoff_id:
        return f"Договорились, позвоним в {slot}."
    return f"Записал удобное время: {slot}."
