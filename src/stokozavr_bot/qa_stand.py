"""Isolated multi-turn QA stand for Ivan. Never touches Sheets or Telegram."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .deepseek import DeepSeekClient
from .models import BotReply, ClientProfile, IncomingMessage
from .repositories import InMemoryCRMRepository
from .service import ConversationService

ISOLATED_ID_MIN = 2_000_000_000
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "qa-dialogues"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]+\b"),
    re.compile(r"(?i)\bDEEPSEEK_API_KEY\s*=\s*\S+"),
    re.compile(r"(?i)\b(?:api[_-]?key|authorization)\s*[:=]\s*\S+"),
)


class QACredentialsError(RuntimeError):
    """Live DeepSeek credentials are missing; the stand stays offline."""


class QAIsolationError(RuntimeError):
    """Attempted to attach production CRM or a non-isolated profile."""


class QATurnLimit(RuntimeError):
    """The QA agent used every allowed user turn."""


@dataclass(slots=True)
class QATurn:
    index: int
    user: str
    assistant: str
    request_contact: bool = False
    delay: bool = False
    attachment_filename: str | None = None


@dataclass(slots=True)
class QASessionResult:
    persona: str
    scenario: str
    goal: str
    telegram_id: int
    turns: list[QATurn]
    profile: dict[str, Any]
    path: Path | None = None


@dataclass(slots=True)
class QASmokeReport:
    ready: bool
    blocker: str | None = None
    model: str | None = None
    reply_preview: str | None = None
    path: Path | None = None


@dataclass(slots=True)
class IsolatedQASession:
    persona: str
    scenario: str
    goal: str
    max_turns: int = 8
    ai: Any | None = None
    repository: InMemoryCRMRepository | None = None
    telegram_id: int | None = None
    username: str | None = None
    output_dir: Path | None = None
    auto_start: bool = True
    clock: Callable[[], datetime] | None = None
    service: ConversationService = field(init=False)
    turns: list[QATurn] = field(init=False, default_factory=list)
    _started: bool = field(init=False, default=False)
    _user_turns: int = field(init=False, default=0)
    _saved_path: Path | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns должен быть >= 1")
        if self.repository is None:
            self.repository = InMemoryCRMRepository()
        else:
            assert_isolated_repository(self.repository)
        if self.telegram_id is None:
            self.telegram_id = random.randint(ISOLATED_ID_MIN, ISOLATED_ID_MIN + 99_999_999)
        elif self.telegram_id < ISOLATED_ID_MIN:
            raise QAIsolationError(
                f"telegram_id QA-стенда должен быть изолированным (>= {ISOLATED_ID_MIN})"
            )
        if self.username is None:
            self.username = f"qa_{_slug(self.persona)}"[:32]
        if self.output_dir is None:
            self.output_dir = DEFAULT_OUTPUT_DIR
        else:
            self.output_dir = Path(self.output_dir)
        if self.clock is None:
            self.clock = lambda: datetime.now(timezone.utc)
        if self.ai is None:
            self.ai = build_live_deepseek()
        self.service = ConversationService(self.repository, self.ai, clock=self.clock)
        self.turns = []

    async def start(self) -> BotReply | None:
        if self._started:
            return None
        self._started = True
        if not self.auto_start:
            return None
        return await self._deliver("/start", count_user_turn=False)

    async def send(self, text: str) -> BotReply:
        if not self._started and self.auto_start:
            await self.start()
        self._started = True
        if self._user_turns >= self.max_turns:
            raise QATurnLimit(f"Достигнут лимит ходов: {self.max_turns}")
        return await self._deliver(text, count_user_turn=True)

    async def profile(self) -> ClientProfile | None:
        assert self.telegram_id is not None
        return await self.repository.get_client(self.telegram_id)

    async def run(self, messages: Sequence[str]) -> QASessionResult:
        await self.start()
        for message in messages:
            if self._user_turns >= self.max_turns:
                break
            await self.send(message)
        return await self.finish()

    async def run_jsonl(self, reader: TextIO, writer: TextIO) -> QASessionResult:
        _write_jsonl(
            writer,
            {
                "event": "hello",
                "persona": self.persona,
                "scenario": self.scenario,
                "goal": self.goal,
                "telegram_id": self.telegram_id,
                "max_turns": self.max_turns,
            },
        )
        start_reply = await self.start()
        if start_reply is not None and self.turns:
            _write_jsonl(writer, self._reply_event(self.turns[-1]))
        for raw in reader:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                _write_jsonl(writer, {"event": "error", "reason": "invalid_json"})
                continue
            if not isinstance(payload, dict):
                _write_jsonl(writer, {"event": "error", "reason": "invalid_json"})
                continue
            if payload.get("stop"):
                break
            user = payload.get("user")
            if not isinstance(user, str) or not user.strip():
                _write_jsonl(writer, {"event": "error", "reason": "missing_user"})
                continue
            try:
                await self.send(user)
            except QATurnLimit:
                break
            _write_jsonl(writer, self._reply_event(self.turns[-1]))
        return await self.finish(writer)

    async def finish(self, writer: TextIO | None = None) -> QASessionResult:
        path = self.save()
        profile = serialize_profile(await self.profile())
        result = QASessionResult(
            persona=self.persona,
            scenario=self.scenario,
            goal=self.goal,
            telegram_id=self.telegram_id or 0,
            turns=list(self.turns),
            profile=profile,
            path=path,
        )
        if writer is not None:
            _write_jsonl(writer, {"event": "done", "path": str(path), "profile": profile})
        return result

    def save(self) -> Path:
        assert self.output_dir is not None
        assert self.telegram_id is not None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
        path = self.output_dir / f"{stamp}-{_slug(self.persona)}-{self.telegram_id}.json"
        payload = {
            "persona": self.persona,
            "scenario": self.scenario,
            "goal": self.goal,
            "telegram_id": self.telegram_id,
            "username": self.username,
            "max_turns": self.max_turns,
            "turns": [self._turn_payload(turn) for turn in self.turns],
            "profile": serialize_profile(_sync_profile(self.repository, self.telegram_id)),
        }
        path.write_text(
            json.dumps(redact_tree(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._saved_path = path
        return path

    async def _deliver(self, text: str, *, count_user_turn: bool) -> BotReply:
        assert self.telegram_id is not None
        reply = await self.service.handle(IncomingMessage(self.telegram_id, self.username, text))
        if count_user_turn:
            self._user_turns += 1
        self.turns.append(
            QATurn(
                index=len(self.turns),
                user=text,
                assistant=reply.text,
                request_contact=reply.request_contact,
                delay=reply.delay,
                attachment_filename=reply.attachment_filename,
            )
        )
        return reply

    def _reply_event(self, turn: QATurn) -> dict[str, Any]:
        remaining = max(self.max_turns - self._user_turns, 0)
        profile = _sync_profile(self.repository, self.telegram_id or 0)
        return {
            "event": "reply",
            "turn": turn.index,
            "user": turn.user,
            "assistant": turn.assistant,
            "status": profile.status if profile else None,
            "remaining": remaining,
            "attachment_filename": turn.attachment_filename,
        }

    @staticmethod
    def _turn_payload(turn: QATurn) -> dict[str, Any]:
        return {
            "index": turn.index,
            "user": turn.user,
            "assistant": turn.assistant,
            "request_contact": turn.request_contact,
            "delay": turn.delay,
            "attachment_filename": turn.attachment_filename,
        }


def assert_isolated_repository(repository: object) -> None:
    module = type(repository).__module__
    name = type(repository).__name__
    if "google_sheets" in module or name == "GoogleSheetsCRMRepository":
        raise QAIsolationError("QA-стенд не может использовать Google Sheets CRM")
    if not isinstance(repository, InMemoryCRMRepository):
        raise QAIsolationError("QA-стенд принимает только InMemoryCRMRepository")


def apply_project_deepseek_env(env_file: Path | None = None) -> None:
    path = env_file or (REPO_ROOT / ".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key.startswith("DEEPSEEK_") or os.environ.get(key):
            continue
        os.environ[key] = value.strip().strip("'").strip('"')


def load_deepseek_settings_from_env() -> dict[str, Any]:
    apply_project_deepseek_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise QACredentialsError(
            "Нет DEEPSEEK_API_KEY в окружении. "
            "Изолированный QA не читает Settings и не трогает Google/Telegram. "
            "Задайте только DEEPSEEK_API_KEY "
            "(опционально DEEPSEEK_BASE_URL, DEEPSEEK_MODEL)."
        )
    timeout_raw = os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "20").strip() or "20"
    tokens_raw = os.environ.get("DEEPSEEK_MAX_TOKENS", "800").strip() or "800"
    return {
        "api_key": api_key,
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        or "https://api.deepseek.com",
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
        or "deepseek-v4-flash",
        "timeout": float(timeout_raw),
        "max_tokens": int(tokens_raw),
    }


def build_live_deepseek() -> DeepSeekClient:
    settings = load_deepseek_settings_from_env()
    return DeepSeekClient(
        settings["api_key"],
        base_url=settings["base_url"],
        model=settings["model"],
        timeout=settings["timeout"],
        max_tokens=settings["max_tokens"],
    )


def live_smoke_ready() -> QASmokeReport:
    try:
        settings = load_deepseek_settings_from_env()
    except QACredentialsError as exc:
        return QASmokeReport(ready=False, blocker=str(exc))
    return QASmokeReport(ready=True, model=settings["model"])


async def smoke_real_deepseek(output_dir: Path | None = None) -> QASmokeReport:
    ready = live_smoke_ready()
    if not ready.ready:
        return ready
    session = IsolatedQASession(
        persona="smoke",
        scenario="один изолированный ход к живому DeepSeek",
        goal="подтвердить реальный клиент без Sheets/Telegram",
        max_turns=1,
        output_dir=output_dir or DEFAULT_OUTPUT_DIR,
    )
    await session.start()
    reply = await session.send("какие фрукты есть?")
    path = session.save()
    preview = redact_text(reply.text)[:160]
    return QASmokeReport(
        ready=True,
        model=getattr(session.ai, "model", None),
        reply_preview=preview,
        path=path,
    )


def serialize_profile(profile: ClientProfile | None) -> dict[str, Any]:
    if profile is None:
        return {}
    payload: dict[str, Any] = {}
    for item in fields(profile):
        value = getattr(profile, item.name)
        if isinstance(value, datetime):
            payload[item.name] = value.isoformat()
        else:
            payload[item.name] = value
    return redact_tree(payload)


def redact_text(text: str) -> str:
    redacted = text
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        redacted = redacted.replace(env_key, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_tree(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Изолированный QA-стенд диалогов с Иваном")
    parser.add_argument("--persona", default="", help="Кто говорит с Иваном")
    parser.add_argument("--scenario", default="", help="Что происходит в диалоге")
    parser.add_argument("--goal", default="", help="Что проверяет QA-агент")
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--telegram-id", type=int)
    parser.add_argument("--username")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--jsonl", action="store_true")
    parser.add_argument("--script", nargs="+")
    parser.add_argument("--smoke", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


async def amain(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.smoke:
        report = await smoke_real_deepseek(args.output_dir)
        if report.ready:
            print(
                f"SMOKE=ok model={report.model} path={report.path} preview={report.reply_preview}",
                flush=True,
            )
            return 0
        print(f"SMOKE=blocked {report.blocker}", flush=True)
        return 2
    if not (args.persona and args.scenario and args.goal):
        print("Нужны --persona, --scenario и --goal (или --smoke).", file=sys.stderr)
        return 2
    session = IsolatedQASession(
        persona=args.persona,
        scenario=args.scenario,
        goal=args.goal,
        max_turns=args.turns,
        telegram_id=args.telegram_id,
        username=args.username,
        output_dir=args.output_dir,
        auto_start=not args.no_start,
    )
    if args.script:
        result = await session.run(args.script)
        print(f"SAVED={result.path}", flush=True)
        return 0
    if args.jsonl:
        await session.run_jsonl(sys.stdin, sys.stdout)
        return 0
    await _interactive(session)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(amain(argv))


async def _interactive(session: IsolatedQASession) -> None:
    start_reply = await session.start()
    if start_reply is not None:
        print(f"ivan: {start_reply.text}", flush=True)
    while session._user_turns < session.max_turns:
        try:
            line = input("user> ")
        except EOFError:
            print(flush=True)
            break
        if line.strip().lower() in {"", "/stop", "/quit"}:
            break
        reply = await session.send(line)
        print(f"ivan: {reply.text}", flush=True)
    result = await session.finish()
    print(f"SAVED={result.path}", flush=True)


def _sync_profile(repository: InMemoryCRMRepository, telegram_id: int) -> ClientProfile | None:
    return repository.clients.get(telegram_id)


def _write_jsonl(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(redact_tree(payload), ensure_ascii=False) + "\n")
    stream.flush()


def _slug(value: str) -> str:
    compact = re.sub(r"[^0-9a-zа-яё]+", "-", value.lower()).strip("-")
    return compact or "qa"


if __name__ == "__main__":
    raise SystemExit(main())
