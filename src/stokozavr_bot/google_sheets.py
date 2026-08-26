from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from .models import ClientProfile, HistoryEntry

CLIENT_HEADERS = [
    "telegram_id",
    "username",
    "имя клиента",
    "телефон",
    "интересующая продукция",
    "статус клиента",
    "дата первого обращения",
    "дата последнего обращения",
    "комментарии",
]
HISTORY_HEADERS = ["дата", "telegram_id", "сообщение клиента", "ответ Ивана"]


class GoogleSheetsCRMRepository:
    def __init__(self, spreadsheet: Any) -> None:
        self.clients_sheet = self._worksheet(spreadsheet, "Клиенты", CLIENT_HEADERS)
        self.history_sheet = self._worksheet(spreadsheet, "История сообщений", HISTORY_HEADERS)

    @classmethod
    def from_service_account(
        cls,
        spreadsheet_id: str,
        *,
        credentials_file: str | None = None,
        credentials_json: str | None = None,
    ) -> GoogleSheetsCRMRepository:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if credentials_json:
            info = json.loads(credentials_json)
            credentials = Credentials.from_service_account_info(info, scopes=scopes)
        elif credentials_file:
            credentials = Credentials.from_service_account_file(credentials_file, scopes=scopes)
        else:
            raise ValueError("Укажите GOOGLE_SERVICE_ACCOUNT_FILE или GOOGLE_SERVICE_ACCOUNT_JSON")
        return cls(gspread.authorize(credentials).open_by_key(spreadsheet_id))

    @staticmethod
    def _worksheet(book: Any, title: str, headers: list[str]) -> Any:
        try:
            sheet = book.worksheet(title)
        except gspread.WorksheetNotFound:
            sheet = book.add_worksheet(title=title, rows=1000, cols=len(headers))
        first = sheet.row_values(1)
        if not first:
            sheet.append_row(headers, value_input_option="RAW")
        elif first != headers:
            raise ValueError(f"Лист {title!r} имеет неверные заголовки: {first}")
        return sheet

    async def get_client(self, telegram_id: int) -> ClientProfile | None:
        return await asyncio.to_thread(self._get_client_sync, telegram_id)

    def _get_client_sync(self, telegram_id: int) -> ClientProfile | None:
        for row in self.clients_sheet.get_all_records():
            if str(row.get("telegram_id")) == str(telegram_id):
                return _client_from_row(row, telegram_id)
        return None

    async def list_clients(self) -> list[ClientProfile]:
        return await asyncio.to_thread(self._list_clients_sync)

    def _list_clients_sync(self) -> list[ClientProfile]:
        result: list[ClientProfile] = []
        for row in self.clients_sheet.get_all_records():
            raw_id = row.get("telegram_id")
            try:
                telegram_id = int(str(raw_id))
            except (TypeError, ValueError):
                continue
            result.append(_client_from_row(row, telegram_id))
        return result

    async def save_client(self, client: ClientProfile) -> None:
        await asyncio.to_thread(self._save_client_sync, client)

    def _save_client_sync(self, client: ClientProfile) -> None:
        comment = _build_comment(client)
        values = [
            client.telegram_id,
            client.username or "",
            _display_name(client),
            client.phone or "",
            client.product or "",
            client.status,
            _format_date(client.first_contact_at),
            _format_date(client.last_contact_at),
            comment,
        ]
        ids = self.clients_sheet.col_values(1)
        try:
            row_number = next(
                i for i, value in enumerate(ids, 1) if str(value) == str(client.telegram_id)
            )
        except StopIteration:
            self.clients_sheet.append_row(values, value_input_option="RAW")
        else:
            self.clients_sheet.update(
                f"A{row_number}:I{row_number}", [values], value_input_option="RAW"
            )

    async def append_history(
        self, telegram_id: int, created_at: datetime, user_message: str, assistant_message: str
    ) -> None:
        values = [_format_date(created_at), telegram_id, user_message, assistant_message]
        await asyncio.to_thread(self.history_sheet.append_row, values, value_input_option="RAW")

    async def get_history(self, telegram_id: int, limit: int = 10) -> list[HistoryEntry]:
        rows = await asyncio.to_thread(self.history_sheet.get_all_records)
        result = [
            HistoryEntry(
                _date(row["дата"]) or datetime.min.replace(tzinfo=timezone.utc),
                telegram_id,
                str(row["сообщение клиента"]),
                str(row["ответ Ивана"]),
            )
            for row in rows
            if str(row.get("telegram_id")) == str(telegram_id)
        ]
        return result[-limit:]


def _client_from_row(row: dict[str, object], telegram_id: int) -> ClientProfile:
    comment = _text(row.get("комментарии")) or ""
    volume, email, budget, skipped, last_name, extra = _parse_comment(comment)
    due, sent, extra = _split_followup(extra)
    return ClientProfile(
        telegram_id=telegram_id,
        username=_text(row.get("username")),
        name=_first_name(_text(row.get("имя клиента"))),
        last_name=last_name or _last_name_from_cell(_text(row.get("имя клиента"))),
        phone=_text(row.get("телефон")),
        landline=_parse_landline(comment),
        email=email,
        product=_text(row.get("интересующая продукция")),
        volume=volume,
        budget=budget,
        status=_text(row.get("статус клиента")) or "новый",
        first_contact_at=_date(row.get("дата первого обращения")),
        last_contact_at=_date(row.get("дата последнего обращения")),
        comment=extra,
        contact_skipped=skipped,
        followup_due_at=due,
        followup_sent=sent,
        needs_human="Нужен менеджер" in extra,
    )


def _parse_comment(
    comment: str,
) -> tuple[str | None, str | None, int | None, bool, str | None, str]:
    volume = None
    email = None
    skipped = False
    budget = None
    last_name = None
    extras: list[str] = []
    for part in [item.strip() for item in comment.split(" | ") if item.strip()]:
        if part.startswith("Объём: "):
            volume = part.removeprefix("Объём: ")
        elif part.startswith("Почта: "):
            email = part.removeprefix("Почта: ")
        elif part.startswith("Бюджет: "):
            raw_budget = part.removeprefix("Бюджет: ").replace("₽", "").replace(" ", "")
            if raw_budget.isdigit():
                budget = int(raw_budget)
        elif part.startswith("Фамилия: "):
            last_name = part.removeprefix("Фамилия: ")
        elif part.startswith("Городской телефон: "):
            continue
        elif part == "Без контакта":
            skipped = True
        else:
            extras.append(part)
    return volume, email, budget, skipped, last_name, " | ".join(extras)


def _parse_landline(comment: str) -> str | None:
    for part in [item.strip() for item in comment.split(" | ") if item.strip()]:
        if part.startswith("Городской телефон: "):
            value = part.removeprefix("Городской телефон: ").strip()
            return value if value.isdigit() and len(value) == 6 else None
    return None


def _split_followup(extra: str) -> tuple[datetime | None, bool, str]:
    due = None
    sent = False
    kept: list[str] = []
    for part in [item.strip() for item in extra.split(" | ") if item.strip()]:
        if part.startswith("Напомнить: "):
            due = _date(part.removeprefix("Напомнить: "))
        elif part == "Напоминание отправлено":
            sent = True
        else:
            kept.append(part)
    return due, sent, " | ".join(kept)


def _build_comment(client: ClientProfile) -> str:
    parts: list[str] = []
    if client.volume:
        parts.append(f"Объём: {client.volume}")
    if client.landline:
        parts.append(f"Городской телефон: {client.landline}")
    if client.email:
        parts.append(f"Почта: {client.email}")
    if client.contact_skipped:
        parts.append("Без контакта")
    if client.followup_due_at:
        parts.append(f"Напомнить: {client.followup_due_at.isoformat()}")
    if client.followup_sent:
        parts.append("Напоминание отправлено")
    if client.needs_human:
        parts.append("Нужен менеджер")
    if client.original_interests:
        parts.append(f"Исходный интерес: {', '.join(client.original_interests)}")
    if client.current_interest:
        parts.append(f"Текущий интерес: {client.current_interest}")
    extra = _split_followup(_parse_comment(client.comment or "")[5])[2]
    if client.last_name:
        parts.append(f"Фамилия: {client.last_name}")
    if client.budget is not None:
        parts.append(f"Бюджет: {client.budget} ₽")
    if extra:
        parts.append(extra)
    return " | ".join(parts)


def _format_date(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _date(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _display_name(client: ClientProfile) -> str:
    return " ".join(part for part in (client.name, client.last_name) if part)


def _first_name(value: str | None) -> str | None:
    if not value:
        return None
    return value.split()[0]


def _last_name_from_cell(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split()
    return " ".join(parts[1:]) or None
