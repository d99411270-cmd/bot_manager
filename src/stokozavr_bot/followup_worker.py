from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .followup import due_for_followup, followup_text
from .models import ClientProfile, FollowupPlan, HistoryEntry
from .repositories import CRMRepository
from .service import is_valid_ai_reply

logger = logging.getLogger(__name__)

FollowupPlanner = Callable[[ClientProfile, list[HistoryEntry]], Awaitable[FollowupPlan]]


async def send_due_followups(
    repository: CRMRepository,
    send: Callable[[int, str], Awaitable[None]],
    *,
    now: datetime | None = None,
    planner: FollowupPlanner | None = None,
    history_limit: int = 10,
) -> int:
    moment = now or datetime.now(timezone.utc)
    sent = 0
    for client in await repository.list_clients():
        if not due_for_followup(client, moment):
            continue
        history = await repository.get_history(client.telegram_id, history_limit)
        try:
            text = await _compose_followup(client, history, planner)
        except Exception:
            logger.exception("Follow-up analysis failed for telegram_id=%s", client.telegram_id)
            continue
        if text is None:
            client.followup_sent = True
            await repository.save_client(client)
            continue
        try:
            await send(client.telegram_id, text)
        except Exception:
            logger.exception("Follow-up send failed for telegram_id=%s", client.telegram_id)
            continue
        client.followup_sent = True
        client.last_contact_at = moment
        await repository.save_client(client)
        await repository.append_history(client.telegram_id, moment, "", text)
        sent += 1
    return sent


async def _compose_followup(
    client: ClientProfile,
    history: list[HistoryEntry],
    planner: FollowupPlanner | None,
) -> str | None:
    if planner is None:
        return followup_text(client)
    plan = await planner(client, history)
    if not plan.appropriate:
        return None
    if plan.reply and is_valid_ai_reply(plan.reply):
        return plan.reply.strip()
    return followup_text(client)


async def followup_loop(
    repository: CRMRepository,
    send: Callable[[int, str], Awaitable[None]],
    *,
    interval_seconds: float = 60.0,
    planner: FollowupPlanner | None = None,
) -> None:
    while True:
        try:
            await send_due_followups(repository, send, planner=planner)
        except Exception:
            logger.exception("Follow-up sweep failed")
        await asyncio.sleep(interval_seconds)
