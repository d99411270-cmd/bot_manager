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
                comment = _text(row.get("комментарии")) or ""
                volume = None
                if comment.startswith("Объём: "):
                    volume = comment.removeprefix("Объём: ").split(" | ", 1)[0]
                return ClientProfile(
                    telegram_id=telegram_id,
                    username=_text(row.get("username")),
                    name=_text(row.get("имя клиента")),
                    phone=_text(row.get("телефон")),
                    product=_text(row.get("интересующая продукция")),
                    volume=volume,
                    status=_text(row.get("статус клиента")) or "новый",
                    first_contact_at=_date(row.get("дата первого обращения")),
                    last_contact_at=_date(row.get("дата последнего обращения")),
                    comment=comment,
                )
        return None

    async def save_client(self, client: ClientProfile) -> None:
        await asyncio.to_thread(self._save_client_sync, client)

    def _save_client_sync(self, client: ClientProfile) -> None:
        comment = client.comment or ""
        if client.volume:
            volume_note = f"Объём: {client.volume}"
            if not comment.startswith(volume_note):
                comment = volume_note + (f" | {comment}" if comment else "")
        values = [
            client.telegram_id,
            client.username or "",
            client.name or "",
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
