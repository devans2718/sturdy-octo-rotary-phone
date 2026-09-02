"""Applicant-tracking-system boards: the company's own listings.

These are the highest-signal sources — they are first-party, complete, and have
public JSON APIs. Each vendor shapes its payload differently, which is exactly
why adapters exist.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..models import Posting
from ..normalize import detect_remote, html_to_text, parse_date, parse_salary
from .base import JobSource, SourceSpec, matches_query, polite_get

_TOKEN_FIELD = {
    "name": "token",
    "label": "Board token",
    "help": "The company slug in the board URL.",
    "required": True,
}
_COMPANY_FIELD = {
    "name": "company",
    "label": "Company name (display)",
    "help": "Optional; falls back to the token.",
    "required": False,
}


class GreenhouseSource(JobSource):
    spec = SourceSpec(
        kind="greenhouse",
        label="Greenhouse",
        help="boards.greenhouse.io/<token> — e.g. token `stripe` for boards.greenhouse.io/stripe",
        fields=[_TOKEN_FIELD, _COMPANY_FIELD],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        token = self.config["token"].strip().strip("/")
        company = self.config.get("company") or token
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        jobs = polite_get(url).json().get("jobs", [])
        for job in jobs[: limit * 3]:
            description = html_to_text(job.get("content", ""))
            location = (job.get("location") or {}).get("name", "")
            lo, hi, currency = parse_salary(description)
            posting = self._posting(
                external_id=str(job.get("id", "")),
                title=job.get("title", ""),
                company=company,
                location=location,
                remote=detect_remote(location, job.get("title", ""), description[:1500]),
                url=job.get("absolute_url", ""),
                description=description,
                posted_at=parse_date(job.get("updated_at") or job.get("first_published")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                raw={"departments": [d.get("name") for d in job.get("departments", [])]},
            )
            if matches_query(posting, query):
                yield posting


class LeverSource(JobSource):
    spec = SourceSpec(
        kind="lever",
        label="Lever",
        help="jobs.lever.co/<token> — e.g. token `netflix`",
        fields=[_TOKEN_FIELD, _COMPANY_FIELD],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        token = self.config["token"].strip().strip("/")
        company = self.config.get("company") or token
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        for job in polite_get(url).json()[: limit * 3]:
            categories = job.get("categories") or {}
            description = html_to_text(job.get("descriptionPlain") or job.get("description", ""))
            for section in job.get("lists", []):
                description += "\n\n" + section.get("text", "") + "\n" + html_to_text(section.get("content", ""))
            location = categories.get("location", "")
            salary_range = job.get("salaryRange") or {}
            lo, hi = salary_range.get("min"), salary_range.get("max")
            currency = salary_range.get("currency", "")
            if lo is None and hi is None:
                lo, hi, currency = parse_salary(description)
            posting = self._posting(
                external_id=job.get("id", ""),
                title=job.get("text", ""),
                company=company,
                location=location,
                remote=detect_remote(location, categories.get("commitment", ""), description[:1500]),
                employment_type=categories.get("commitment", ""),
                url=job.get("hostedUrl", ""),
                description=description.strip(),
                posted_at=parse_date(job.get("createdAt")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                raw={"team": categories.get("team", "")},
            )
            if matches_query(posting, query):
                yield posting


class AshbySource(JobSource):
    spec = SourceSpec(
        kind="ashby",
        label="Ashby",
        help="jobs.ashbyhq.com/<token> — e.g. token `openai`",
        fields=[_TOKEN_FIELD, _COMPANY_FIELD],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        token = self.config["token"].strip().strip("/")
        company = self.config.get("company") or token
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        payload = polite_get(url).json()
        for job in payload.get("jobs", [])[: limit * 3]:
            description = html_to_text(job.get("descriptionHtml") or job.get("descriptionPlain", ""))
            comp = job.get("compensation") or {}
            lo, hi, currency = _ashby_comp(comp)
            if lo is None:
                lo, hi, currency = parse_salary(description)
            location = job.get("location", "")
            posting = self._posting(
                external_id=job.get("id", ""),
                title=job.get("title", ""),
                company=company,
                location=location,
                remote="remote" if job.get("isRemote") else detect_remote(location, description[:1500]),
                employment_type=job.get("employmentType", ""),
                url=job.get("jobUrl", ""),
                description=description,
                posted_at=parse_date(job.get("publishedAt")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                raw={"team": job.get("team", ""), "department": job.get("department", "")},
            )
            if matches_query(posting, query):
                yield posting


def _ashby_comp(comp: dict[str, Any]) -> tuple[float | None, float | None, str]:
    """Ashby exposes structured compensation; prefer it over regex."""
    for tier in comp.get("compensationTiers", []) or []:
        for component in tier.get("components", []) or []:
            if component.get("compensationType") != "Salary":
                continue
            lo, hi = component.get("minValue"), component.get("maxValue")
            if lo or hi:
                return (
                    float(lo) if lo else None,
                    float(hi) if hi else float(lo),
                    component.get("currencyCode", ""),
                )
    return None, None, ""


class WorkableSource(JobSource):
    spec = SourceSpec(
        kind="workable",
        label="Workable",
        help="apply.workable.com/<token> — e.g. token `bolt`",
        fields=[_TOKEN_FIELD, _COMPANY_FIELD],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        token = self.config["token"].strip().strip("/")
        company = self.config.get("company") or token
        url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
        payload = polite_get(url).json()
        for job in payload.get("jobs", [])[: limit * 3]:
            description = html_to_text(job.get("description", "") + " " + job.get("requirements", ""))
            location = ", ".join(x for x in [job.get("city", ""), job.get("country", "")] if x)
            lo, hi, currency = parse_salary(description)
            posting = self._posting(
                external_id=job.get("shortcode", ""),
                title=job.get("title", ""),
                company=company,
                location=location,
                remote="remote" if job.get("telecommuting") else detect_remote(location, description[:1500]),
                employment_type=job.get("employment_type", ""),
                url=job.get("url") or job.get("application_url", ""),
                description=description,
                posted_at=parse_date(job.get("published_on")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                raw={"department": job.get("department", "")},
            )
            if matches_query(posting, query):
                yield posting
