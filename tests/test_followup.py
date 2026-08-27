from datetime import datetime, timedelta, timezone

import pytest

from stokozavr_bot.followup import (
    apply_followup_rules,
    due_for_followup,
    followup_delay,
    followup_text,
    looks_like_thinking,
    reply_quoted_price,
)
from stokozavr_bot.followup_worker import send_due_followups
from stokozavr_bot.models import AiTurn, ClientProfile, FollowupPlan, HistoryEntry, IncomingMessage
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import PRICE_LIST_EMAIL_ACK, ConversationService

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class FakeAI:
    def __init__(self, turns=None):
        self.turns = list(turns or [])

    async def respond(self, profile, history, message):
        return self.turns.pop(0)

    async def analyze_intake(self, profile, history, message):
        from stokozavr_bot.models import IntakeAnalysis

        return IntakeAnalysis(intent="offtopic")


class FixedRng:
    def __init__(self, value: int):
        self.value = value
        self.calls: list[tuple[int, int]] = []

    def randint(self, a: int, b: int) -> int:
        self.calls.append((a, b))
        return self.value


def test_thinking_and_price_detection():
    assert looks_like_thinking("Я подумаю")
    assert looks_like_thinking("надо посоветоваться")
    assert not looks_like_thinking("сколько яблоки")
    assert reply_quoted_price("Яблоки 850 ₽ за ящик")
    assert not reply_quoted_price("Я уточню этот вопрос и вернусь к вам.")


def test_thinking_schedules_followup_with_jitter():
    client = ClientProfile(1, name="Дмитрий")
    rng = FixedRng(58)
    apply_followup_rules(client, "подумаю", "Хорошо.", NOW, rng=rng)
    assert rng.calls == [(55, 66)]
    assert client.followup_due_at == NOW + timedelta(minutes=58)
    assert client.followup_sent is False
    assert client.status == "ожидает решение"
    assert not due_for_followup(client, NOW + timedelta(minutes=57))
    assert due_for_followup(client, NOW + timedelta(minutes=58))


def test_other_reply_cancels_pending_followup():
    client = ClientProfile(1, name="Дмитрий", followup_due_at=NOW + timedelta(hours=1))
    apply_followup_rules(client, "а масло есть?", "Да, есть позиции.", NOW)
    assert client.followup_due_at is None


def test_followup_text_uses_name():
    text = followup_text(ClientProfile(1, name="Дмитрий"))
    assert text.startswith("Дмитрий,")
    assert "подумали" in text
    assert "секрет" in text


@pytest.mark.asyncio
async def test_price_reply_schedules_followup(now=NOW):
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=8,
            name="Дмитрий",
            phone="+79990001122",
            product="груши",
            volume="10 ящиков",
            status="квалифицирован",
        )
    )
    service = ConversationService(
        repo,
        FakeAI([AiTurn(reply="Груши 880 ₽ за ящик. Какой объём берёте?")]),
        clock=lambda: now,
    )
    await service.handle(IncomingMessage(8, None, "Сколько груши?"))
    saved = await repo.get_client(8)
    assert saved.followup_due_at is not None
    assert saved.followup_sent is False


@pytest.mark.asyncio
async def test_worker_sends_due_followup_once():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=9,
            name="Дмитрий",
            followup_due_at=NOW - timedelta(minutes=1),
            followup_sent=False,
        )
    )
    sent: list[tuple[int, str]] = []

    async def send(telegram_id: int, text: str) -> None:
        sent.append((telegram_id, text))

    count = await send_due_followups(repo, send, now=NOW)
    saved = await repo.get_client(9)
    assert count == 1
    assert sent[0][0] == 9
    assert "подумали" in sent[0][1]
    assert saved.followup_sent is True
    assert await send_due_followups(repo, send, now=NOW) == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_worker_skips_when_deepseek_says_inappropriate():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=10,
            name="Дмитрий",
            followup_due_at=NOW - timedelta(minutes=1),
            followup_sent=False,
        )
    )
    await repo.append_history(10, NOW, "не надо больше писать", "Хорошо.")
    sent: list[str] = []

    async def send(telegram_id: int, text: str) -> None:
        sent.append(text)

    async def planner(client, history):
        assert history[-1].user_message == "не надо больше писать"
        return FollowupPlan(appropriate=False)

    count = await send_due_followups(repo, send, now=NOW, planner=planner)
    saved = await repo.get_client(10)
    assert count == 0
    assert sent == []
    assert saved.followup_sent is True


@pytest.mark.asyncio
async def test_worker_sends_deepseek_reply_when_appropriate():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=11,
            name="Дмитрий",
            followup_due_at=NOW - timedelta(minutes=1),
            followup_sent=False,
        )
    )
    await repo.append_history(11, NOW, "подумаю", "Хорошо, жду.")
    sent: list[str] = []

    async def send(telegram_id: int, text: str) -> None:
        sent.append(text)

    async def planner(client, history):
        assert "подумаю" in history[-1].user_message
        return FollowupPlan(
            appropriate=True,
            reply="Дмитрий, получилось сориентироваться по грушам?",
        )

    count = await send_due_followups(repo, send, now=NOW, planner=planner)
    assert count == 1
    assert sent == ["Дмитрий, получилось сориентироваться по грушам?"]


@pytest.mark.asyncio
async def test_worker_keeps_due_if_deepseek_fails():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=12,
            followup_due_at=NOW - timedelta(minutes=1),
            followup_sent=False,
        )
    )

    async def send(telegram_id: int, text: str) -> None:
        raise AssertionError("should not send")

    async def planner(client, history):
        raise RuntimeError("api down")

    count = await send_due_followups(repo, send, now=NOW, planner=planner)
    saved = await repo.get_client(12)
    assert count == 0
    assert saved.followup_sent is False


def test_followup_delay_inclusive_bounds():
    assert followup_delay(FixedRng(55)) == timedelta(minutes=55)
    assert followup_delay(FixedRng(66)) == timedelta(minutes=66)


def test_price_list_email_ack_schedules_followup_with_jitter():
    client = ClientProfile(1, name="Дмитрий")
    rng = FixedRng(60)
    apply_followup_rules(client, "shop@example.com", PRICE_LIST_EMAIL_ACK, NOW, rng=rng)
    assert rng.calls == [(55, 66)]
    assert client.followup_due_at == NOW + timedelta(minutes=60)
    assert client.followup_sent is False
    assert not due_for_followup(client, NOW + timedelta(minutes=59))
    assert due_for_followup(client, NOW + timedelta(minutes=60))


def test_client_message_after_email_ack_cancels_pending_followup():
    client = ClientProfile(1, name="Дмитрий", followup_due_at=NOW + timedelta(minutes=60))
    apply_followup_rules(client, "а гречка есть?", "Да, есть позиции.", NOW)
    assert client.followup_due_at is None


def test_price_list_followup_fallback_mentions_price_list_not_old_price_template():
    history = [HistoryEntry(NOW, 1, "shop@example.com", PRICE_LIST_EMAIL_ACK)]
    text = followup_text(ClientProfile(1, name="Дмитрий"), history)
    lowered = text.lower()
    assert "прайс" in lowered
    assert "подумали" not in lowered


@pytest.mark.asyncio
async def test_email_ack_via_service_schedules_jittered_followup():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=20,
            name="Дмитрий",
            phone="+799****1122",
            price_list_requested=True,
        )
    )
    rng = FixedRng(61)
    service = ConversationService(repo, FakeAI(), clock=lambda: NOW, followup_rng=rng)
    result = await service.handle(IncomingMessage(20, None, "shop@example.com"))
    saved = await repo.get_client(20)
    assert result.text == PRICE_LIST_EMAIL_ACK
    assert rng.calls == [(55, 66)]
    assert saved.followup_due_at == NOW + timedelta(minutes=61)
    assert saved.followup_sent is False


@pytest.mark.asyncio
async def test_worker_sends_planner_reply_after_price_list_ack():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=21,
            name="Дмитрий",
            followup_due_at=NOW - timedelta(minutes=1),
            followup_sent=False,
        )
    )
    await repo.append_history(21, NOW, "shop@example.com", PRICE_LIST_EMAIL_ACK)
    sent: list[str] = []

    async def send(telegram_id: int, text: str) -> None:
        sent.append(text)

    async def planner(client, history):
        assert PRICE_LIST_EMAIL_ACK in history[-1].assistant_message
        return FollowupPlan(appropriate=True, reply="Дмитрий, глянули прайс — что зацепило?")

    count = await send_due_followups(repo, send, now=NOW, planner=planner)
    assert count == 1
    assert sent == ["Дмитрий, глянули прайс — что зацепило?"]


@pytest.mark.asyncio
async def test_worker_skips_inappropriate_after_price_list_ack():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=22,
            name="Дмитрий",
            followup_due_at=NOW - timedelta(minutes=1),
            followup_sent=False,
        )
    )
    await repo.append_history(22, NOW, "не пишите больше", "Хорошо.")
    sent: list[str] = []

    async def send(telegram_id: int, text: str) -> None:
        sent.append(text)

    async def planner(client, history):
        return FollowupPlan(appropriate=False)

    count = await send_due_followups(repo, send, now=NOW, planner=planner)
    saved = await repo.get_client(22)
    assert count == 0
    assert sent == []
    assert saved.followup_sent is True


@pytest.mark.asyncio
async def test_worker_keeps_due_if_planner_fails_after_price_list_ack():
    repo = InMemoryCRMRepository()
    await repo.save_client(
        ClientProfile(
            telegram_id=23,
            followup_due_at=NOW - timedelta(minutes=1),
            followup_sent=False,
        )
    )
    await repo.append_history(23, NOW, "shop@example.com", PRICE_LIST_EMAIL_ACK)

    async def send(telegram_id: int, text: str) -> None:
        raise AssertionError("should not send")

    async def planner(client, history):
        raise RuntimeError("api down")

    count = await send_due_followups(repo, send, now=NOW, planner=planner)
    saved = await repo.get_client(23)
    assert count == 0
    assert saved.followup_sent is False


def test_followup_system_prompt_covers_promised_price_list():
    from stokozavr_bot.deepseek import FOLLOWUP_SYSTEM_PROMPT

    lowered = FOLLOWUP_SYSTEM_PROMPT.lower()
    assert "прайс" in lowered
    assert "подумали по цене" not in lowered or "прайс" in lowered
