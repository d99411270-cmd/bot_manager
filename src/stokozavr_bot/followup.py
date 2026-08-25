from __future__ import annotations

import re
from datetime import datetime, timedelta

from .models import ClientProfile

FOLLOWUP_DELAY = timedelta(hours=1)
FOLLOWUP_TEXT = "Вы подумали по цене? Если есть сомнения — напишите, в чём затык, если не секрет."


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


def followup_text(client: ClientProfile) -> str:
    if client.name:
        return f"{client.name}, {FOLLOWUP_TEXT[0].lower() + FOLLOWUP_TEXT[1:]}"
    return FOLLOWUP_TEXT


def due_for_followup(client: ClientProfile, now: datetime) -> bool:
    return bool(
        client.followup_due_at and not client.followup_sent and client.followup_due_at <= now
    )


def apply_followup_rules(
    client: ClientProfile,
    user_message: str,
    bot_reply: str,
    now: datetime,
    delay: timedelta = FOLLOWUP_DELAY,
) -> None:
    text = user_message.strip().lower()
    if text in {"/start", "start", "начать"}:
        client.followup_due_at = None
        client.followup_sent = False
        return
    if looks_like_thinking(user_message) or reply_quoted_price(bot_reply):
        client.followup_due_at = now + delay
        client.followup_sent = False
        if looks_like_thinking(user_message):
            client.status = "ожидает решение"
        return
    if client.followup_due_at and not client.followup_sent:
        client.followup_due_at = None
