from __future__ import annotations

import ast
import io
import json
from dataclasses import fields
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from stokozavr_bot.models import AiTurn, ClientProfile, IntakeAnalysis
from stokozavr_bot.product_catalog import generated_price_list
from stokozavr_bot.qa_stand import (
    ISOLATED_ID_MIN,
    IsolatedQASession,
    QACredentialsError,
    QAIsolationError,
    QATransportAckError,
    QATurnLimit,
    build_live_deepseek,
    live_smoke_ready,
    parse_args,
)
from stokozavr_bot.repositories import InMemoryCRMRepository
from stokozavr_bot.service import START_TEXT, ConversationService


class ScriptedAI:
    def __init__(self, analyses=None, turns=None):
        self.analyses = list(analyses or [])
        self.turns = list(turns or [])
        self.intake_calls = []
        self.respond_calls = []

    async def analyze_intake(self, profile, history, message):
        self.intake_calls.append(message)
        if self.analyses:
            return self.analyses.pop(0)
        if message == "Анна":
            return IntakeAnalysis("provide_data", name="Анна")
        return IntakeAnalysis("question")

    async def respond(self, profile, history, message):
        self.respond_calls.append(message)
        if self.turns:
            return self.turns.pop(0)
        return AiTurn(reply="Чем могу помочь по поставке?")


@pytest.fixture
def now():
    return datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _session(tmp_path, now, **overrides):
    values = {
        "persona": "закупщик столовой",
        "scenario": "спрашивает фрукты и не даёт телефон",
        "goal": "Иван отвечает по каталогу, не анкетит",
        "max_turns": 4,
        "ai": ScriptedAI(),
        "output_dir": tmp_path,
        "clock": lambda: now,
        "telegram_id": ISOLATED_ID_MIN + 7,
        "username": "qa_buyer",
    }
    values.update(overrides)
    return IsolatedQASession(**values)


def test_qa_stand_module_does_not_import_sheets_telegram_or_settings():
    source = Path(__file__).resolve().parents[1] / "src" / "stokozavr_bot" / "qa_stand.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.add(node.module.split(".")[0])
    banned = {
        "stokozavr_bot.config",
        "stokozavr_bot.google_sheets",
        "stokozavr_bot.telegram",
        "stokozavr_bot.__main__",
        "config",
        "google_sheets",
        "telegram",
    }
    assert imported.isdisjoint(banned)


@pytest.mark.asyncio
async def test_start_uses_real_service_and_clean_in_memory_profile(tmp_path, now):
    session = _session(tmp_path, now)
    reply = await session.start()

    assert reply.text == START_TEXT
    assert isinstance(session.service, ConversationService)
    assert isinstance(session.repository, InMemoryCRMRepository)
    profile = await session.profile()
    assert profile is not None
    assert profile.telegram_id == ISOLATED_ID_MIN + 7
    assert profile.name is None
    assert session.turns[0].user == "/start"
    assert session.turns[0].assistant == START_TEXT


@pytest.mark.asyncio
async def test_external_agent_chooses_next_user_message_after_ivan_reply(tmp_path, now):
    session = _session(tmp_path, now, max_turns=2)
    first = await session.start()
    second = await session.send("Анна")

    assert "как я могу к вам обращаться" in first.text.lower()
    assert "телефон" in second.text.lower()
    assert [turn.user for turn in session.turns] == ["/start", "Анна"]
    profile = await session.profile()
    assert profile.name == "Анна"


@pytest.mark.asyncio
async def test_send_stops_after_max_user_turns(tmp_path, now):
    session = _session(tmp_path, now, max_turns=1)
    await session.start()
    await session.send("Анна")

    with pytest.raises(QATurnLimit):
        await session.send("какие фрукты есть?")


@pytest.mark.asyncio
async def test_two_sessions_do_not_share_crm_state(tmp_path, now):
    first = _session(tmp_path, now, telegram_id=ISOLATED_ID_MIN + 1)
    second = _session(tmp_path, now, telegram_id=ISOLATED_ID_MIN + 2)
    await first.start()
    await first.send("Анна")
    await second.start()

    assert (await first.profile()).name == "Анна"
    assert (await second.profile()).name is None
    assert first.repository is not second.repository


@pytest.mark.asyncio
async def test_save_writes_transcript_and_profile_without_secrets_or_attachments(tmp_path, now):
    session = _session(
        tmp_path,
        now,
        ai=ScriptedAI(turns=[AiTurn(reply="Яблоки 820 ₽ за ящик. Какой объём берёте?")]),
    )
    await session.start()
    await session.send("Анна")
    await session.send("прайс прямо в чат")
    path = session.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = path.read_text(encoding="utf-8")
    assert payload["persona"] == "закупщик столовой"
    assert payload["scenario"].startswith("спрашивает фрукты")
    assert payload["goal"].startswith("Иван отвечает")
    assert payload["telegram_id"] == ISOLATED_ID_MIN + 7
    assert payload["turns"][0]["user"] == "/start"
    assert payload["turns"][1]["user"] == "Анна"
    assert payload["profile"]["name"] == "Анна"
    assert payload["turns"][-1]["attachment_filename"] == "stokozavr-price-list.md"
    assert "attachment_content" not in payload["turns"][-1]
    assert "attachment_content" not in raw
    assert "VEG-POTATO-001" not in raw
    assert "api_key" not in raw.lower()
    assert "Bearer" not in raw
    assert "Authorization" not in raw
    assert "choices" not in raw


@pytest.mark.asyncio
async def test_save_redacts_secret_like_user_text(tmp_path, now):
    session = _session(tmp_path, now, max_turns=1, auto_start=False)
    await session.send("мой ключ Bearer sk-secretvalue123 и DEEPSEEK_API_KEY=abc123")
    path = session.save()

    raw = path.read_text(encoding="utf-8")
    assert "sk-secretvalue123" not in raw
    assert "abc123" not in raw
    assert "[REDACTED]" in raw


@pytest.mark.asyncio
async def test_jsonl_loop_lets_agent_stop_and_persists_result(tmp_path, now):
    session = _session(tmp_path, now, max_turns=3)
    inbox = io.StringIO('{"user": "Анна"}\n{"stop": true}\n')
    outbox = io.StringIO()

    result = await session.run_jsonl(inbox, outbox)
    events = [json.loads(line) for line in outbox.getvalue().splitlines() if line.strip()]

    assert events[0]["event"] == "hello"
    assert events[0]["persona"] == "закупщик столовой"
    assert any(item.get("user") == "/start" for item in events)
    assert any(item.get("user") == "Анна" for item in events)
    assert events[-1]["event"] == "done"
    assert result.path is not None and result.path.exists()
    assert result.profile["name"] == "Анна"


@pytest.mark.asyncio
async def test_run_scripted_messages_and_returns_final_profile(tmp_path, now):
    session = _session(tmp_path, now, max_turns=2)
    result = await session.run(["Анна"])

    assert [turn.user for turn in result.turns] == ["/start", "Анна"]
    assert result.profile["name"] == "Анна"
    assert result.path is not None


def test_default_telegram_id_is_in_isolated_range(tmp_path, now):
    session = _session(tmp_path, now, telegram_id=None)

    assert session.telegram_id >= ISOLATED_ID_MIN


def test_google_sheets_repository_is_rejected(tmp_path, now):
    class GoogleSheetsCRMRepository:
        pass

    GoogleSheetsCRMRepository.__module__ = "stokozavr_bot.google_sheets"

    with pytest.raises(QAIsolationError, match="Google Sheets"):
        _session(tmp_path, now, repository=GoogleSheetsCRMRepository())


def test_build_live_deepseek_uses_env_and_never_settings(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.test")

    from stokozavr_bot import config

    monkeypatch.setattr(
        config,
        "Settings",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Settings запрещён")),
    )

    client = build_live_deepseek()

    assert client.api_key == "test-deepseek-key"
    assert client.model == "deepseek-v4-flash"
    assert client.base_url == "https://deepseek.test"


def test_live_client_requires_deepseek_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(QACredentialsError, match="DEEPSEEK_API_KEY"):
        build_live_deepseek()


def test_smoke_readiness_reports_missing_key_without_network(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    report = live_smoke_ready()

    assert report.ready is False
    assert report.blocker is not None
    assert "DEEPSEEK_API_KEY" in report.blocker
    assert "Google" in report.blocker or "Telegram" in report.blocker


def test_cli_parser_accepts_persona_scenario_goal_and_jsonl():
    args = parse_args(
        [
            "--persona",
            "закупщик",
            "--scenario",
            "фрукты",
            "--goal",
            "не анкетить",
            "--turns",
            "6",
            "--jsonl",
        ]
    )

    assert args.persona == "закупщик"
    assert args.scenario == "фрукты"
    assert args.goal == "не анкетить"
    assert args.turns == 6
    assert args.jsonl is True
    assert args.smoke is False


PROFILE_FIELDS = {item.name for item in fields(ClientProfile)}


def _assert_profile_snapshot(snapshot: dict) -> None:
    assert set(snapshot) >= PROFILE_FIELDS
    assert "attachment_content" not in snapshot
    assert "api_key" not in json.dumps(snapshot).lower()


@pytest.mark.asyncio
async def test_each_turn_keeps_profile_before_and_after_snapshots(tmp_path, now):
    session = _session(tmp_path, now, max_turns=2)
    await session.start()
    await session.send("Анна")
    path = (await session.finish()).path
    payload = json.loads(path.read_text(encoding="utf-8"))

    start_turn = session.turns[0]
    name_turn = session.turns[1]
    _assert_profile_snapshot(start_turn.profile_before)
    _assert_profile_snapshot(start_turn.profile_after)
    _assert_profile_snapshot(name_turn.profile_before)
    _assert_profile_snapshot(name_turn.profile_after)
    assert start_turn.profile_before["name"] is None
    assert start_turn.profile_after["name"] is None
    assert name_turn.profile_before["name"] is None
    assert name_turn.profile_after["name"] == "Анна"
    assert name_turn.profile_after["status"]
    saved_name_turn = payload["turns"][1]
    assert saved_name_turn["profile_before"]["name"] is None
    assert saved_name_turn["profile_after"]["name"] == "Анна"
    assert payload["profile"]["name"] == "Анна"
    assert name_turn.profile_before is not name_turn.profile_after
    assert name_turn.profile_after["name"] == "Анна"


@pytest.mark.asyncio
async def test_attachment_event_keeps_filename_and_safe_metrics_not_content(tmp_path, now):
    content = generated_price_list()
    expected_bytes = len(content.encode("utf-8"))
    expected_hash = sha256(content.encode("utf-8")).hexdigest()
    expected_skus = content.count("SKU:")
    session = _session(
        tmp_path,
        now,
        ai=ScriptedAI(turns=[AiTurn(reply="Отправляю прайс.")]),
        max_turns=2,
    )
    await session.start()
    await session.send("прайс прямо в чат")
    turn = session.turns[-1]
    path = (await session.finish()).path
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = path.read_text(encoding="utf-8")
    saved = payload["turns"][-1]

    assert turn.attachment_filename == "stokozavr-price-list.md"
    assert turn.attachment_bytes == expected_bytes
    assert turn.attachment_sku_count == expected_skus == 30
    assert turn.attachment_sha256 == expected_hash
    assert saved["attachment_filename"] == "stokozavr-price-list.md"
    assert saved["attachment_bytes"] == expected_bytes
    assert saved["attachment_sku_count"] == 30
    assert saved["attachment_sha256"] == expected_hash
    assert "attachment_content" not in saved
    assert content not in raw
    assert "VEG-POTATO-001" not in raw


@pytest.mark.asyncio
async def test_transport_ack_marks_price_list_sent_only_after_pending_attachment(tmp_path, now):
    session = _session(tmp_path, now, max_turns=2)
    await session.start()
    await session.send("прайс прямо в чат")
    before_ack = await session.profile()

    assert session.turns[-1].attachment_filename == "stokozavr-price-list.md"
    assert before_ack is not None
    assert before_ack.price_list_sent_at is None
    assert before_ack.price_list_requested is True

    state = await session.ack_attachment()
    after_ack = await session.profile()

    assert after_ack is not None
    assert after_ack.price_list_sent_at == now
    assert after_ack.price_list_requested is False
    assert state["ack"] == "attachment_sent"
    assert state["profile"]["price_list_sent_at"] == now.isoformat()
    assert state["profile"]["price_list_requested"] is False
    assert set(state["profile"]) >= PROFILE_FIELDS


@pytest.mark.asyncio
async def test_transport_ack_without_pending_attachment_is_error(tmp_path, now):
    session = _session(tmp_path, now, max_turns=1, auto_start=False)

    with pytest.raises(QATransportAckError, match="pending"):
        await session.ack_attachment()
    assert (await session.profile()) is None


@pytest.mark.asyncio
async def test_jsonl_ack_event_marks_sent_and_returns_updated_profile(tmp_path, now):
    session = _session(tmp_path, now, max_turns=3)
    inbox = io.StringIO(
        '{"user": "прайс прямо в чат"}\n{"ack": "attachment_sent"}\n{"stop": true}\n'
    )
    outbox = io.StringIO()

    result = await session.run_jsonl(inbox, outbox)
    events = [json.loads(line) for line in outbox.getvalue().splitlines() if line.strip()]
    replies = [item for item in events if item.get("event") == "reply"]
    acks = [item for item in events if item.get("event") == "ack"]
    attachments = [item for item in events if item.get("event") == "attachment"]
    price_reply = next(item for item in replies if item.get("user") == "прайс прямо в чат")

    assert price_reply["attachment_filename"] == "stokozavr-price-list.md"
    assert price_reply["profile_after"]["price_list_sent_at"] is None
    assert attachments
    assert attachments[0]["filename"] == "stokozavr-price-list.md"
    assert attachments[0]["bytes"]
    assert attachments[0]["sku_count"] == 30
    assert attachments[0]["sha256"]
    assert "content" not in attachments[0]
    assert len(acks) == 1
    assert acks[0]["ack"] == "attachment_sent"
    assert acks[0]["profile"]["price_list_sent_at"] == now.isoformat()
    assert result.profile["price_list_sent_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_jsonl_ack_without_pending_attachment_emits_error(tmp_path, now):
    session = _session(tmp_path, now, max_turns=1, auto_start=False)
    inbox = io.StringIO('{"ack": "attachment_sent"}\n{"stop": true}\n')
    outbox = io.StringIO()

    await session.run_jsonl(inbox, outbox)
    events = [json.loads(line) for line in outbox.getvalue().splitlines() if line.strip()]

    assert any(
        item.get("event") == "error" and "pending" in str(item.get("reason", "")) for item in events
    )
    assert not any(item.get("event") == "ack" for item in events)


@pytest.mark.asyncio
async def test_isolated_stand_marks_manager_handoff_unobservable(tmp_path, now):
    session = _session(
        tmp_path,
        now,
        max_turns=2,
        ai=ScriptedAI(turns=[AiTurn(reply="Передам вопрос менеджеру.", needs_human=True)]),
    )
    inbox = io.StringIO('{"user": "кто производитель?"}\n{"stop": true}\n')
    outbox = io.StringIO()

    result = await session.run_jsonl(inbox, outbox)
    events = [json.loads(line) for line in outbox.getvalue().splitlines() if line.strip()]
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    profile = await session.profile()

    assert profile is not None
    assert profile.needs_human is True
    assert result.manager_handoff_observable is False
    assert result.handoff is None
    for event in events:
        assert event.get("manager_handoff_observable") is False
        assert event.get("handoff") is None
    assert payload["manager_handoff_observable"] is False
    assert payload["handoff"] is None
    assert "amocrm" not in json.dumps(payload).lower()
    assert "deal_id" not in payload
    assert "task_id" not in payload


def test_unicode_escape_persona_becomes_readable_slug_and_username(tmp_path, now):
    session = _session(
        tmp_path,
        now,
        persona=r"P8 \u0421\u0435\u0440\u0433\u0435\u0439 \u0418\u0432\u0430\u043d\u043e\u0432 phones",
        scenario=r"\u0424\u0418\u041e \u0433\u043e\u0440\u043e\u0434\u0441\u043a\u043e\u0439",
        goal=r"\u043a\u043e\u043d\u0442\u0430\u043a\u0442\u044b",
        username=None,
        auto_start=False,
    )
    path = session.save()
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert "Сергей" in session.persona
    assert "ФИО" in session.scenario
    assert "контакты" in session.goal
    assert "сергей" in session.username.lower()
    assert "u0421" not in session.username
    assert "u0421" not in path.name
    assert "сергей" in path.name.lower()
    assert "/" not in path.name
    assert "\\" not in path.name
    assert payload["persona"] == "P8 Сергей Иванов phones"
    assert "u0421" not in payload["username"]
    assert "Сергей" in payload["persona"]


def test_double_escaped_unicode_persona_is_decoded(tmp_path, now):
    session = _session(
        tmp_path,
        now,
        persona="P8 \\\\u0421\\\\u0435\\\\u0440\\\\u0433\\\\u0435\\\\u0439",
        username=None,
        auto_start=False,
    )

    assert "Сергей" in session.persona
    assert "u0421" not in session.username
    assert "сергей" in session.username.lower()


@pytest.mark.asyncio
async def test_send_does_not_write_transcript_until_finish(tmp_path, now):
    session = _session(tmp_path, now, max_turns=1, run_id="run-observable-1")
    await session.start()
    await session.send("Анна")

    assert list(tmp_path.glob("*.json")) == []
    assert session.run_id == "run-observable-1"

    result = await session.finish()
    payload = json.loads(result.path.read_text(encoding="utf-8"))

    assert result.path is not None
    assert result.path.exists()
    assert result.completed is True
    assert result.run_id == "run-observable-1"
    assert payload["completed"] is True
    assert payload["run_id"] == "run-observable-1"


def test_explicit_save_before_finish_is_incomplete(tmp_path, now):
    session = _session(tmp_path, now, auto_start=False, run_id="run-aborted-1")
    path = session.save()
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["completed"] is False
    assert payload["run_id"] == "run-aborted-1"


@pytest.mark.asyncio
async def test_jsonl_hello_and_done_carry_run_id_and_completion(tmp_path, now):
    session = _session(tmp_path, now, max_turns=1, run_id="run-jsonl-1")
    inbox = io.StringIO('{"user": "Анна"}\n{"stop": true}\n')
    outbox = io.StringIO()

    result = await session.run_jsonl(inbox, outbox)
    events = [json.loads(line) for line in outbox.getvalue().splitlines() if line.strip()]
    hello = events[0]
    done = events[-1]

    assert hello["event"] == "hello"
    assert hello["run_id"] == "run-jsonl-1"
    assert hello["completed"] is False
    assert done["event"] == "done"
    assert done["run_id"] == "run-jsonl-1"
    assert done["completed"] is True
    assert result.completed is True
    assert result.run_id == "run-jsonl-1"
