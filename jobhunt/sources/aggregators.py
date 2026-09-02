"""Aggregators: broad reach, lower signal, wildly inconsistent schemas."""

from __future__ import annotations

from typing import Iterable
from urllib.parse import quote_plus

from ..models import Posting
from ..normalize import detect_remote, html_to_text, parse_date, parse_salary
from .base import JobSource, SourceSpec, matches_query, polite_get


class RemotiveSource(JobSource):
    spec = SourceSpec(
        kind="remotive",
        label="Remotive",
        help="Free remote-jobs API. Optional category filter (e.g. `software-dev`).",
        fields=[{"name": "category", "label": "Category slug", "help": "Optional", "required": False}],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        url = "https://remotive.com/api/remote-jobs?limit=" + str(min(limit * 2, 200))
        if query:
            url += "&search=" + quote_plus(query)
        if self.config.get("category"):
            url += "&category=" + quote_plus(self.config["category"])
        for job in polite_get(url).json().get("jobs", [])[: limit * 2]:
            description = html_to_text(job.get("description", ""))
            lo, hi, currency = parse_salary(job.get("salary") or description)
            yield self._posting(
                external_id=str(job.get("id", "")),
                title=job.get("title", ""),
                company=job.get("company_name", ""),
                location=job.get("candidate_required_location", ""),
                remote="remote",
                employment_type=job.get("job_type", ""),
                url=job.get("url", ""),
                description=description,
                posted_at=parse_date(job.get("publication_date")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                raw={"category": job.get("category", ""), "tags": job.get("tags", [])},
            )


class RemoteOKSource(JobSource):
    spec = SourceSpec(
        kind="remoteok",
        label="RemoteOK",
        help="Free remote-jobs feed. No server-side search; filtered locally.",
        fields=[],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        payload = polite_get("https://remoteok.com/api").json()
        # The first element is a legal notice, not a job.
        jobs = [j for j in payload if isinstance(j, dict) and j.get("position")]
        for job in jobs[: limit * 3]:
            description = html_to_text(job.get("description", ""))
            lo, hi = job.get("salary_min"), job.get("salary_max")
            currency = "USD" if lo else ""
            if not lo:
                lo, hi, currency = parse_salary(description)
            posting = self._posting(
                external_id=str(job.get("id", "")),
                title=job.get("position", ""),
                company=job.get("company", ""),
                location=job.get("location", "") or "Remote",
                remote="remote",
                url=job.get("url", ""),
                description=description,
                posted_at=parse_date(job.get("date")),
                salary_min=float(lo) if lo else None,
                salary_max=float(hi) if hi else None,
                salary_currency=currency,
                raw={"tags": job.get("tags", [])},
            )
            if matches_query(posting, query):
                yield posting


class ArbeitnowSource(JobSource):
    spec = SourceSpec(
        kind="arbeitnow",
        label="Arbeitnow",
        help="Free EU-heavy job board API (no key required).",
        fields=[{"name": "pages", "label": "Pages to fetch", "help": "Default 2", "required": False}],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        pages = int(self.config.get("pages") or 2)
        seen = 0
        for page in range(1, pages + 1):
            payload = polite_get(f"https://www.arbeitnow.com/api/job-board-api?page={page}").json()
            for job in payload.get("data", []):
                description = html_to_text(job.get("description", ""))
                lo, hi, currency = parse_salary(description)
                posting = self._posting(
                    external_id=job.get("slug", ""),
                    title=job.get("title", ""),
                    company=job.get("company_name", ""),
                    location=job.get("location", ""),
                    remote="remote" if job.get("remote") else detect_remote(job.get("location", "")),
                    employment_type=", ".join(job.get("job_types", []) or []),
                    url=job.get("url", ""),
                    description=description,
                    posted_at=parse_date(job.get("created_at")),
                    salary_min=lo,
                    salary_max=hi,
                    salary_currency=currency,
                    raw={"tags": job.get("tags", [])},
                )
                if matches_query(posting, query):
                    seen += 1
                    yield posting
                    if seen >= limit * 2:
                        return


class AdzunaSource(JobSource):
    """Example of a keyed aggregator — free tier at developer.adzuna.com."""

    spec = SourceSpec(
        kind="adzuna",
        label="Adzuna",
        help="Requires a free app_id / app_key from developer.adzuna.com.",
        fields=[
            {"name": "app_id", "label": "App ID", "required": True},
            {"name": "app_key", "label": "App key", "required": True, "secret": True},
            {"name": "country", "label": "Country code", "help": "gb, us, de, …", "required": True},
            {"name": "where", "label": "Location filter", "required": False},
        ],
    )

    def fetch(self, query: str = "", limit: int = 100) -> Iterable[Posting]:
        country = (self.config.get("country") or "gb").lower()
        params = (
            f"app_id={quote_plus(self.config['app_id'])}"
            f"&app_key={quote_plus(self.config['app_key'])}"
            f"&results_per_page={min(limit, 50)}"
            f"&content-type=application/json"
        )
        if query:
            params += "&what=" + quote_plus(query)
        if self.config.get("where"):
            params += "&where=" + quote_plus(self.config["where"])
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{params}"
        for job in polite_get(url).json().get("results", []):
            description = html_to_text(job.get("description", ""))
            location = (job.get("location") or {}).get("display_name", "")
            yield self._posting(
                external_id=str(job.get("id", "")),
                title=job.get("title", ""),
                company=(job.get("company") or {}).get("display_name", ""),
                location=location,
                remote=detect_remote(location, description[:1000]),
                employment_type=job.get("contract_time", ""),
                url=job.get("redirect_url", ""),
                description=description,
                posted_at=parse_date(job.get("created")),
                salary_min=job.get("salary_min"),
                salary_max=job.get("salary_max"),
                salary_currency="GBP" if country == "gb" else "",
                raw={"category": (job.get("category") or {}).get("label", "")},
            )
