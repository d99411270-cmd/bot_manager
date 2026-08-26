from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(slots=True)
class ClientProfile:
    telegram_id: int
    username: str | None = None
    name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    landline: str | None = None
    email: str | None = None
    product: str | None = None
    volume: str | None = None
    budget: int | None = None
    status: str = "новый"
    first_contact_at: datetime | None = None
    last_contact_at: datetime | None = None
    comment: str | None = None
    contact_skipped: bool = False
    followup_due_at: datetime | None = None
    followup_sent: bool = False
    original_interests: list[str] | None = None
    current_interest: str | None = None
    needs_human: bool = False
    pending_manager_question: str | None = None
    competitor_mentions: int = 0
    competitor_last_reply: bool = False
    phone_correction_pending: bool = False
    price_list_requested: bool = False
    price_list_sent_at: datetime | None = None
    catalog_no_match_query: str | None = None


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
    attachment_content: str | None = None
    attachment_filename: str | None = None


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
    budget: int | None = None
    unit_price_request: str | None = None
    target_product: str | None = None
    invalid_phone_length: int | None = None
    invalid_phone_direction: str | None = None
    reply: str | None = None


@dataclass(slots=True, frozen=True)
class FollowupPlan:
    appropriate: bool
    reply: str | None = None
