from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(slots=True)
class ClientProfile:
    telegram_id: int
    username: str | None = None
    name: str | None = None
    phone: str | None = None
    product: str | None = None
    volume: str | None = None
    status: str = "новый"
    first_contact_at: datetime | None = None
    last_contact_at: datetime | None = None
    comment: str | None = None


@dataclass(slots=True)
class HistoryEntry:
    created_at: datetime
    telegram_id: int
    user_message: str
    assistant_message: str


@dataclass(slots=True)
class IncomingMessage:
    telegram_id: int
    username: str | None
    text: str
    contact_phone: str | None = None


@dataclass(slots=True)
class BotReply:
    text: str
    request_contact: bool = False
    delay: bool = False


@dataclass(slots=True)
class AiTurn:
    reply: str
    product: str | None = None
    volume: str | None = None
    needs_human: bool = False


@dataclass(slots=True, frozen=True)
class IntakeAnalysis:
    intent: Literal["provide_data", "refusal", "question", "greeting", "offtopic", "correction"]
    name: str | None = None
    phone: str | None = None
    product: str | None = None
    volume: str | None = None
    reply: str | None = None
