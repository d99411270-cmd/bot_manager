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
async def test_google_sheets_persists_email_and_skipped_contact():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    client = ClientProfile(
        telegram_id=7,
        name="Анна",
        landline="646647",
        email="anna@shop.ru",
        product="груши",
        volume="5 ящиков",
        status="уточнение объёма",
        contact_skipped=False,
        first_contact_at=now,
        last_contact_at=now,
        comment="важный",
    )
    await repo.save_client(client)
    loaded = await repo.get_client(7)

    assert loaded.email == "anna@shop.ru"
    assert loaded.landline == "646647"
    assert loaded.volume == "5 ящиков"
    assert loaded.comment == "важный"
    assert "Городской телефон: 646647" in book.sheets["Клиенты"].rows[1][-1]
    assert "Почта: anna@shop.ru" in book.sheets["Клиенты"].rows[1][-1]

    client.email = None
    client.contact_skipped = True
    await repo.save_client(client)
    loaded = await repo.get_client(7)
    assert loaded.email is None
    assert loaded.contact_skipped is True
    assert "Без контакта" in book.sheets["Клиенты"].rows[1][-1]


@pytest.mark.asyncio
async def test_google_sheets_persists_price_list_request_and_success_timestamp():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    client = ClientProfile(
        telegram_id=70,
        name="Анна",
        price_list_requested=True,
        price_list_sent_at=now,
    )

    await repo.save_client(client)
    loaded = await repo.get_client(70)

    assert loaded.price_list_requested is True
    assert loaded.price_list_sent_at == now
    assert "Прайс запрошен" in book.sheets["Клиенты"].rows[1][-1]
    assert f"Прайс отправлен: {now.isoformat()}" in book.sheets["Клиенты"].rows[1][-1]


@pytest.mark.asyncio
async def test_google_sheets_stores_surname_separately():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    await repo.save_client(
        ClientProfile(
            telegram_id=8,
            name="Сергей",
            last_name="Иванов",
            first_contact_at=now,
            last_contact_at=now,
        )
    )
    loaded = await repo.get_client(8)
    assert loaded.name == "Сергей"
    assert loaded.last_name == "Иванов"
    assert book.sheets["Клиенты"].rows[1][2] == "Сергей Иванов"
    assert "Фамилия: Иванов" in book.sheets["Клиенты"].rows[1][-1]


@pytest.mark.asyncio
async def test_google_sheets_persists_catalog_no_match_query_without_product():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    client = ClientProfile(
        telegram_id=71,
        name="Энрике",
        phone="+799****0001",
        status="уточнение продукта",
        catalog_no_match_query="наполнитель для кошачьего туалета турецкий",
        comment="важный клиент",
    )

    await repo.save_client(client)
    loaded = await repo.get_client(71)

    assert loaded.product is None
    assert loaded.catalog_no_match_query == "наполнитель для кошачьего туалета турецкий"
    assert loaded.needs_human is False
    assert (
        "Товар не найден: наполнитель для кошачьего туалета турецкий"
        in (book.sheets["Клиенты"].rows[1][-1])
    )


@pytest.mark.asyncio
async def test_google_sheets_roundtrips_original_and_current_interest():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    await repo.save_client(
        ClientProfile(
            telegram_id=72,
            name="Анна",
            product="сок",
            original_interests=["творожки"],
            current_interest="сок",
            first_contact_at=now,
            last_contact_at=now,
        )
    )
    loaded = await repo.get_client(72)

    assert loaded.original_interests == ["творожки"]
    assert loaded.current_interest == "сок"
    comment = book.sheets["Клиенты"].rows[1][-1]
    assert "Исходный интерес: творожки" in comment or "Интересы" in comment
    assert "Текущий интерес: сок" in comment or "Интересы" in comment


@pytest.mark.asyncio
async def test_google_sheets_roundtrips_interests_with_comma_and_pipe():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    await repo.save_client(
        ClientProfile(
            telegram_id=73,
            name="Анна",
            product="рис",
            original_interests=["огурцы, свежие"],
            current_interest="рис | крупа",
            first_contact_at=now,
            last_contact_at=now,
        )
    )
    loaded = await repo.get_client(73)

    assert loaded.original_interests == ["огурцы, свежие"]
    assert loaded.current_interest == "рис | крупа"


@pytest.mark.asyncio
async def test_google_sheets_reads_legacy_plain_interest_comments():
    book = FakeBook()
    book.sheets["Клиенты"].rows.append(
        [
            74,
            "",
            "Анна",
            "",
            "сок",
            "новый",
            "",
            "",
            "Исходный интерес: творожки | Текущий интерес: сок",
        ]
    )
    repo = GoogleSheetsCRMRepository(book)
    loaded = await repo.get_client(74)

    assert loaded.original_interests == ["творожки"]
    assert loaded.current_interest == "сок"


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


@pytest.mark.asyncio
async def test_google_sheets_roundtrips_pause_volume_prompt():
    book = FakeBook()
    repo = GoogleSheetsCRMRepository(book)
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    await repo.save_client(
        ClientProfile(
            telegram_id=75,
            name="Таня",
            product="морковь",
            current_interest="морковь",
            pause_volume_prompt=True,
            first_contact_at=now,
            last_contact_at=now,
        )
    )
    loaded = await repo.get_client(75)
    assert loaded is not None
    assert loaded.pause_volume_prompt is True
    comment = book.sheets["Клиенты"].rows[1][-1]
    assert "Не спрашивать объём" in comment
    assert comment.count("Не спрашивать объём") == 1
