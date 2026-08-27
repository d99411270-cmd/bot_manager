from __future__ import annotations

import asyncio
import base64
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
CATALOG_NO_MATCH_MARKER = "Товар не найден: "
INTERESTS_MARKER = "Интересы:"
PAUSE_VOLUME_MARKER = "Не спрашивать объём"


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
    price_list_requested, price_list_sent_at, extra = _split_price_list_state(extra)
    catalog_no_match_query, extra = _split_catalog_no_match(extra)
    original_interests, current_interest, extra = _split_interests(extra)
    pause_volume_prompt, extra = _split_pause_volume(extra)
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
        original_interests=original_interests,
        current_interest=current_interest,
        needs_human="Нужен менеджер" in extra,
        price_list_requested=price_list_requested,
        price_list_sent_at=price_list_sent_at,
        catalog_no_match_query=catalog_no_match_query,
        pause_volume_prompt=pause_volume_prompt,
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


def _split_price_list_state(extra: str) -> tuple[bool, datetime | None, str]:
    requested = False
    sent_at = None
    kept: list[str] = []
    for part in [item.strip() for item in extra.split(" | ") if item.strip()]:
        if part == "Прайс запрошен":
            requested = True
        elif part.startswith("Прайс отправлен: "):
            sent_at = _date(part.removeprefix("Прайс отправлен: "))
        else:
            kept.append(part)
    return requested, sent_at, " | ".join(kept)


def _split_catalog_no_match(extra: str) -> tuple[str | None, str]:
    query = None
    kept: list[str] = []
    for part in [item.strip() for item in extra.split(" | ") if item.strip()]:
        if part.startswith(CATALOG_NO_MATCH_MARKER):
            query = part.removeprefix(CATALOG_NO_MATCH_MARKER).strip() or None
        else:
            kept.append(part)
    return query, " | ".join(kept)


def _split_pause_volume(extra: str) -> tuple[bool, str]:
    paused = False
    kept: list[str] = []
    for part in [item.strip() for item in extra.split(" | ") if item.strip()]:
        if part == PAUSE_VOLUME_MARKER:
            paused = True
        else:
            kept.append(part)
    return paused, " | ".join(kept)


def _split_interests(extra: str) -> tuple[list[str] | None, str | None, str]:
    original: list[str] | None = None
    current: str | None = None
    kept: list[str] = []
    for part in [item.strip() for item in extra.split(" | ") if item.strip()]:
        decoded = _decode_interests_part(part)
        if decoded is not None:
            original, current = decoded
            continue
        if part.startswith("Исходный интерес: "):
            raw = part.removeprefix("Исходный интерес: ")
            values = [item.strip() for item in raw.split(",") if item.strip()]
            original = values or None
        elif part.startswith("Текущий интерес: "):
            current = part.removeprefix("Текущий интерес: ").strip() or None
        else:
            kept.append(part)
    return original, current, " | ".join(kept)


def _decode_interests_part(part: str) -> tuple[list[str] | None, str | None] | None:
    if not part.startswith(INTERESTS_MARKER):
        return None
    blob = part.removeprefix(INTERESTS_MARKER).strip()
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    original_raw = payload.get("o", payload.get("original"))
    current_raw = payload.get("c", payload.get("current"))
    original: list[str] | None
    if isinstance(original_raw, list):
        original = [str(item) for item in original_raw if str(item).strip()] or None
    else:
        original = None
    current = (
        str(current_raw).strip() if isinstance(current_raw, str) and current_raw.strip() else None
    )
    return original, current


def _encode_interests(original: list[str] | None, current: str | None) -> str | None:
    if not original and not current:
        return None
    payload = {"o": list(original or []), "c": current}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"{INTERESTS_MARKER}{encoded}"


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
    if client.price_list_requested:
        parts.append("Прайс запрошен")
    if client.price_list_sent_at:
        parts.append(f"Прайс отправлен: {client.price_list_sent_at.isoformat()}")
    if client.needs_human:
        parts.append("Нужен менеджер")
    if client.pause_volume_prompt:
        parts.append(PAUSE_VOLUME_MARKER)
    encoded_interests = _encode_interests(client.original_interests, client.current_interest)
    if encoded_interests:
        parts.append(encoded_interests)
    if client.catalog_no_match_query:
        query = client.catalog_no_match_query.replace("|", "/")
        parts.append(f"{CATALOG_NO_MATCH_MARKER}{query}")
    extra = _split_followup(_parse_comment(client.comment or "")[5])[2]
    extra = _split_price_list_state(extra)[2]
    extra = _split_catalog_no_match(extra)[1]
    extra = _split_interests(extra)[2]
    extra = _split_pause_volume(extra)[1]
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
