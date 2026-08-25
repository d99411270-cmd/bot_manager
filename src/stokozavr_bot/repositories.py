from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Protocol

from .models import ClientProfile, HistoryEntry


class CRMRepository(Protocol):
    async def get_client(self, telegram_id: int) -> ClientProfile | None: ...
    async def save_client(self, client: ClientProfile) -> None: ...
    async def append_history(
        self, telegram_id: int, created_at: datetime, user_message: str, assistant_message: str
    ) -> None: ...
    async def get_history(self, telegram_id: int, limit: int = 10) -> list[HistoryEntry]: ...
    async def list_clients(self) -> list[ClientProfile]: ...


class InMemoryCRMRepository:
    """Fake CRM for tests and local development; never persists secrets."""

    def __init__(self) -> None:
        self.clients: dict[int, ClientProfile] = {}
        self.history: list[HistoryEntry] = []

    async def get_client(self, telegram_id: int) -> ClientProfile | None:
        client = self.clients.get(telegram_id)
        return deepcopy(client) if client else None

    async def save_client(self, client: ClientProfile) -> None:
        self.clients[client.telegram_id] = deepcopy(client)

    async def append_history(
        self, telegram_id: int, created_at: datetime, user_message: str, assistant_message: str
    ) -> None:
        self.history.append(HistoryEntry(created_at, telegram_id, user_message, assistant_message))

    async def get_history(self, telegram_id: int, limit: int = 10) -> list[HistoryEntry]:
        rows = [x for x in self.history if x.telegram_id == telegram_id]
        return deepcopy(rows[-limit:])

    async def list_clients(self) -> list[ClientProfile]:
        return [deepcopy(client) for client in self.clients.values()]
