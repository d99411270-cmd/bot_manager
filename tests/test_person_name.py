from datetime import datetime, timezone

import pytest

from stokozavr_bot.models import IncomingMessage, IntakeAnalysis
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import ConversationService, parse_person_name


@pytest.fixture
def now():
    return datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


class SemanticAI:
    def __init__(self, analyses=()):
        self.analyses = list(analyses)

    async def analyze_intake(self, profile, history, message):
        if self.analyses:
            return self.analyses.pop(0)
        return IntakeAnalysis(intent="provide_data")

    async def respond(self, profile, history, message):
        raise RuntimeError("no respond")


def test_parse_full_name():
    assert parse_person_name("Сергей Иванов") == ("Сергей", "Иванов")
    assert parse_person_name("Меня зовут Анна Петрова") == ("Анна", "Петрова")
    assert parse_person_name("Дмитрий") == ("Дмитрий", None)
    assert parse_person_name("Огурцы много") is None


@pytest.mark.asyncio
async def test_bot_addresses_first_name_and_keeps_last_name(now):
    repo = InMemoryCRMRepository()
    service = ConversationService(repo, SemanticAI(), clock=lambda: now)

    result = await service.handle(IncomingMessage(1, None, "Сергей Иванов"))
    saved = await repo.get_client(1)

    assert saved.name == "Сергей"
    assert saved.last_name == "Иванов"
    assert "Сергей" in result.text
    assert "Иванов" not in result.text
