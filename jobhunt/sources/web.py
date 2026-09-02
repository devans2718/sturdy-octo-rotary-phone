"""Sources for the long tail: RSS feeds and arbitrary company careers pages.

`GenericCareersPageSource` is the escape hatch for the "every company site is a
little different" problem. It tries three strategies in order of reliability:

1. schema.org JobPosting JSON-LD  (deterministic, exact)
2. link + heading heuristics       (deterministic, approximate)
3. the LLM agent reading the text  (flexible, costs a call)
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Posting
from ..normalize import detect_remote, html_to_text, parse_date, parse_salary
from .base import JobSource, SourceSpec, matches_query, polite_get


class RSSSource(JobSource):
    spec = SourceSpec(
        kind="rss",
        label="RSS / Atom feed",
        help="Any job feed URL. Many boards (WeWorkRemotely, Stack Overflow-style feeds) expose one.",
        fields=[
            {"name": "url", "label": "Feed URL", "required": True},
            {"name": "company", "label": "Default company name", "required": False},
        ],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        xml = polite_get(self.config["url"]).text
        soup = BeautifulSoup(xml, "xml")
        entries = soup.find_all("item") or soup.find_all("entry")
        for entry in entries[: limit * 3]:
            title = _text(entry, "title")
            link = _text(entry, "link") or (entry.find("link") or {}).get("href", "")
            body = html_to_text(_text(entry, "description") or _text(entry, "content") or _text(entry, "summary"))
            company, clean_title = _split_company(title, self.config.get("company", ""))
            lo, hi, currency = parse_salary(body)
            posting = self._posting(
                external_id=_text(entry, "guid") or link,
                title=clean_title,
                company=company,
                location=_text(entry, "location"),
                remote=detect_remote(title, body[:1200]),
                url=link,
                description=body,
                posted_at=parse_date(_text(entry, "pubDate") or _text(entry, "published") or _text(entry, "updated")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
            )
            if matches_query(posting, query):
                yield posting


def _text(node: Any, name: str) -> str:
    found = node.find(name)
    return found.get_text(strip=True) if found and found.get_text else ""


def _split_company(title: str, default: str) -> tuple[str, str]:
    """Feeds often pack "Company: Role" or "Role at Company" into the title."""
    for pattern in (r"^(?P<company>[^:]{2,60}):\s*(?P<title>.+)$", r"^(?P<title>.+?)\s+at\s+(?P<company>.+)$"):
        match = re.match(pattern, title)
        if match:
            return match.group("company").strip(), match.group("title").strip()
    return default, title


class GenericCareersPageSource(JobSource):
    spec = SourceSpec(
        kind="careers_page",
        label="Company careers page",
        help=(
            "Point at any careers URL. Reads schema.org JobPosting data when present, "
            "falls back to link heuristics, then to the AI agent for messy pages."
        ),
        fields=[
            {"name": "url", "label": "Careers page URL", "required": True},
            {"name": "company", "label": "Company name", "required": False},
            {
                "name": "follow_links",
                "label": "Open each job link for full detail (slower)",
                "type": "bool",
                "required": False,
            },
        ],
        needs_llm=True,
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        url = self.config["url"]
        company = self.config.get("company") or _company_from_url(url)
        html = polite_get(url, timeout=45).text
        soup = BeautifulSoup(html, "lxml")

        postings = list(self._from_jsonld(soup, url, company))
        if not postings:
            postings = list(self._from_links(soup, url, company, limit))
        if not postings and self.llm is not None:
            postings = list(self._from_llm(soup, url, company, limit))

        if self.config.get("follow_links") and postings:
            for posting in postings[:limit]:
                self._enrich(posting)

        for posting in postings[:limit]:
            if matches_query(posting, query):
                yield posting

    # ------------------------------------------------------------ strategy 1
    def _from_jsonld(self, soup: BeautifulSoup, base: str, company: str) -> Iterable[Posting]:
        """schema.org/JobPosting is the closest thing to a standard that exists."""
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "{}")
            except (ValueError, TypeError):
                continue
            for node in _iter_jobpostings(data):
                description = html_to_text(node.get("description", ""))
                location = _jsonld_location(node)
                lo, hi, currency = _jsonld_salary(node)
                if lo is None:
                    lo, hi, currency = parse_salary(description)
                org = node.get("hiringOrganization") or {}
                yield self._posting(
                    external_id=str(node.get("identifier", "") or node.get("url", "")),
                    title=node.get("title", ""),
                    company=(org.get("name") if isinstance(org, dict) else "") or company,
                    location=location,
                    remote="remote" if node.get("jobLocationType") == "TELECOMMUTE" else detect_remote(location, description[:1200]),
                    employment_type=_as_str(node.get("employmentType")),
                    url=urljoin(base, node.get("url", "") or base),
                    description=description,
                    posted_at=parse_date(node.get("datePosted")),
                    salary_min=lo,
                    salary_max=hi,
                    salary_currency=currency,
                    raw={"strategy": "jsonld"},
                )

    # ------------------------------------------------------------ strategy 2
    def _from_links(self, soup: BeautifulSoup, base: str, company: str, limit: int) -> Iterable[Posting]:
        """Heuristic: anchors whose href looks like a job detail page."""
        seen: set[str] = set()
        pattern = re.compile(r"(?i)/(job|jobs|career|careers|position|opening|vacanc|role)s?[/\-]")
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base, anchor["href"].split("#")[0])
            title = anchor.get_text(" ", strip=True)
            if href in seen or not pattern.search(href) or not (3 < len(title) < 120):
                continue
            if urlparse(href).netloc != urlparse(base).netloc:
                continue
            seen.add(href)
            yield self._posting(
                external_id=href,
                title=title,
                company=company,
                location=_nearby_location(anchor),
                url=href,
                description="",
                raw={"strategy": "links"},
            )
            if len(seen) >= limit:
                return

    # ------------------------------------------------------------ strategy 3
    def _from_llm(self, soup: BeautifulSoup, base: str, company: str, limit: int) -> Iterable[Posting]:
        from ..agent import extract_postings_from_text

        text = html_to_text(str(soup))[:20000]
        for record in extract_postings_from_text(self.llm, text, base, company)[:limit]:
            lo, hi, currency = parse_salary(record.get("salary_text", ""))
            yield self._posting(
                external_id=record.get("url", "") or record.get("title", ""),
                title=record.get("title", ""),
                company=record.get("company") or company,
                location=record.get("location", ""),
                remote=record.get("remote", "") or detect_remote(record.get("location", "")),
                employment_type=record.get("employment_type", ""),
                url=urljoin(base, record.get("url", "") or base),
                description=record.get("description", ""),
                posted_at=parse_date(record.get("posted_at", "")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                raw={"strategy": "llm"},
            )

    def _enrich(self, posting: Posting) -> None:
        """Second pass: pull the full description off the detail page."""
        if posting.description or not posting.url:
            return
        try:
            detail = polite_get(posting.url, timeout=30).text
        except Exception:  # a dead link should not abort the whole harvest
            return
        detail_soup = BeautifulSoup(detail, "lxml")
        for jsonld in self._from_jsonld(detail_soup, posting.url, posting.company):
            posting.description = jsonld.description or posting.description
            posting.location = posting.location or jsonld.location
            posting.posted_at = posting.posted_at or jsonld.posted_at
            posting.salary_min = posting.salary_min or jsonld.salary_min
            posting.salary_max = posting.salary_max or jsonld.salary_max
            return
        body = detail_soup.find("main") or detail_soup.find("article") or detail_soup.body
        posting.description = html_to_text(str(body))[:20000] if body else ""
        lo, hi, currency = parse_salary(posting.description)
        posting.salary_min, posting.salary_max, posting.salary_currency = lo, hi, currency
        posting.remote = posting.remote or detect_remote(posting.location, posting.description[:1500])


def _iter_jobpostings(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, list):
        for child in node:
            yield from _iter_jobpostings(child)
    elif isinstance(node, dict):
        if node.get("@type") == "JobPosting" or "JobPosting" in _as_str(node.get("@type")):
            yield node
        for key in ("@graph", "itemListElement", "item"):
            if key in node:
                yield from _iter_jobpostings(node[key])


def _as_str(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value) if value else ""


def _jsonld_location(node: dict[str, Any]) -> str:
    location = node.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else {}
    address = (location or {}).get("address", {}) if isinstance(location, dict) else {}
    if isinstance(address, str):
        return address
    parts = [address.get("addressLocality", ""), address.get("addressRegion", ""), address.get("addressCountry", "")]
    parts = [p if isinstance(p, str) else _as_str(p.get("name") if isinstance(p, dict) else p) for p in parts]
    return ", ".join(p for p in parts if p)


def _jsonld_salary(node: dict[str, Any]) -> tuple[float | None, float | None, str]:
    salary = node.get("baseSalary") or {}
    if not isinstance(salary, dict):
        return None, None, ""
    value = salary.get("value") or {}
    if not isinstance(value, dict):
        return None, None, ""
    lo, hi = value.get("minValue"), value.get("maxValue")
    if lo is None and hi is None:
        lo = hi = value.get("value")
    unit = (value.get("unitText") or "").upper()
    factor = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}.get(unit, 1)
    try:
        lo = float(lo) * factor if lo is not None else None
        hi = float(hi) * factor if hi is not None else lo
    except (TypeError, ValueError):
        return None, None, ""
    return lo, hi, salary.get("currency", "")


def _nearby_location(anchor: Any) -> str:
    """Careers tables usually put the location in a sibling cell."""
    parent = anchor.find_parent(["tr", "li", "div"])
    if not parent:
        return ""
    text = parent.get_text(" · ", strip=True)
    match = re.search(r"(?i)((?:remote|hybrid|on-?site)[^·]*|[A-Z][a-z]+,\s*[A-Z]{2,})", text)
    return match.group(1).strip()[:80] if match else ""


def _company_from_url(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "").replace("careers.", "").replace("jobs.", "")
    return host.split(".")[0].replace("-", " ").title()
