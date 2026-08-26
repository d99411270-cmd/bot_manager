from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

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


def _category_listing(files: list[tuple[str, str]]) -> str:
    headings = [_heading(text) or Path(name).stem for name, text in sorted(files)]
    lines = ["Доступные категории:"]
    lines.extend(f"- {heading}" for heading in headings)
    return "\n".join(lines)


def search(query: str) -> str:
    files = _load_catalog()
    if not files:
        return "Каталог пуст."
    cleaned = (query or "").strip().lower()
    if not cleaned:
        return _category_listing(files)
    records = _all_records()

    tokens = [
        token
        for token in re.split(r"\s+", cleaned)
        if len(token) >= 3 and not re.search(r"подешев|вариант|сравн|конкур", token)
    ]

    def matches(record: CatalogRecord) -> bool:
        haystack = (
            f"{record.category} {record.subcategory} {record.sku} {record.manufacturer} "
            f"{record.packaging} {record.price} {record.availability} {record.updated_at}"
        ).lower()
        return any(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", haystack) for token in tokens)

    primary = [record for record in records if not record.is_competitor and matches(record)]
    matched = primary

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
