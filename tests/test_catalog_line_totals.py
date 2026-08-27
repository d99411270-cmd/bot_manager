from stokozavr_bot.catalog_quotes import (
    QuoteFailure,
    combine_line_totals,
    line_total_quote,
    parse_requested_quantity,
)
from stokozavr_bot.product_catalog import CatalogRecord, parse_catalog_records, unit_price_quote


def test_line_total_quote_apples_20kg_is_exactly_two_boxes():
    quote = line_total_quote("яблоки", "20 кг")

    assert quote.record.sku == "FRU-APPLE-001"
    assert quote.pack_count == 2
    assert quote.requested_quantity == "20 кг"
    assert quote.requested_unit == "кг"
    assert quote.total == "1640 ₽"
    assert quote.total_amount == 1640
    assert "1640" in quote.allowed_amounts
    assert "820" in quote.allowed_amounts
    assert "2" in quote.human_line
    assert "короб" in quote.human_line


def test_line_total_quote_severnaya_kaplya_10_packs_is_8900():
    quote = line_total_quote("Северная Капля", "10 упаковок")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "SOK-APPLE-001"
    assert quote.pack_count == 10
    assert quote.requested_unit == "упаковка"
    assert quote.total == "8900 ₽"
    assert "8900" in quote.allowed_amounts
    assert "890" in quote.allowed_amounts


def test_line_total_quote_matches_inflected_manufacturer_in_quantity_question():
    quote = line_total_quote("10 упаковок Северной Капли это сколько?", "10 упаковок")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "SOK-APPLE-001"
    assert quote.pack_count == 10
    assert quote.total == "8900 ₽"


def test_line_total_quote_corn_36_cans_is_three_packs_and_allows_unit_price():
    quote = line_total_quote("кукуруза сладкая", "36 банок")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "CAN-CORN-001"
    assert quote.record.packaging == "12 x 340 г"
    assert quote.pack_count == 3
    assert quote.total == "2070 ₽"
    assert quote.allowed_amounts >= {"2070", "690", "57.50"}


def test_existing_unit_price_goldens_are_unchanged():
    rice = unit_price_quote("рис длиннозёрный", "кг")
    corn = unit_price_quote("кукуруза сладкая", "шт")
    juice = unit_price_quote("сок яблочный", "л")

    assert rice is not None
    assert rice.unit_price == "85 ₽/кг"
    assert rice.total_quantity == "8 кг"
    assert corn is not None
    assert corn.unit_price == "57.50 ₽/шт"
    assert juice is not None
    assert juice.unit_price == "148.33 ₽/л"


def test_rice_line_total_allows_derived_price_per_kg():
    quote = line_total_quote("рис длиннозёрный", "8 кг")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "GRC-RICE-001"
    assert quote.pack_count == 1
    assert quote.total == "680 ₽"
    assert quote.allowed_amounts >= {"680", "85"}


def test_line_total_quote_inflected_rice_resolves_to_rice_sku():
    quote = line_total_quote("риса", "8 кг")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "GRC-RICE-001"
    assert quote.pack_count == 1
    assert quote.total == "680 ₽"


def test_line_total_quote_potato_100kg_is_four_nets():
    quote = line_total_quote("картофель", "100 кг")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "VEG-POTATO-001"
    assert quote.pack_count == 4
    assert quote.requested_unit == "кг"
    assert quote.total == "3000 ₽"
    assert "4" in quote.human_line
    assert "сет" in quote.human_line
    assert quote.allowed_amounts >= {"3000", "750"}


def test_horns_10_packs_can_compose_with_another_line_total():
    horns = line_total_quote("рожки", "10 упаковок")
    apples = line_total_quote("яблоки", "20 кг")

    assert not isinstance(horns, QuoteFailure)
    assert not isinstance(apples, QuoteFailure)
    assert horns.record.sku == "PASTA-HORNS-001"
    assert horns.pack_count == 10
    assert horns.total == "3900 ₽"

    combined = combine_line_totals(horns, apples)
    assert combined.total_amount == 5540
    assert combined.allowed_amounts >= {"3900", "1640", "390", "820"}
    assert horns in combined.lines
    assert apples in combined.lines


def test_fresh_cucumbers_20kg_are_four_boxes_not_pickled():
    quote = line_total_quote("огурцы", "20 кг")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "VEG-CUCUMBER-001"
    assert quote.record.subcategory == "огурцы короткоплодные"
    assert quote.pack_count == 4
    assert quote.total == "2720 ₽"
    assert quote.allowed_amounts >= {"2720", "680"}
    assert "маринов" not in quote.record.subcategory
    assert quote.record.sku != "CAN-PICKLES-001"


def test_pickled_cucumbers_cannot_fill_a_kilogram_order():
    result = line_total_quote("огурцы маринованные", "20 кг")

    assert isinstance(result, QuoteFailure)


def test_fresh_modifier_selects_fresh_cucumber_for_pack_units():
    quote = line_total_quote("свежие огурцы", "2 упаковки")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "VEG-CUCUMBER-001"
    assert quote.pack_count == 2


def test_short_fruit_modifier_selects_fresh_cucumber_for_pack_units():
    quote = line_total_quote("короткоплодные огурцы", "2 короба")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "VEG-CUCUMBER-001"


def test_pickled_modifier_selects_pickles_for_pack_units():
    quote = line_total_quote("маринованные огурцы", "2 упаковки")

    assert not isinstance(quote, QuoteFailure)
    assert quote.record.sku == "CAN-PICKLES-001"


def test_bare_cucumbers_pack_units_stay_ambiguous():
    result = line_total_quote("огурцы", "2 упаковки")

    assert isinstance(result, QuoteFailure)
    assert result.reason == "ambiguous_product"


def test_ambiguous_category_does_not_invent_a_total():
    result = line_total_quote("бакалея", "10 упаковок")

    assert isinstance(result, QuoteFailure)
    assert result.reason == "ambiguous_product"


def test_non_integer_pack_count_offers_nearest_packs_without_inventing_a_blend():
    apples = line_total_quote("яблоки", "15 кг")
    corn = line_total_quote("кукуруза сладкая", "10 банок")

    assert type(apples).__name__ == "NearestPackQuote"
    assert apples.lower is not None and apples.upper is not None
    assert apples.lower.pack_count == 1
    assert apples.lower.total == "820 ₽"
    assert apples.upper.pack_count == 2
    assert apples.upper.total == "1640 ₽"
    assert "1230" not in apples.allowed_amounts
    assert type(corn).__name__ == "NearestPackQuote"
    assert corn.lower is None
    assert corn.upper is not None
    assert corn.upper.pack_count == 1
    assert corn.upper.total == "690 ₽"


def test_incomplete_record_does_not_invent_a_total():
    skipped = parse_catalog_records(
        """
# Тест
- SKU: X-002; Производитель: Аква; Фасовка: короб 10 кг; Цена: 100 ₽; Статус наличия: много; Дата обновления: 2026-08-25
"""
    )
    incomplete = CatalogRecord(
        category="тест",
        subcategory="сырой товар",
        sku="RAW-001",
        manufacturer="Аква",
        packaging="на развес",
        price="договорная",
        availability="много",
        updated_at="2026-08-25",
    )

    assert skipped == []
    result = line_total_quote("сырой товар", "10 кг", records=[incomplete])
    assert isinstance(result, QuoteFailure)
    assert result.reason == "incomplete_record"


def test_competitor_is_not_quoted_without_linked_opt_in():
    blocked = line_total_quote("FRU-APPLE-ALT-001", "20 кг")
    allowed = line_total_quote("FRU-APPLE-ALT-001", "20 кг", include_linked_competitor=True)

    assert isinstance(blocked, QuoteFailure)
    assert blocked.reason == "competitor_blocked"
    assert not isinstance(allowed, QuoteFailure)
    assert allowed.record.sku == "FRU-APPLE-ALT-001"
    assert allowed.record.is_competitor is True
    assert allowed.record.for_sku == "FRU-APPLE-001"
    assert allowed.pack_count == 2
    assert allowed.total == "1800 ₽"


def test_client_money_strings_are_not_treated_as_catalog_quantity():
    assert parse_requested_quantity("тысяч на 10") is None
    assert parse_requested_quantity("10000 ₽") is None
    result = line_total_quote("рожки", "10000 ₽")
    assert isinstance(result, QuoteFailure)
    assert result.reason == "invalid_quantity"


def test_quote_explicit_lines_potato_nets_and_horns_packs():
    from stokozavr_bot.catalog_quotes import quote_explicit_lines

    combined = quote_explicit_lines(
        "это не телефон, это объём. посчитайте итого: 4 сетки картофеля и 10 упаковок рожков"
    )

    assert combined is not None
    assert combined.total_amount == 6900
    assert "6900" in combined.allowed_amounts
    assert {line.record.sku for line in combined.lines} == {"VEG-POTATO-001", "PASTA-HORNS-001"}


def test_quantity_parser_is_general_not_phrase_specific():
    twenty_kg = parse_requested_quantity("нужно 20 кг, это сколько?")
    ten_packs = parse_requested_quantity("10 упаковок это сколько")
    cans = parse_requested_quantity("36 банок")
    nets = parse_requested_quantity("4 сетки")

    assert twenty_kg is not None and twenty_kg.amount == 20 and twenty_kg.unit == "кг"
    assert ten_packs is not None and ten_packs.amount == 10 and ten_packs.unit == "упаковка"
    assert cans is not None and cans.amount == 36 and cans.unit == "банка"
    assert nets is not None and nets.amount == 4 and nets.unit == "сетка"


def test_quantity_parser_reads_word_number_plus_container():
    two_boxes = parse_requested_quantity("два короба сколько?")
    two_packs = parse_requested_quantity("две упаковки")
    two_nets = parse_requested_quantity("две сетки")
    digit_boxes = parse_requested_quantity("2 короба")

    assert two_boxes is not None and two_boxes.amount == 2 and two_boxes.unit == "короб"
    assert two_packs is not None and two_packs.amount == 2 and two_packs.unit == "упаковка"
    assert two_nets is not None and two_nets.amount == 2 and two_nets.unit == "сетка"
    assert digit_boxes is not None and digit_boxes.amount == 2 and digit_boxes.unit == "короб"


def test_quantity_parser_prefers_asked_pack_count_over_quoted_packaging_size():
    asked = parse_requested_quantity(
        "вы же сами сказали короб 10 кг 820 рублей. два короба сколько?"
    )

    assert asked is not None
    assert asked.amount == 2
    assert asked.unit == "короб"
    assert asked.raw.lower().startswith("два") or "2" in asked.raw


def test_line_total_catalog_result_exposes_confirmed_amounts_for_future_safety():
    from stokozavr_bot.product_catalog import line_total_catalog_result

    payload = line_total_catalog_result("яблоки", "20 кг")

    assert payload is not None
    text, quote = payload
    assert quote.record.sku == "FRU-APPLE-001"
    assert "FRU-APPLE-001" in text
    assert "Подтверждённый расчёт:" in text
    assert "1640" in text
    assert "2" in text
    assert line_total_catalog_result("бакалея", "10 упаковок") is None
