from stokozavr_bot.models import ClientProfile
from stokozavr_bot.service import limit_competitor_mentions


def client() -> ClientProfile:
    return ClientProfile(telegram_id=700)


def test_zero_mentions_stays_zero():
    profile = client()

    assert limit_competitor_mentions(profile, "Цена подтверждена.") == "Цена подтверждена."
    assert profile.competitor_mentions == 0


def test_competitor_mention_is_always_suppressed():
    profile = client()

    result = limit_competitor_mentions(profile, "Сравню с конкурентом по этой позиции.")

    assert "конкурент" not in result.lower()
    assert profile.competitor_mentions == 0


def test_comparison_language_is_always_suppressed():
    profile = client()

    first = limit_competitor_mentions(profile, "Есть вариант конкурента.")
    profile.competitor_last_reply = False
    second = limit_competitor_mentions(profile, "Покажу сравнение цены.")

    assert "конкурент" not in first.lower()
    assert "сравнение" not in second.lower()
    assert profile.competitor_mentions == 0


def test_third_mention_is_replaced_with_safe_reply():
    profile = client()
    profile.competitor_mentions = 2
    profile.competitor_last_reply = False

    result = limit_competitor_mentions(profile, "Могу предложить альтернативу.")

    assert "конкурент" not in result.lower()
    assert profile.competitor_mentions == 2


def test_two_competitor_answers_cannot_be_consecutive():
    profile = client()

    first = limit_competitor_mentions(profile, "Сравню с конкурентом.")
    second = limit_competitor_mentions(profile, "Ещё один вариант.")

    assert "конкурент" not in first.lower()
    assert "вариант" not in second.lower()
    assert profile.competitor_mentions == 0
