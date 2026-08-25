from datetime import datetime, timezone

import pytest

from stokozavr_bot.google_sheets import CLIENT_HEADERS, HISTORY_HEADERS, GoogleSheetsCRMRepository
from stokozavr_bot.models import ClientProfile


class FakeWorksheet:
    def __init__(self, headers):
        self.rows = [headers]

    def row_values(self, row):
        return self.rows[row - 1] if len(self.rows) >= row else []

    def append_row(self, values, **kwargs):
        self.rows.append(list(values))

    def col_values(self, column):
        return [str(row[column - 1]) for row in self.rows]

    def get_all_records(self):
        return [dict(zip(self.rows[0], row, strict=False)) for row in self.rows[1:]]

    def update(self, range_name, values, **kwargs):
        row_number = int(range_name.split(":")[0][1:])
        self.rows[row_number - 1] = list(values[0])


class FakeBook:
    def __init__(self):
        self.sheets = {
            "Клиенты": FakeWorksheet(CLIENT_HEADERS),
            "История сообщений": FakeWorksheet(HISTORY_HEADERS),
        }

    def worksheet(self, title):
        return self.sheets[title]


@pytest.mark.asyncio
async def test_google_sheets_repository_uses_required_columns_and_upserts():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    client = ClientProfile(
        telegram_id=42,
        username="buyer",
        name="Анна",
        phone="+79991234567",
        product="оливки",
        volume="20 коробок",
        status="квалифицирован",
        first_contact_at=now,
        last_contact_at=now,
    )

    await repo.save_client(client)
    client.status = "передан менеджеру"
    await repo.save_client(client)
    loaded = await repo.get_client(42)

    assert book.sheets["Клиенты"].rows[0] == CLIENT_HEADERS
    assert len(book.sheets["Клиенты"].rows) == 2
    assert loaded.name == "Анна"
    assert loaded.volume == "20 коробок"
    assert loaded.status == "передан менеджеру"


@pytest.mark.asyncio
async def test_google_sheets_repository_loads_client_with_empty_comment():
    book = FakeBook()
    book.sheets["Клиенты"].rows.append(
        [42, "buyer", "Анна", "+799****4567", "оливки", "новый", "", "", None]
    )
    repo = GoogleSheetsCRMRepository(book)

    loaded = await repo.get_client(42)

    assert isinstance(loaded, ClientProfile)
    assert loaded.comment == ""
    assert loaded.volume is None


@pytest.mark.asyncio
async def test_google_sheets_repository_appends_and_limits_history():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for i in range(3):
        await repo.append_history(42, now, f"q{i}", f"a{i}")

    history = await repo.get_history(42, limit=2)

    assert book.sheets["История сообщений"].rows[0] == HISTORY_HEADERS
    assert [item.user_message for item in history] == ["q1", "q2"]
