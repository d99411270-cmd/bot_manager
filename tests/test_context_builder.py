from datetime import datetime, timezone

from stokozavr_bot.context_builder import build_model_context
from stokozavr_bot.models import ClientProfile, HistoryEntry

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
FIRST = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


def test_context_has_profile_missing_fields_stage_and_history_not_table_dump():
    profile = ClientProfile(
        telegram_id=9,
        username="dima",
        name="Дмитрий",
        phone="+79991234567",
        product="масло",
        volume="10 коробок",
        status="квалифицирован",
        comment="важный клиент",
        first_contact_at=FIRST,
        last_contact_at=NOW,
    )
    history = [
        HistoryEntry(FIRST, 9, "хочу масло", "Какой объём вам нужен?"),
        HistoryEntry(NOW, 9, "10 коробок", "Зафиксировал."),
    ]

    ctx = build_model_context(profile, history)

    assert set(ctx) == {
        "profile",
        "missing_fields",
        "deal_stage",
        "returning",
        "interests",
        "recent_history",
    }
    assert set(ctx["profile"]) == {
        "name",
        "last_name",
        "phone",
        "email",
        "product",
        "volume",
        "budget",
        "status",
        "comment",
        "username",
        "first_contact_at",
        "last_contact_at",
        "contact_skipped",
        "original_interests",
        "current_interest",
        "needs_human",
    }
    assert "telegram_id" not in ctx
    assert "telegram_id" not in ctx["profile"]
    assert ctx["profile"]["comment"] == "важный клиент"
    assert ctx["profile"]["username"] == "dima"
    assert ctx["profile"]["first_contact_at"] == FIRST.isoformat()
    assert ctx["profile"]["last_contact_at"] == NOW.isoformat()
    assert ctx["missing_fields"] == []
    assert ctx["deal_stage"] == "qualified"
    assert ctx["returning"] is True
    assert ctx["interests"] == ["масло"]
    assert ctx["recent_history"] == [
        {
            "user": "хочу масло",
            "assistant": "Какой объём вам нужен?",
            "at": FIRST.isoformat(),
        },
        {
            "user": "10 коробок",
            "assistant": "Зафиксировал.",
            "at": NOW.isoformat(),
        },
    ]


def test_incomplete_profile_lists_missing_fields_and_new_lead_stage():
    ctx = build_model_context(ClientProfile(telegram_id=1, status="новый"), [])

    assert ctx["profile"]["name"] is None
    assert ctx["profile"]["comment"] is None
    assert ctx["profile"]["first_contact_at"] is None
    assert ctx["missing_fields"] == ["name", "phone", "product", "volume"]
    assert ctx["deal_stage"] == "new_lead"
    assert ctx["returning"] is False
    assert ctx["interests"] == []
    assert ctx["recent_history"] == []


def test_context_uses_only_provided_history_not_full_table():
    profile = ClientProfile(telegram_id=2, name="Анна", status="ожидает телефон")
    history = [HistoryEntry(NOW, 2, f"u{i}", f"a{i}") for i in range(3)]

    ctx = build_model_context(profile, history[-2:])

    assert [row["user"] for row in ctx["recent_history"]] == ["u1", "u2"]
    assert ctx["deal_stage"] == "discovery"
    assert ctx["missing_fields"] == ["phone", "product", "volume"]
