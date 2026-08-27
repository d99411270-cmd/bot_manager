from __future__ import annotations

import random
import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from .models import ClientProfile, HistoryEntry

FOLLOWUP_DELAY = timedelta(hours=1)
FOLLOWUP_TEXT = "Вы подумали по цене? Если есть сомнения — напишите, в чём затык, если не секрет."
PRICE_LIST_FOLLOWUP_TEXT = "Успели посмотреть прайс? Напишите, что заинтересовало."


def followup_delay(rng=None) -> timedelta:
    picker = rng if rng is not None else random
    return timedelta(minutes=picker.randint(55, 66))


def looks_like_thinking(text: str) -> bool:
    lowered = text.strip().lower()
    return bool(
        re.search(
            r"подума|посовет|сориентир|вернус|надо подумать|я подумаю",
            lowered,
        )
    )


def reply_quoted_price(text: str) -> bool:
    return bool(re.search(r"\d[\d\s]*\s*(?:₽|руб)", text))


def reply_promised_price_list(text: str) -> bool:
    lowered = (text or "").lower()
    return "почту записал" in lowered and "прайс" in lowered


def _history_promised_price_list(history: Sequence[HistoryEntry] | None) -> bool:
    if not history:
        return False
    return reply_promised_price_list(history[-1].assistant_message or "")


def followup_text(client: ClientProfile, history: Sequence[HistoryEntry] | None = None) -> str:
    body = PRICE_LIST_FOLLOWUP_TEXT if _history_promised_price_list(history) else FOLLOWUP_TEXT
    if client.name:
        return f"{client.name}, {body[0].lower() + body[1:]}"
    return body


def due_for_followup(client: ClientProfile, now: datetime) -> bool:
    return bool(
        client.followup_due_at and not client.followup_sent and client.followup_due_at <= now
    )


def apply_followup_rules(
    client: ClientProfile,
    user_message: str,
    bot_reply: str,
    now: datetime,
    delay: timedelta | None = None,
    rng=None,
) -> None:
    text = user_message.strip().lower()
    if text in {"/start", "start", "начать"}:
        client.followup_due_at = None
        client.followup_sent = False
        return
    if (
        looks_like_thinking(user_message)
        or reply_quoted_price(bot_reply)
        or reply_promised_price_list(bot_reply)
    ):
        wait = delay if delay is not None else followup_delay(rng)
        client.followup_due_at = now + wait
        client.followup_sent = False
        if looks_like_thinking(user_message):
            client.status = "ожидает решение"
        return
    if client.followup_due_at and not client.followup_sent:
        client.followup_due_at = None
