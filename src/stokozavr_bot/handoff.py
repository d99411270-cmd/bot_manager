from __future__ import annotations

from typing import Any, Protocol


class ManagerHandoff(Protocol):
    """Narrow port for a confirmed manager handoff. Not amoCRM."""

    async def create(self, kind: str, payload: dict[str, Any]) -> str | None: ...


class InMemoryManagerHandoff:
    """Test/local adapter. Isolated QA stand does not attach this until wired."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self._next = 1

    async def create(self, kind: str, payload: dict[str, Any]) -> str | None:
        record_id = f"mem-{self._next}"
        self._next += 1
        record = {"id": record_id, "kind": kind, "payload": dict(payload)}
        self.created.append(record)
        return record_id
