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


def looks_like_call_time(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"\b(?:час|утра|дня|вечер|завтра|после|в\s+\d{1,2})\b|\d{1,2}\s*:\s*\d{2}",
            lowered,
        )
    )


def closing_reply(client: ClientProfile, user_text: str) -> str | None:
    if not looks_like_ready_to_buy(user_text):
        return None
    if not client.phone:
        return CLOSE_NEED_PHONE
    return CLOSE_ASK_TIME
