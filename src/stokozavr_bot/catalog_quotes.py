from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from stokozavr_bot.catalog_tokens import (
    catalog_record_score,
    catalog_word_tokens,
)

if TYPE_CHECKING:
    from stokozavr_bot.product_catalog import CatalogRecord

_UNIT_ALIASES = {
    "кг": "кг",
    "килограмм": "кг",
    "килограмма": "кг",
    "килограммов": "кг",
    "л": "л",
    "литр": "л",
    "литра": "л",
    "литров": "л",
    "шт": "шт",
    "штука": "шт",
    "штуки": "шт",
    "штук": "шт",
    "упаковка": "упаковка",
    "упаковки": "упаковка",
    "упаковок": "упаковка",
    "уп": "упаковка",
    "короб": "короб",
    "короба": "короб",
    "коробов": "короб",
    "коробка": "короб",
    "коробки": "короб",
    "коробок": "короб",
    "сетка": "сетка",
    "сетки": "сетка",
    "сеток": "сетка",
    "мешок": "мешок",
    "мешка": "мешок",
    "мешков": "мешок",
    "банка": "банка",
    "банки": "банка",
    "банок": "банка",
    "бутылка": "бутылка",
    "бутылки": "бутылка",
    "бутылок": "бутылка",
}

_CONTAINER_ALIASES = {
    "короб": "короб",
    "коробка": "короб",
    "сетка": "сетка",
    "мешок": "мешок",
    "упаковка": "упаковка",
    "ящик": "ящик",
    "канистра": "канистра",
}

_MASS = {"г": Decimal("0.001"), "кг": Decimal(1)}
_VOLUME = {"мл": Decimal("0.001"), "л": Decimal(1)}
_PACK_UNITS = frozenset({"упаковка", "короб", "сетка", "мешок", "ящик", "канистра"})
_PIECE_UNITS = frozenset({"шт", "банка", "бутылка"})
_QUANTITY_UNIT = (
    r"кг|килограмм\w*|л|литр\w*|шт|штук\w*|упаков\w*|уп\b|"
    r"короб\w*|сет(?:ок|к\w*)|мешк\w*|банка|банки|банок|бутылк\w*"
)
_WORD_NUMBERS = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "двое": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}
_WORD_NUMBER_RE = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_PACKAGING_CONTAINER_RE = re.compile(r"(?:короб(?:ка)?|сетка|мешок|упаковка|ящик|канистра)\s+$")


@dataclass(frozen=True, slots=True)
class RequestedQuantity:
    amount: Decimal
    unit: str
    raw: str


@dataclass(frozen=True, slots=True)
class PackagingSpec:
    container: str
    content_amount: Decimal
    content_unit: str
    piece_count: int | None = None


@dataclass(frozen=True, slots=True)
class LineTotalQuote:
    record: CatalogRecord
    pack_count: int
    requested_quantity: str
    requested_unit: str
    total_amount: Decimal
    total: str
    human_line: str
    allowed_amounts: frozenset[str]


@dataclass(frozen=True, slots=True)
class QuoteFailure:
    reason: str


@dataclass(frozen=True, slots=True)
class CompositeLineTotals:
    lines: tuple[LineTotalQuote, ...]
    total_amount: Decimal
    allowed_amounts: frozenset[str]


def parse_requested_quantity(text: str) -> RequestedQuantity | None:
    items = parse_requested_quantities(text)
    return items[0][2] if items else None


def parse_requested_quantities(text: str) -> list[tuple[int, int, RequestedQuantity]]:
    found: list[tuple[int, int, RequestedQuantity]] = []
    lowered = (text or "").lower().replace("ё", "е")
    pattern = rf"(?:(\d+(?:[.,]\d+)?)|(?<![а-яa-z])({_WORD_NUMBER_RE}))\s*({_QUANTITY_UNIT})"
    for match in re.finditer(pattern, lowered):
        if re.search(r"₽|руб", lowered[match.end() : match.end() + 4]):
            continue
        unit = _normalize_unit(match.group(3))
        if unit is None:
            continue
        if match.group(1) is not None:
            amount = _decimal(match.group(1))
            if _PACKAGING_CONTAINER_RE.search(lowered[: match.start()]) and unit not in _PACK_UNITS:
                continue
        else:
            amount = Decimal(_WORD_NUMBERS[match.group(2)])
        if amount <= 0:
            continue
        found.append(
            (
                match.start(),
                match.end(),
                RequestedQuantity(amount=amount, unit=unit, raw=match.group(0)),
            )
        )
    return found


def quote_explicit_lines(text: str) -> CompositeLineTotals | None:
    spans = parse_requested_quantities(text)
    if len(spans) < 2:
        return None
    quotes: list[LineTotalQuote] = []
    for index, (start, end, quantity) in enumerate(spans):
        previous_end = spans[index - 1][1] if index else 0
        next_start = spans[index + 1][0] if index + 1 < len(spans) else len(text)
        after = text[end:next_start]
        before = text[previous_end:start]
        nearby = after if _product_terms(after) else before
        quoted = line_total_quote(nearby, quantity.raw)
        if isinstance(quoted, LineTotalQuote):
            quotes.append(quoted)
    if len(quotes) < 2:
        return None
    return combine_line_totals(*quotes)


def parse_packaging(packaging: str) -> PackagingSpec | None:
    text = (packaging or "").lower().strip()
    multi = re.fullmatch(
        r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл|шт|штук)",
        text,
    )
    if multi:
        count = _decimal(multi.group(1))
        size = _decimal(multi.group(2))
        unit = multi.group(3)
        if count <= 0 or size <= 0:
            return None
        piece_count = int(count) if count == count.to_integral_value() else None
        if unit in _MASS:
            return PackagingSpec(
                container="упаковка",
                content_amount=count * size * _MASS[unit],
                content_unit="кг",
                piece_count=piece_count,
            )
        if unit in _VOLUME:
            return PackagingSpec(
                container="упаковка",
                content_amount=count * size * _VOLUME[unit],
                content_unit="л",
                piece_count=piece_count,
            )
        return PackagingSpec(
            container="упаковка",
            content_amount=count * size,
            content_unit="шт",
            piece_count=piece_count,
        )
    container = re.fullmatch(
        r"(короб(?:ка)?|сетка|мешок|упаковка|ящик|канистра)\s+"
        r"(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл)",
        text,
    )
    if not container:
        return None
    label = _CONTAINER_ALIASES[container.group(1)]
    amount = _decimal(container.group(2))
    unit = container.group(3)
    if amount <= 0:
        return None
    if unit in _MASS:
        return PackagingSpec(label, amount * _MASS[unit], "кг")
    return PackagingSpec(label, amount * _VOLUME[unit], "л")


def combine_line_totals(*quotes: LineTotalQuote) -> CompositeLineTotals:
    total = sum((quote.total_amount for quote in quotes), Decimal(0))
    allowed: set[str] = set()
    for quote in quotes:
        allowed.update(quote.allowed_amounts)
    if quotes:
        allowed.add(_format_amount(total))
    return CompositeLineTotals(
        lines=tuple(quotes), total_amount=total, allowed_amounts=frozenset(allowed)
    )


def line_total_quote(
    product: str,
    quantity: str,
    *,
    include_linked_competitor: bool = False,
    records: Iterable[CatalogRecord] | None = None,
) -> LineTotalQuote | QuoteFailure:
    requested = parse_requested_quantity(quantity)
    if requested is None:
        return QuoteFailure("invalid_quantity")
    pool = list(records) if records is not None else _catalog_records()
    selected = _select_record(pool, product, requested, include_linked_competitor)
    if isinstance(selected, QuoteFailure):
        return selected
    return _quote_record(selected, requested)


def _catalog_records() -> list[CatalogRecord]:
    from stokozavr_bot.product_catalog import _all_records

    return list(_all_records())


def _select_record(
    records: Iterable[CatalogRecord],
    product: str,
    requested: RequestedQuantity,
    include_linked_competitor: bool,
) -> CatalogRecord | QuoteFailure:
    terms = _product_terms(product)
    if not terms:
        return QuoteFailure("no_match")
    scored: list[tuple[int, CatalogRecord]] = []
    for record in records:
        score = catalog_record_score(
            product,
            category=record.category,
            subcategory=record.subcategory,
            sku=record.sku,
            manufacturer=record.manufacturer,
        )
        if score > 0:
            scored.append((score, record))
    if not scored:
        return QuoteFailure("no_match")
    best = max(score for score, _record in scored)
    winners = [record for score, record in scored if score == best]
    primary = [record for record in winners if not record.is_competitor]
    linked = [record for record in winners if record.is_competitor and record.for_sku]
    candidates: list[CatalogRecord]
    if include_linked_competitor:
        candidates = primary + linked
    else:
        candidates = primary
        if not candidates and linked:
            return QuoteFailure("competitor_blocked")
    if not candidates:
        return QuoteFailure("no_match")
    if len(candidates) == 1:
        return candidates[0]
    compatible = [record for record in candidates if _resolution_reason(record, requested) == "ok"]
    if len(compatible) == 1:
        return compatible[0]
    if len(compatible) > 1:
        return QuoteFailure("ambiguous_product")
    reasons = {_resolution_reason(record, requested) for record in candidates}
    if reasons == {"non_integer_packs"}:
        return QuoteFailure("non_integer_packs")
    if reasons == {"incomplete_record"}:
        return QuoteFailure("incomplete_record")
    if "incompatible_unit" in reasons:
        return QuoteFailure("incompatible_unit")
    return QuoteFailure("ambiguous_product")


def _quote_record(
    record: CatalogRecord, requested: RequestedQuantity
) -> LineTotalQuote | QuoteFailure:
    reason = _resolution_reason(record, requested)
    if reason != "ok":
        return QuoteFailure(reason)
    spec = parse_packaging(record.packaging)
    price = _price_decimal(record.price)
    pack_count = _pack_count_for(spec, requested) if spec is not None else None
    if spec is None or price is None or pack_count is None or pack_count <= 0:
        return QuoteFailure("incomplete_record")
    total = (price * pack_count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    allowed = {_format_amount(price), _format_amount(total)}
    allowed.update(_derived_unit_prices(spec, price))
    return LineTotalQuote(
        record=record,
        pack_count=pack_count,
        requested_quantity=requested.raw,
        requested_unit=requested.unit,
        total_amount=total,
        total=f"{_format_amount(total)} ₽",
        human_line=_human_line(record, spec, pack_count, total),
        allowed_amounts=frozenset(allowed),
    )


def _resolution_reason(record: CatalogRecord, requested: RequestedQuantity) -> str:
    spec = parse_packaging(record.packaging)
    price = _price_decimal(record.price)
    if spec is None or price is None:
        return "incomplete_record"
    pack_count = _pack_count_for(spec, requested)
    if pack_count is None:
        return "incompatible_unit"
    if pack_count == 0:
        return "non_integer_packs"
    return "ok"


def _pack_count_for(spec: PackagingSpec, requested: RequestedQuantity) -> int | None:
    unit = requested.unit
    if unit in _PACK_UNITS:
        if unit != "упаковка" and unit != spec.container:
            return None
        if unit == "упаковка" and spec.container not in _PACK_UNITS:
            return None
        return _whole_count(requested.amount)
    if unit in _PIECE_UNITS:
        if spec.piece_count is None or spec.piece_count <= 0:
            return None
        return _whole_count(requested.amount / Decimal(spec.piece_count))
    if unit != spec.content_unit or spec.content_amount <= 0:
        return None
    return _whole_count(requested.amount / spec.content_amount)


def _whole_count(value: Decimal) -> int | None:
    if value <= 0:
        return None
    integral = value.to_integral_value(rounding=ROUND_HALF_UP)
    if (value - integral).copy_abs() > Decimal("0.000000001"):
        return 0
    count = int(integral)
    return count if count >= 1 else None


def derived_allowed_amounts(packaging: str, price: str) -> frozenset[str]:
    """Pack price plus ₽/кг, ₽/л and ₽/шт implied by an unambiguous pack."""
    spec = parse_packaging(packaging)
    amount = _price_decimal(price)
    if spec is None or amount is None:
        return frozenset()
    allowed = {_format_amount(amount)}
    allowed.update(_derived_unit_prices(spec, amount))
    return frozenset(allowed)


def _derived_unit_prices(spec: PackagingSpec, price: Decimal) -> list[str]:
    amounts: list[str] = []
    if spec.content_amount > 0:
        amounts.append(
            _format_amount(
                (price / spec.content_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        )
    if spec.piece_count:
        amounts.append(
            _format_amount(
                (price / Decimal(spec.piece_count)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        )
    return amounts


def _human_line(record: CatalogRecord, spec: PackagingSpec, pack_count: int, total: Decimal) -> str:
    name = record.subcategory
    label = _plural_container(spec.container, pack_count)
    if spec.container != "упаковка" and spec.content_unit in {"кг", "л"}:
        detail = (
            f"{pack_count} {label} по {_format_amount(spec.content_amount)} {spec.content_unit}"
        )
    else:
        detail = f"{pack_count} {label}"
    return f"{name}: {detail}; {_format_amount(total)} ₽"


def _plural_container(container: str, count: int) -> str:
    forms = {
        "короб": ("короб", "короба", "коробов"),
        "сетка": ("сетка", "сетки", "сеток"),
        "мешок": ("мешок", "мешка", "мешков"),
        "упаковка": ("упаковка", "упаковки", "упаковок"),
        "ящик": ("ящик", "ящика", "ящиков"),
        "канистра": ("канистра", "канистры", "канистр"),
    }
    one, few, many = forms.get(container, (container, container, container))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _product_terms(product: str) -> list[str]:
    skip = set(_UNIT_ALIASES) | {
        "за",
        "это",
        "сколько",
        "цена",
        "цену",
        "нужно",
        "надо",
        "будет",
        "хочу",
        "ещё",
        "еще",
        "есть",
    }
    terms: list[str] = []
    for token in catalog_word_tokens(product or ""):
        if len(token) < 3 or token.isdigit() or token in skip:
            continue
        if token not in terms:
            terms.append(token)
    return terms


def _price_decimal(price: str) -> Decimal | None:
    match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб)", (price or "").lower())
    if not match:
        return None
    return _decimal(match.group(1).replace(" ", ""))


def _format_amount(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == quantized.to_integral_value():
        return str(int(quantized))
    return f"{quantized:.2f}"


def _decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", "."))


def _normalize_unit(raw: str) -> str | None:
    token = raw.lower()
    if token in _UNIT_ALIASES:
        return _UNIT_ALIASES[token]
    for prefix, unit in (
        ("килограмм", "кг"),
        ("литр", "л"),
        ("штук", "шт"),
        ("упаков", "упаковка"),
        ("короб", "короб"),
        ("сет", "сетка"),
        ("мешк", "мешок"),
        ("бутыл", "бутылка"),
    ):
        if token.startswith(prefix):
            return unit
    return None
