from __future__ import annotations

import re

# Customer wording is normalized here, before matching catalog records.
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
    "огурец": "огурцы",
    "огурцов": "огурцы",
    "огурцами": "огурцы",
}
_CATEGORY_PREFIX_ALIASES = {
    "фрукт": "фрукты",
    "овощ": "овощи",
    "бакале": "бакалея",
    "напит": "напитки",
    "макарон": "макароны",
}
_NOISE = re.compile(r"подешев|вариант|сравн|конкур")
_TOKEN_RE = re.compile(r"[\w-]+", flags=re.UNICODE)
_ENDINGS = (
    "ями",
    "ами",
    "ого",
    "ему",
    "ими",
    "ыми",
    "ой",
    "ей",
    "ий",
    "ый",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ов",
    "ев",
    "ах",
    "ях",
    "ом",
    "ем",
    "ы",
    "и",
    "а",
    "я",
    "у",
    "ю",
    "е",
    "о",
)


def normalize_catalog_token(token: str) -> str:
    return (token or "").lower().replace("ё", "е")


def stem_catalog_token(token: str) -> str:
    """Light Russian stem for catalog tokens. Keeps short product stems like рис."""
    cleaned = normalize_catalog_token(token).replace("ь", "").replace("ъ", "")
    if len(cleaned) <= 3:
        return cleaned
    for ending in _ENDINGS:
        leftover = len(cleaned) - len(ending)
        if cleaned.endswith(ending) and leftover >= 3:
            return cleaned[:leftover]
    return cleaned


def catalog_word_tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(normalize_catalog_token(text)) if token]


def expand_query_terms(query: str) -> list[str]:
    """Alias-expand and keep literal tokens so search/quote share one vocabulary."""
    terms: list[str] = []
    for token in catalog_word_tokens(query):
        if len(token) < 3 or _NOISE.search(token):
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
            candidates = (token, canonical)
        for term in candidates:
            if term not in terms:
                terms.append(term)
    return terms


def _has_word(needle: str, hay: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay))


def term_matches_haystack(term: str, haystack: str) -> bool:
    """True when a query term hits catalog text by phrase, token, or inflection stem."""
    needle = normalize_catalog_token(term)
    hay = normalize_catalog_token(haystack)
    if not needle or not hay:
        return False
    if " " in needle:
        return all(term_matches_haystack(part, hay) for part in needle.split() if len(part) >= 3)
    if _has_word(needle, hay):
        return True
    stem = stem_catalog_token(needle)
    if stem == needle or len(stem) < 3:
        return False
    if _has_word(stem, hay):
        return True
    if len(stem) < 4:
        return False
    return any(stem_catalog_token(token) == stem for token in catalog_word_tokens(hay))


_FRESH_PREFIXES = ("свеж",)
_PRESERVED_PREFIXES = ("маринован", "солен", "квашен", "консервир")
_FRESH_CATEGORIES = frozenset({"овощи", "фрукты"})


def _token_process(token: str) -> str | None:
    raw = normalize_catalog_token(token)
    stem = stem_catalog_token(token)
    if any(raw.startswith(prefix) or stem.startswith(prefix) for prefix in _PRESERVED_PREFIXES):
        return "preserved"
    if any(raw.startswith(prefix) or stem.startswith(prefix) for prefix in _FRESH_PREFIXES):
        return "fresh"
    return None


def process_polarity(text: str) -> str | None:
    seen = {flag for token in catalog_word_tokens(text) if (flag := _token_process(token))}
    if seen == {"preserved"}:
        return "preserved"
    if seen == {"fresh"}:
        return "fresh"
    return None


def is_process_modifier(token: str) -> bool:
    return _token_process(token) is not None


def catalog_record_score(
    query: str,
    *,
    category: str,
    subcategory: str,
    sku: str = "",
    manufacturer: str = "",
) -> int:
    """Score a catalog row against a query using stems plus process modifiers."""
    terms = expand_query_terms(query)
    product_hay = f"{category} {subcategory} {sku}"
    score = 0
    for term in terms:
        if term_matches_haystack(term, product_hay):
            score += 2
        elif not is_process_modifier(term) and term_matches_haystack(term, manufacturer):
            score += 1
    wanted = process_polarity(query)
    present = process_polarity(f"{category} {subcategory}")
    if wanted == "fresh":
        if present == "preserved":
            score -= 5
        elif present == "fresh" or normalize_catalog_token(category) in _FRESH_CATEGORIES:
            score += 3
    elif wanted == "preserved":
        if present == "preserved":
            score += 3
        elif present == "fresh" or normalize_catalog_token(category) in _FRESH_CATEGORIES:
            score -= 5
    return score


def best_catalog_scores(scored: list[tuple[int, object]]) -> list[object]:
    positive = [(score, item) for score, item in scored if score > 0]
    if not positive:
        return []
    best = max(score for score, _item in positive)
    return [item for score, item in positive if score == best]
