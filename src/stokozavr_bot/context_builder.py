from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .models import ClientProfile, HistoryEntry
from .sales_state import infer_deal_stage

_PROFILE_FIELDS = (
    "name",
    "last_name",
    "phone",
    "email",
    "product",
    "volume",
    "budget",
    "status",
    "comment",
    "username",
    "first_contact_at",
    "last_contact_at",
    "contact_skipped",
    "original_interests",
    "current_interest",
    "needs_human",
)


def missing_fields(profile: ClientProfile) -> list[str]:
    missing: list[str] = []
    if not profile.name:
        missing.append("name")
    if not profile.phone and not profile.email and not profile.contact_skipped:
        missing.append("phone")
    if not profile.product:
        missing.append("product")
    if not profile.volume:
        missing.append("volume")
    return missing


def build_model_context(
    profile: ClientProfile,
    history: Sequence[HistoryEntry] | None = None,
    *,
    intent: str | None = None,
) -> dict[str, object]:
    """Single compact context for the sales model. Not a raw CRM table dump."""
    rows = list(history or ())
    return {
        "profile": {name: _profile_value(profile, name) for name in _PROFILE_FIELDS},
        "missing_fields": missing_fields(profile),
        "deal_stage": infer_deal_stage(profile, intent),
        "returning": bool(profile.name and profile.product),
        "interests": [
            value
            for value in dict.fromkeys(
                [*(profile.original_interests or []), profile.product, profile.current_interest]
            )
            if value
        ],
        "recent_history": [_history_row(row) for row in rows],
    }


def _profile_value(profile: ClientProfile, name: str) -> object:
    value = getattr(profile, name)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _history_row(row: HistoryEntry) -> dict[str, str | None]:
    return {
        "user": row.user_message,
        "assistant": row.assistant_message,
        "at": row.created_at.isoformat() if row.created_at else None,
    }
