from pathlib import Path

from stokozavr_bot.product_catalog import (
    _all_records,
    catalog_has_stock_status,
    generated_price_list,
    grounded_search_reply,
    listed_price_amounts,
    listed_stock_amounts,
    search,
    unit_price_quote,
)

REPO_CATALOG = Path(__file__).resolve().parents[1] / "catalog"


def test_unit_price_quote_calculates_confirmed_rice_price_per_kg():
    quote = unit_price_quote("рис длиннозёрный", "кг")

    assert quote is not None
    assert quote.unit_price == "85 ₽/кг"
    assert quote.total_quantity == "8 кг"
    assert quote.record.sku == "GRC-RICE-001"


def test_unit_price_quote_calculates_liters_and_rejects_ambiguous_packaging():
    liter_quote = unit_price_quote("сок яблочный", "л")
    assert liter_quote is not None
    assert liter_quote.unit_price == "148.33 ₽/л"

    assert unit_price_quote("товар с неясной фасовкой", "кг") is None


def test_unit_price_quote_calculates_each_beverage_and_tea_item_from_packaging():
    expected = {
        "сок яблочный": "148.33 ₽/шт",
        "лимонад цитрусовый": "120 ₽/шт",
        "вода питьевая негазированная": "50 ₽/шт",
        "чай чёрный": "58 ₽/шт",
    }

    for product, unit_price in expected.items():
        quote = unit_price_quote(product, "шт")
        assert quote is not None
        assert quote.unit_price == unit_price


def test_search_conserves_returns_all_primary_positions_without_competitors():
    result = search("консервы")

    assert result.count("SKU:") == 5
    assert all(
        sku in result
        for sku in (
            "CAN-PEAS-001",
            "CAN-CORN-001",
            "CAN-BEAN-001",
            "CAN-PICKLES-001",
            "CAN-TOMATO-001",
        )
    )
    assert "-ALT-" not in result
    assert "Урожайная Кладовая" not in result


def test_search_question_about_conserves_returns_all_primary_positions():
    result = search("какие консервы")

    assert result.count("SKU:") == 5
    assert "CAN-PEAS-001" in result
    assert "CAN-TOMATO-001" in result
    assert "-ALT-" not in result


def test_search_product_synonym_peas_returns_primary_position_only():
    result = search("горошек")

    assert "CAN-PEAS-001" in result
    assert "CAN-PEAS-ALT-001" not in result
    assert "Урожайная Кладовая" not in result


def test_search_product_synonym_corn_returns_sweet_corn_position():
    result = search("кукурузы")

    assert "CAN-CORN-001" in result
    assert "CAN-CORN-ALT-001" not in result


def test_search_inflected_rice_resolves_to_rice_not_no_match():
    result = search("риса")

    assert "GRC-RICE-001" in result
    assert "рис длиннозёрный" in result.lower()
    assert "подтверждённых позиций" not in result.lower()
    assert "GRC-RICE-ALT-001" not in result


def test_search_inflected_conservation_category_returns_all_positions():
    result = search("консервации")

    assert result.count("SKU:") == 5
    assert "CAN-BEAN-001" in result


def test_search_fresh_cucumber_modifier_selects_short_fruit_not_pickles():
    result = search("свежие огурцы")

    assert "VEG-CUCUMBER-001" in result
    assert "CAN-PICKLES-001" not in result
    assert "короткоплодные" in result.lower()


def test_search_short_fruit_modifier_selects_fresh_cucumber():
    result = search("огурцы короткоплодные")

    assert "VEG-CUCUMBER-001" in result
    assert "CAN-PICKLES-001" not in result


def test_search_pickled_modifier_selects_canned_cucumber():
    result = search("огурцы маринованные")

    assert "CAN-PICKLES-001" in result
    assert "VEG-CUCUMBER-001" not in result


def test_search_bare_cucumbers_may_list_both_fresh_and_pickled():
    result = search("огурцы")

    assert "VEG-CUCUMBER-001" in result
    assert "CAN-PICKLES-001" in result


def test_search_fruits_returns_apples_and_bananas():
    result = search("фрукты")

    lowered = result.lower()
    assert "фрукты" in lowered
    assert "fru-apple-001" in lowered
    assert "780" not in lowered


def test_search_apple_juice_returns_grounded_product_details():
    result = search("яблочный сок")

    lowered = result.lower()
    assert "сок яблочный" in lowered
    assert "северная капля" in lowered
    assert "sku: sok-apple-001" in lowered
    assert "6 x 1 л" in lowered
    assert "890 ₽" in result
    assert "много" in lowered
    assert "дата обновления: 2026-08-25" in lowered


def test_grounded_catalog_questions_change_between_adjacent_replies():
    result = search("яблочный сок")
    first = grounded_search_reply(result, "Пётр")
    second = grounded_search_reply(result, "Пётр", first)

    assert first and second
    assert first != second
    assert first.count("?") == second.count("?") == 1


def test_search_apple_juice_cheaper_never_returns_competitors():
    result = search("яблочный сок подешевле")

    lowered = result.lower()
    assert "северная капля" in lowered
    assert "росинка поля" not in lowered
    assert "990 ₽" not in result
    assert lowered.count("производитель:") == 1


def test_regular_apple_juice_search_does_not_dump_competitors():
    result = search("яблочный сок")

    assert "росинка поля" not in result.lower()
    assert "990 ₽" not in result


def test_search_competitor_request_returns_only_primary_product():
    result = search("вода подешевле")

    assert "росинка поля" not in result.lower()
    assert "водный круг" in result.lower()
    assert "источник луга" not in result.lower()


def test_empty_search_lists_categories_from_headings():
    result = search("")

    lowered = result.lower()
    assert "фрукты" in lowered
    assert "бакалея" in lowered
    assert "напитки" in lowered
    assert "консервац" in lowered
    assert "макарон" in lowered
    assert "масло" in lowered
    assert "яблок" not in lowered


def test_catalog_prefers_env_directory(tmp_path, monkeypatch):
    (tmp_path / "ovoshchi.md").write_text("# Овощи\n\n- Морковь\n", encoding="utf-8")
    monkeypatch.setenv("STOKOZAVR_CATALOG_DIR", str(tmp_path))

    found = search("овощи")
    listing = search("")

    assert "подтверждённых позиций" in found.lower()
    assert "овощи" in listing.lower()
    assert "яблок" not in found.lower()


def test_unknown_category_is_honest_and_lists_what_exists():
    result = search("молоко")

    lowered = result.lower()
    assert "не" in lowered
    assert "фрукты" in lowered or "бакалея" in lowered
    assert "яблок" not in lowered


def test_pyproject_force_includes_catalog_in_wheel():
    text = (
        Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8")
    )

    assert "force-include" in text
    assert '"catalog"' in text or "'catalog'" in text
    assert "stokozavr_bot/catalog" in text


def test_listed_price_amounts_include_fruit_prices():
    amounts = listed_price_amounts()

    assert {"890", "990", "720", "810", "300", "360"}.issubset(amounts)


def test_listed_stock_amounts_are_internal_only():
    amounts = listed_stock_amounts()

    assert amounts == set()
    assert catalog_has_stock_status() is True


def test_generated_price_list_contains_only_30_primary_products_without_stock_counts():
    price_list = generated_price_list()

    assert price_list.count("SKU:") == 30
    assert "-ALT-" not in price_list
    assert "конкур" not in price_list.lower()
    assert "остат" not in price_list.lower()
    assert not any(token in price_list.lower() for token in ("много", "мало", "нет в наличии"))
    assert "Цена:" in price_list
    assert "Фасовка:" in price_list


def test_local_catalog_has_exactly_30_primary_and_30_scoped_competitors():
    records = _all_records()
    primary = [record for record in records if not record.is_competitor]
    competitors = [record for record in records if record.is_competitor]

    assert len(primary) == 30
    assert len(competitors) == 30
    assert len({record.sku for record in records}) == 60
    assert {record.category for record in primary} == {
        "напитки",
        "овощи",
        "фрукты",
        "бакалея",
        "макароны",
        "масло",
        "консервация",
    }
    primary_by_sku = {record.sku: record for record in primary}
    for competitor in competitors:
        source = primary_by_sku[competitor.for_sku]
        assert competitor.category == source.category
        assert competitor.subcategory == source.subcategory
        assert competitor.packaging == source.packaging
        assert int(competitor.price.split()[0]) > int(source.price.split()[0])


DEAD_COMPETITOR_BRANDS = (
    "крупяной берег",
    "росинка поля",
    "белый колос",
    "источник луга",
    "яблоневый край",
    "сахарный дом",
    "белая мельница",
    "солнечный шиповник",
    "северная ягода",
    "янтарный лист",
)


def test_competitor_rows_use_retail_chain_stubs_not_brand_names():
    records = _all_records()
    competitors = [record for record in records if record.is_competitor]
    catalog_text = "\n".join(path.read_text(encoding="utf-8") for path in REPO_CATALOG.glob("*.md"))
    haystack = catalog_text.lower()

    assert competitors
    for competitor in competitors:
        assert competitor.manufacturer.lower() == "розничные сети"
        assert competitor.for_sku
        assert "₽" in competitor.price
    for brand in DEAD_COMPETITOR_BRANDS:
        assert brand not in haystack
    juice = next(item for item in competitors if item.for_sku == "SOK-APPLE-001")
    buckwheat = next(item for item in competitors if item.for_sku == "GRC-BUCKWHEAT-001")
    assert juice.price.startswith("990")
    assert buckwheat.price.startswith("810")


def test_every_primary_product_is_searchable_without_competitor_leakage():
    records = [record for record in _all_records() if not record.is_competitor]

    for record in records:
        result = search(record.subcategory)
        assert record.sku in result
        assert f"{record.sku}-ALT" not in result
        competitor = next(item for item in _all_records() if item.for_sku == record.sku)
        assert competitor.sku not in result


def test_comparison_returns_only_matching_primary():
    result = search("лимонад цитрусовый сравнить")

    assert "LIM-CITRUS-001" in result
    assert "LIM-CITRUS-ALT-001" not in result
    assert result.lower().count("производитель:") == 1
    assert "SOK-APPLE-001" not in result
    assert "SOK-APPLE-ALT-001" not in result


def test_explicit_comparison_opt_in_returns_matching_competitor_after_primary():
    result = search("лимонад цитрусовый сравнить", include_competitors=True)

    assert "LIM-CITRUS-001" in result
    assert "LIM-CITRUS-ALT-001" in result
    assert result.index("LIM-CITRUS-001") < result.index("LIM-CITRUS-ALT-001")
