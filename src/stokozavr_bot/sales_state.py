from __future__ import annotations

from .models import ClientProfile

DEAL_STAGES = (
    "new_lead",
    "discovery",
    "product_selection",
    "qualified",
    "quote_requested",
    "objection",
    "ready_to_order",
)

_INTENT_STAGE = {
    "quote_requested": "quote_requested",
    "quote": "quote_requested",
    "objection": "objection",
    "ready_to_order": "ready_to_order",
    "order": "ready_to_order",
}


def infer_deal_stage(profile: ClientProfile, intent: str | None = None) -> str:
    """Sales stage for the model. Separate from Google Sheets status strings."""
    if intent in _INTENT_STAGE:
        return _INTENT_STAGE[intent]
    if profile.status == "готов к заказу":
        return "ready_to_order"
    if profile.status == "получил предложение":
        return "quote_requested"
    if profile.product and profile.volume:
        return "qualified"
    if profile.product:
        return "product_selection"
    if profile.name or profile.phone or intent == "question":
        return "discovery"
    return "new_lead"
