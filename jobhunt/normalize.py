"""Turning messy board-specific text into comparable fields.

Every source adapter emits raw-ish strings; this module is the single place
where salary, seniority, remote-ness and HTML get canonicalized, so adding a
new board never means re-implementing salary parsing.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as date_parser

SENIORITY_PATTERNS: list[tuple[str, str]] = [
    ("intern", r"\b(intern|internship|placement|co-?op)\b"),
    ("junior", r"\b(junior|jr\.?|entry[- ]level|graduate|new ?grad|associate)\b"),
    ("mid", r"\b(mid[- ]level|intermediate|ii\b|2\b)\b"),
    ("senior", r"\b(senior|sr\.?|iii\b|lead engineer)\b"),
    ("staff", r"\b(staff|principal|architect|distinguished)\b"),
    ("lead", r"\b(lead|manager|head of|director|vp|chief)\b"),
]

_CURRENCIES = {"$": "USD", "£": "GBP", "€": "EUR", "usd": "USD", "gbp": "GBP", "eur": "EUR", "cad": "CAD", "aud": "AUD"}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(raw: str | None) -> str:
    """Strip markup without pulling in a parser for every tiny field."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def detect_remote(*fields: str) -> str:
    """-> 'remote' | 'hybrid' | 'onsite' | ''."""
    blob = " ".join(f for f in fields if f).lower()
    if not blob:
        return ""
    if re.search(r"\bhybrid\b", blob):
        return "hybrid"
    if re.search(r"\b(fully[- ]remote|remote|work from home|wfh|distributed|anywhere)\b", blob):
        # "no remote" / "not remote" should not count as remote.
        if re.search(r"\b(no|not|non)[- ]remote\b", blob):
            return "onsite"
        return "remote"
    if re.search(r"\b(on[- ]?site|in[- ]office|in[- ]person)\b", blob):
        return "onsite"
    return ""


def detect_seniority(*fields: str) -> str:
    blob = " ".join(f for f in fields if f).lower()
    # Reverse order: "senior staff engineer" should read as staff, not senior.
    for label, pattern in reversed(SENIORITY_PATTERNS):
        if re.search(pattern, blob):
            return label
    return ""


def _to_number(token: str) -> float | None:
    token = token.replace(",", "").strip().lower()
    multiplier = 1.0
    if token.endswith("k"):
        multiplier, token = 1000.0, token[:-1]
    try:
        value = float(token)
    except ValueError:
        return None
    return value * multiplier


def parse_salary(text: str | None) -> tuple[float | None, float | None, str]:
    """Pull a (min, max, currency) range out of free text.

    Handles "$120,000 - $150,000", "£70k–£90k", "up to 100000 EUR", and hourly
    rates (annualized at 2,080 h). Returns (None, None, "") when unsure — a
    wrong number is worse than no number for scoring.
    """
    if not text:
        return None, None, ""
    blob = text.replace("—", "-").replace("–", "-")
    window = blob
    # Prefer a compensation-looking neighbourhood if one exists.
    hit = re.search(r"(?i)(salary|compensation|pay range|base pay|remuneration)", blob)
    if hit:
        window = blob[max(0, hit.start() - 100) : hit.start() + 400]

    currency = ""
    for symbol, code in _CURRENCIES.items():
        if symbol in window.lower():
            currency = code
            break

    money = r"(?:(?P<sym>[$£€])\s*)?(?P<num>\d[\d,]*(?:\.\d+)?\s*[kK]?)"
    pair = re.search(rf"{money}\s*(?:-|to)\s*{money.replace('sym', 'sym2').replace('num', 'num2')}", window)
    if pair:
        lo, hi = _to_number(pair.group("num")), _to_number(pair.group("num2"))
        symbol = bool(pair.group("sym") or pair.group("sym2"))
    else:
        single = re.search(rf"(?i)(?:up to|from|starting at)?\s*{money}", window)
        if not single:
            return None, None, currency
        lo = hi = _to_number(single.group("num"))
        symbol = bool(single.group("sym"))

    # A bare number with no currency marker and no salary heading is almost
    # always something else (team size, founding year, a version number).
    if not symbol and not hit and not re.search(r"(?i)\b(usd|gbp|eur|cad|aud)\b", window):
        return None, None, currency

    values = [v for v in (lo, hi) if v is not None]
    if not values:
        return None, None, currency
    if re.search(r"(?i)\b(per hour|/ ?hour|hourly|/ ?hr|per hr)\b", window) and max(values) < 500:
        lo = lo * 2080 if lo else None
        hi = hi * 2080 if hi else None
    elif max(values) < 10000:  # too small to be an annual salary
        return None, None, currency
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    return lo, hi, currency


def parse_date(value: Any) -> str:
    """Normalize whatever a board calls a date into an ISO string."""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    try:
        parsed = date_parser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def days_since(iso: str) -> float | None:
    if not iso:
        return None
    try:
        when = date_parser.parse(iso)
    except (ValueError, OverflowError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


_STOPWORDS = set(
    """a an the and or of to in for with on at by from as is are be been we you our your their they it its
    will would can could should must may might have has had do does did not no this that these those than then
    role job position team company work working experience years year including etc via per about into over under
    across more most other others any all such new use used using help helps helping strong good great
    ability able across within without""".split()
)

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.\-/]{1,}")


def tokenize(text: str) -> list[str]:
    """Lowercased content words; keeps c++, .net, ci/cd intact."""
    return [t for t in (m.group(0).lower().strip(".-/") for m in _TOKEN_RE.finditer(text or "")) if t and t not in _STOPWORDS and len(t) > 1]
