from stokozavr_bot.models import ClientProfile
from stokozavr_bot.service import limit_competitor_mentions


def client() -> ClientProfile:
    return ClientProfile(telegram_id=700)


def test_zero_mentions_stays_zero():
    profile = client()

    assert limit_competitor_mentions(profile, "Цена подтверждена.") == "Цена подтверждена."
    assert profile.competitor_mentions == 0


def test_one_mention_is_allowed_and_counted():
    profile = client()

    result = limit_competitor_mentions(profile, "Сравню с конкурентом по этой позиции.")

    assert "конкурентом" in result
    assert profile.competitor_mentions == 1


def test_two_mentions_are_allowed_only_after_a_substantive_turn():
    profile = client()

    first = limit_competitor_mentions(profile, "Есть вариант конкурента.")
    profile.competitor_last_reply = False
    second = limit_competitor_mentions(profile, "Покажу сравнение цены.")

    assert "конкурента" in first
    assert "сравнение" in second
    assert profile.competitor_mentions == 2


def test_third_mention_is_replaced_with_safe_reply():
    profile = client()
    profile.competitor_mentions = 2
    profile.competitor_last_reply = False

    result = limit_competitor_mentions(profile, "Могу предложить альтернативу.")

    assert result == "Актуальную информацию уточню и вернусь к вам."
    assert profile.competitor_mentions == 2


def test_two_competitor_answers_cannot_be_consecutive():
    profile = client()

    first = limit_competitor_mentions(profile, "Сравню с конкурентом.")
    second = limit_competitor_mentions(profile, "Ещё один вариант.")

    assert "конкурентом" in first
    assert second == "Актуальную информацию уточню и вернусь к вам."
    assert profile.competitor_mentions == 1
