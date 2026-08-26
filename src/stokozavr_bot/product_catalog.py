from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from stokozavr_bot.catalog_quotes import LineTotalQuote

_REPO_CATALOG = Path(__file__).resolve().parents[2] / "catalog"
_FILENAME_ALIASES = {
    "frukty": ("фрукты", "фрукт"),
    "ovoshi": ("овощи", "овощ"),
    "bakaleya": ("бакалея", "бакале"),
    "napitki": ("напитки", "напиток"),
    "konservy": ("консервы", "консервация", "консерв"),
    "makarony": ("макароны", "макарон"),
    "maslo": ("масло", "масла"),
}

# Customer wording is normalized here, before matching catalog records.  Keep
# the map focused on product/category vocabulary: DeepSeek still interprets
# intent and receives the original message unchanged.
_QUERY_ALIASES = {
    "консервы": "консервация",
    "консерв": "консервация",
    "консервации": "консервация",
    "консервацию": "консервация",
    "консервацией": "консервация",
    "горошек": "горошек зелёный",
    "горошка": "горошек зелёный",
    "кукуруза": "кукуруза сладкая",
    "кукурузы": "кукуруза сладкая",
    "огурец": "огурцы маринованные",
    "огурцы": "огурцы маринованные",
    "огурцов": "огурцы маринованные",
    "огурцами": "огурцы маринованные",
}
_CATEGORY_PREFIX_ALIASES = {
    "фрукт": "фрукты",
    "овощ": "овощи",
    "бакале": "бакалея",
    "напит": "напитки",
    "макарон": "макароны",
}


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    category: str
    subcategory: str
    sku: str
    manufacturer: str
    packaging: str
    price: str
    availability: str
    updated_at: str
    is_competitor: bool = False
    for_sku: str | None = None


@dataclass(frozen=True, slots=True)
class UnitPriceQuote:
    record: CatalogRecord
    unit: str
    total_quantity: str
    unit_price: str


class _CatalogDir(Protocol):
    def iterdir(self): ...

    def __truediv__(self, other: str): ...

    def joinpath(self, *parts: str): ...


def _candidate_dirs() -> Iterator[Path | _CatalogDir]:
    env_dir = os.environ.get("STOKOZAVR_CATALOG_DIR")
    if env_dir:
        yield Path(env_dir)
    yield _REPO_CATALOG
    try:
        from importlib.resources import files

        yield files("stokozavr_bot") / "catalog"
    except (ModuleNotFoundError, FileNotFoundError, TypeError, ValueError, OSError):
        return


def _list_markdown(directory: Path | _CatalogDir) -> list[tuple[str, str]]:
    try:
        entries = list(directory.iterdir())
    except (OSError, FileNotFoundError, TypeError, ValueError, AttributeError):
        return []
    files: list[tuple[str, str]] = []
    for entry in entries:
        name = getattr(entry, "name", str(entry))
        if not name.endswith(".md"):
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError, TypeError, ValueError, AttributeError):
            continue
        files.append((name, text))
    return files


def _load_catalog() -> list[tuple[str, str]]:
    for directory in _candidate_dirs():
        files = _list_markdown(directory)
        if files:
            return files
    return []


def _heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _field(line: str, label: str) -> str | None:
    match = re.search(rf"(?:^|;)\s*{re.escape(label)}\s*:\s*([^;\n]+)", line, re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_catalog_records(text: str) -> list[CatalogRecord]:
    """Parse only complete, dated records; prose and partial rows are ignored."""
    category = _heading(text).lower()
    records: list[CatalogRecord] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("- ").strip()
        if "SKU:" not in line:
            continue
        values = {
            "subcategory": _field(line, "Подкатегория"),
            "sku": _field(line, "SKU"),
            "manufacturer": _field(line, "Производитель"),
            "packaging": _field(line, "Фасовка"),
            "price": _field(line, "Цена"),
            "availability": _field(line, "Статус наличия"),
            "updated_at": _field(line, "Дата обновления"),
        }
        if not all(values.values()) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", values["updated_at"] or ""
        ):
            continue
        try:
            date.fromisoformat(values["updated_at"] or "")
        except ValueError:
            continue
        record_type = (_field(line, "Тип") or "основной").lower()
        records.append(
            CatalogRecord(
                category=category,
                **values,
                is_competitor=record_type in {"конкурент", "альтернатива"},
                for_sku=_field(line, "Для SKU"),
            )
        )
    return records


def _all_records() -> list[CatalogRecord]:
    records: list[CatalogRecord] = []
    for _name, text in _load_catalog():
        records.extend(parse_catalog_records(text))
    return records


def _parse_packaging_quantity(packaging: str) -> tuple[float, str] | None:
    match = re.fullmatch(
        r"\s*(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл|шт|штук)\s*",
        packaging.lower(),
    )
    if not match:
        return None
    count, amount, unit = match.groups()
    count_value = float(count.replace(",", "."))
    amount_value = float(amount.replace(",", "."))
    factors = {"г": (0.001, "кг"), "кг": (1, "кг"), "мл": (0.001, "л"), "л": (1, "л")}
    if unit in factors:
        factor, normalized = factors[unit]
        return count_value * amount_value * factor, normalized
    return count_value * amount_value, "шт"


def _format_quantity(value: float, unit: str) -> str:
    rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{rendered} {unit}"


def _price_amount(price: str) -> float | None:
    match = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб)", price.lower())
    if not match:
        return None
    return float(match.group(1).replace(" ", "").replace(",", "."))


def _packaging_piece_count(packaging: str) -> float | None:
    match = re.fullmatch(
        r"\s*(\d+(?:[.,]\d+)?)\s*x\s*\d+(?:[.,]\d+)?\s*(?:кг|г|л|мл|шт|штук)\s*",
        packaging.lower(),
    )
    return float(match.group(1).replace(",", ".")) if match else None


def recover_product_from_history(history_text: str, category: str | None = None) -> str | None:
    """Recover one unambiguous primary product named in recent dialogue."""
    haystack = (history_text or "").lower()
    if not haystack:
        return None
    category_text = (category or "").lower()
    candidates: list[tuple[int, CatalogRecord]] = []
    for record in _all_records():
        if record.is_competitor:
            continue
        if (
            category_text
            and category_text not in record.category
            and record.category not in category_text
        ):
            continue
        terms = [
            term for term in re.findall(r"[\w-]+", record.subcategory.lower()) if len(term) >= 4
        ]
        score = sum(term in haystack for term in terms)
        if score:
            candidates.append((score, record))
    if not candidates:
        return None
    best_score = max(score for score, _record in candidates)
    best = [record for score, record in candidates if score == best_score]
    return best[0].subcategory if len(best) == 1 else None


def unit_price_quote(product: str, unit: str) -> UnitPriceQuote | None:
    """Calculate a unit price only from one confirmed primary catalog record."""
    requested = {
        "кг": "кг",
        "килограмм": "кг",
        "литр": "л",
        "л": "л",
        "штука": "шт",
        "шт": "шт",
    }.get(unit.strip().lower())
    if requested is None:
        return None
    terms = [token for token in re.findall(r"[\w-]+", (product or "").lower()) if len(token) >= 3]
    matches = [
        (
            sum(token in f"{record.category} {record.subcategory}".lower() for token in terms),
            record,
        )
        for record in _all_records()
        if not record.is_competitor
    ]
    matches = [(score, record) for score, record in matches if score]
    if not matches:
        return None
    best_score = max(score for score, _record in matches)
    candidates = [record for score, record in matches if score == best_score]
    if len(candidates) != 1:
        return None
    parsed = _parse_packaging_quantity(candidates[0].packaging)
    price = _price_amount(candidates[0].price)
    piece_count = _packaging_piece_count(candidates[0].packaging)
    if parsed is None or price is None or piece_count is None or piece_count <= 0:
        return None
    total, normalized = parsed
    if requested == "шт":
        total, normalized = piece_count, "шт"
    elif normalized != requested:
        return None
    amount = price / total
    rendered_price = f"{amount:.2f}" if amount % 1 else f"{amount:.0f}"
    return UnitPriceQuote(
        record=candidates[0],
        unit=normalized,
        total_quantity=_format_quantity(total, normalized),
        unit_price=rendered_price + f" ₽/{normalized}",
    )


def unit_price_catalog_result(product: str, unit: str) -> tuple[str, UnitPriceQuote] | None:
    quote = unit_price_quote(product, unit)
    if quote is None:
        return None
    record = quote.record
    raw = (
        f"Категория: {record.category}; Подкатегория: {record.subcategory}; SKU: {record.sku}; "
        f"Производитель: {record.manufacturer}; Фасовка: {record.packaging}; Цена: {record.price}; "
        f"Статус наличия: {record.availability}; Дата обновления: {record.updated_at}"
    )
    return (
        raw + f"; Подтверждённый расчёт: {quote.total_quantity} в упаковке; {quote.unit_price}",
        quote,
    )


def line_total_catalog_result(
    product: str,
    quantity: str,
    *,
    include_linked_competitor: bool = False,
) -> tuple[str, LineTotalQuote] | None:
    from stokozavr_bot.catalog_quotes import QuoteFailure, line_total_quote

    quote = line_total_quote(
        product,
        quantity,
        include_linked_competitor=include_linked_competitor,
    )
    if isinstance(quote, QuoteFailure):
        return None
    record = quote.record
    raw = (
        f"Категория: {record.category}; Подкатегория: {record.subcategory}; SKU: {record.sku}; "
        f"Производитель: {record.manufacturer}; Фасовка: {record.packaging}; Цена: {record.price}; "
        f"Статус наличия: {record.availability}; Дата обновления: {record.updated_at}"
    )
    allowed = " ".join(f"{amount} ₽" for amount in sorted(quote.allowed_amounts))
    return raw + f"; Подтверждённый расчёт: {quote.human_line}; Разрешённые суммы: {allowed}", quote


def composite_line_total_catalog_result(text: str) -> tuple[str, object] | None:
    from stokozavr_bot.catalog_quotes import quote_explicit_lines

    combined = quote_explicit_lines(text)
    if combined is None:
        return None
    parts: list[str] = []
    for quote in combined.lines:
        record = quote.record
        parts.append(
            f"Категория: {record.category}; Подкатегория: {record.subcategory}; SKU: {record.sku}; "
            f"Производитель: {record.manufacturer}; Фасовка: {record.packaging}; Цена: {record.price}; "
            f"Статус наличия: {record.availability}; Дата обновления: {record.updated_at}; "
            f"Подтверждённый расчёт: {quote.human_line}"
        )
    parts.append(
        "Подтверждённый расчёт: итого "
        f"{combined.total_amount} ₽; Разрешённые суммы: "
        + " ".join(f"{amount} ₽" for amount in sorted(combined.allowed_amounts))
    )
    return "\n".join(parts), combined


def _category_listing(files: list[tuple[str, str]]) -> str:
    headings = [_heading(text) or Path(name).stem for name, text in sorted(files)]
    lines = ["Доступные категории:"]
    lines.extend(f"- {heading}" for heading in headings)
    return "\n".join(lines)


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[\w-]+", query.lower(), flags=re.UNICODE):
        if len(token) < 3 or re.search(r"подешев|вариант|сравн|конкур", token):
            continue
        canonical = _QUERY_ALIASES.get(token)
        if canonical is None:
            canonical = next(
                (
                    value
                    for prefix, value in _CATEGORY_PREFIX_ALIASES.items()
                    if token.startswith(prefix)
                ),
                token,
            )
            candidates = (canonical,)
        else:
            # Keep the literal term too, so an existing exact product search
            # remains broad when a colloquial alias is ambiguous (e.g. огурцы).
            candidates = (token, canonical)
        for term in candidates:
            if term not in terms:
                terms.append(term)
    return terms


def search(query: str, *, include_competitors: bool = False) -> str:
    files = _load_catalog()
    if not files:
        return "Каталог пуст."
    cleaned = (query or "").strip().lower()
    if not cleaned:
        return _category_listing(files)
    records = _all_records()

    tokens = _query_terms(cleaned)

    def matches(record: CatalogRecord) -> bool:
        haystack = (
            f"{record.category} {record.subcategory} {record.sku} {record.manufacturer} "
            f"{record.packaging} {record.price} {record.availability} {record.updated_at}"
        ).lower()
        return any(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", haystack) for token in tokens)

    primary = [record for record in records if not record.is_competitor and matches(record)]
    competitors = [record for record in records if record.is_competitor and matches(record)]
    matched = primary
    if include_competitors and primary:
        matched = primary + competitors
    elif include_competitors and competitors:
        matched = competitors

    if not matched:
        return f"Подтверждённых позиций по запросу «{query.strip()}» нет.\n\n{_category_listing(files)}"
    return "\n".join(
        f"Категория: {r.category}; Подкатегория: {r.subcategory}; SKU: {r.sku}; Производитель: {r.manufacturer}; Фасовка: {r.packaging}; Цена: {r.price}; Статус наличия: {r.availability}; Дата обновления: {r.updated_at}"
        for r in matched[:10]
    )


def listed_price_amounts() -> set[str]:
    return {
        re.sub(r"\s+", "", match)
        for record in _all_records()
        for match in re.findall(r"(\d[\d\s]*)\s*(?:₽|руб)", record.price.lower())
    }


def listed_stock_amounts() -> set[str]:
    return set()


def catalog_has_stock_status() -> bool:
    return bool(_all_records())


def catalog_price_lines(query: str) -> list[str]:
    result = search(query)
    return [line for line in result.splitlines() if "Цена:" in line]


def generated_price_list() -> str:
    """Render the current primary catalog as a customer-safe price list.

    The catalog remains the single source of truth.  Competitors, dates and
    availability are deliberately omitted from this generated artifact.
    """
    records = sorted(
        (record for record in _all_records() if not record.is_competitor),
        key=lambda record: (record.category, record.subcategory, record.sku),
    )
    lines = [
        "# Актуальный прайс «Стокозавр»",
        "",
        "Товар | SKU | Производитель | Фасовка | Цена",
        "--- | --- | --- | --- | ---",
    ]
    lines.extend(
        f"Товар: {record.subcategory} | SKU: {record.sku} | Производитель: {record.manufacturer} | "
        f"Фасовка: {record.packaging} | Цена: {record.price}"
        for record in records
    )
    return "\n".join(lines)


def infer_catalog_interest(result: str, reply: str) -> str | None:
    """Return categories explicitly reflected by a grounded catalog answer."""
    if not _catalog_result_has_positions(result):
        return None
    categories: list[str] = []
    reply_lower = reply.lower()
    for raw_line in result.splitlines():
        if "SKU:" not in raw_line:
            continue
        category = _field(raw_line, "Категория")
        subcategory = _field(raw_line, "Подкатегория")
        mentioned = any(
            term
            and (
                term.lower() in reply_lower
                or any(
                    len(word) >= 4 and word.lower() in reply_lower
                    for word in re.findall(r"[а-яёa-z0-9-]+", term.lower())
                )
            )
            for term in (category, subcategory)
        )
        if mentioned and category and category not in categories:
            categories.append(category)
    return " и ".join(categories) if categories else None


def named_catalog_item(result: str, text: str) -> str | None:
    """Return one subcategory named in the utterance or reply; mixed hits stay ambiguous."""
    if not text or not _catalog_result_has_positions(result):
        return None
    lowered = text.lower()
    items: list[str] = []
    for raw_line in result.splitlines():
        if "SKU:" not in raw_line:
            continue
        subcategory = _field(raw_line, "Подкатегория")
        if not subcategory:
            continue
        tokens = [
            word for word in re.findall(r"[а-яёa-z0-9-]+", subcategory.lower()) if len(word) >= 3
        ]
        if (
            subcategory.lower() in lowered or any(token in lowered for token in tokens)
        ) and subcategory not in items:
            items.append(subcategory)
    return items[0] if len(items) == 1 else None


def catalog_categories_in_result(result: str) -> set[str]:
    categories: set[str] = set()
    for raw_line in result.splitlines():
        if "SKU:" not in raw_line:
            continue
        category = _field(raw_line, "Категория")
        if category:
            categories.add(category.lower())
    return categories


def _catalog_result_has_positions(result: str) -> bool:
    return any("SKU:" in line for line in result.splitlines())


def grounded_search_reply(
    result: str, name: str | None = None, previous_reply: str | None = None
) -> str | None:
    """Format confirmed primary catalog lines without exposing internal SKU/date fields."""
    lines = []
    for raw_line in result.splitlines():
        if "SKU:" not in raw_line:
            continue
        subcategory = _field(raw_line, "Подкатегория")
        manufacturer = _field(raw_line, "Производитель")
        packaging = _field(raw_line, "Фасовка")
        price = _field(raw_line, "Цена")
        availability = _field(raw_line, "Статус наличия")
        if not all((subcategory, manufacturer, packaging, price, availability)):
            continue
        lines.append(
            f"{subcategory}: {price} за {packaging}; производитель — {manufacturer}; "
            f"сейчас {availability}"
        )
    if not lines:
        return None
    prefix = f"{name}, " if name else ""
    endings = (
        "Что из этого вам подходит?",
        "Какой из них посмотреть?",
        "Что из списка рассмотреть?",
    )
    ending = next(
        (item for item in endings if not previous_reply or item not in previous_reply), endings[0]
    )
    return prefix + "В каталоге есть:\n- " + "\n- ".join(lines) + "\n" + ending


def grounded_quote_reply(
    product: str,
    name: str | None = None,
    volume: str | None = None,
    previous_reply: str | None = None,
) -> str | None:
    cleaned = (product or "").strip().lower()
    tokens = [token for token in re.split(r"\s+", cleaned) if len(token) >= 3]
    candidates = [
        record
        for record in _all_records()
        if not record.is_competitor
        and any(token in f"{record.category} {record.subcategory}".lower() for token in tokens)
    ]
    if len(candidates) != 1:
        return None
    record = candidates[0]
    prefix = f"{name}, " if name else ""
    endings = ("Самовывоз или доставка?", "Когда удобно забрать или нужна доставка?")
    ending = next(
        (item for item in endings if not previous_reply or item not in previous_reply), endings[0]
    )
    line = (
        f"{record.subcategory}: {record.price} за {record.packaging}; "
        f"производитель — {record.manufacturer}; сейчас {record.availability}."
    )
    return f"{prefix}{line} {ending}"
