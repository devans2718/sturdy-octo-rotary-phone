"""End-to-end smoke test of everything that doesn't need the network or a model.

    python smoke_test.py

Uses a throwaway database, the offline LLM provider, and a fake source, so it
runs anywhere.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from jobhunt.db import Database
from jobhunt.llm.base import extract_json
from jobhunt.llm.offline import OfflineProvider
from jobhunt.models import Posting, Profile, ProfileItem
from jobhunt.normalize import detect_remote, detect_seniority, html_to_text, parse_salary
from jobhunt.scoring import harvest, rescore
from jobhunt.scoring.deterministic import Corpus, hard_filter, score_posting
from jobhunt.sources import SOURCES
from jobhunt.sources.base import JobSource, SourceSpec, matches_query

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# ------------------------------------------------------------------ normalize
check("html_to_text strips markup", html_to_text("<p>Hello <b>world</b></p><script>x</script>") == "Hello world")
check("salary range", parse_salary("Salary: $120,000 - $150,000 per year")[:2] == (120000.0, 150000.0))
check("salary k-suffix", parse_salary("Compensation £70k–£90k")[:2] == (70000.0, 90000.0))
check("salary currency", parse_salary("Salary: £70,000 to £90,000")[2] == "GBP")
check("hourly annualised", parse_salary("Pay range: $50 - $60 per hour")[1] == 60 * 2080)
check("no salary", parse_salary("We are a team of 25 people") == (None, None, ""))
check("remote detection", detect_remote("Remote - Europe") == "remote")
check("hybrid beats remote", detect_remote("Hybrid remote, London") == "hybrid")
check("negated remote", detect_remote("This role is not remote") == "onsite")
check("seniority staff over senior", detect_seniority("Senior Staff Engineer") == "staff")
check("seniority junior", detect_seniority("Junior Data Analyst") == "junior")

# ---------------------------------------------------------------- json repair
check("json passthrough", extract_json('{"a": 1}') == {"a": 1})
check("json in fences", extract_json('Sure!\n```json\n{"a": 2}\n```') == {"a": 2})
check("json embedded", extract_json('Here you go: {"a": {"b": 3}} hope that helps') == {"a": {"b": 3}})
check("json with braces in strings", extract_json('text {"a": "}{"} end') == {"a": "}{"})

# --------------------------------------------------------------------- offline
offline = OfflineProvider()
stub = offline.complete_json("anything", json_schema={"type": "object", "properties": {"x": {"type": "integer"}}})
check("offline provider returns schema-shaped json", stub == {"x": 0})
check("offline health check", offline.health_check()[0])

# -------------------------------------------------------------------- database
with tempfile.TemporaryDirectory() as tmp:
    db = Database(str(Path(tmp) / "test.db"))

    profile = Profile(
        name="Test",
        headline="Senior data engineer",
        summary="Ten years building Python data platforms.",
        location="Manchester, UK",
        preferences={
            "target_titles": ["data engineer"],
            "target_locations": ["manchester", "remote"],
            "remote_preference": "remote",
            "target_seniority": "senior",
            "target_salary": 80000,
            "exclude_keywords": ["unpaid"],
            "exclude_companies": ["BadCorp"],
            "ai_weight": 0.5,
        },
    )
    profile.id = db.create_profile(profile)
    check("profile created", profile.id is not None)

    item_id = db.upsert_item(
        ProfileItem(
            profile_id=profile.id,
            kind="experience",
            title="Senior Data Engineer",
            org="Acme",
            description="Built ETL pipelines",
            highlights=["Cut pipeline runtime by 60%"],
            tags=["python", "airflow", "dbt", "sql", "aws"],
        )
    )
    db.upsert_item(ProfileItem(profile_id=profile.id, kind="skill", title="Python, SQL, Spark, Terraform"))
    profile = db.get_profile(profile.id)
    check("items round-trip", len(profile.items) == 2)
    check("skills extracted", {"python", "sql", "airflow"} <= {s.lower() for s in profile.skills()})
    check("master cv renders", "Senior Data Engineer" in profile.as_markdown())

    # update + delete
    profile.headline = "Staff data engineer"
    db.update_profile(profile)
    check("profile update persists", db.get_profile(profile.id).headline == "Staff data engineer")
    db.delete_item(item_id)
    check("item delete", len(db.get_profile(profile.id).items) == 1)
    db.upsert_item(
        ProfileItem(
            profile_id=profile.id,
            kind="experience",
            title="Senior Data Engineer",
            org="Acme",
            description="Built ETL pipelines with Airflow and dbt on AWS",
            tags=["python", "airflow", "dbt", "sql", "aws"],
        )
    )
    profile = db.get_profile(profile.id)

    copy_id = db.duplicate_profile(profile.id, "Test copy")
    check("profile duplicated with items", len(db.get_profile(copy_id).items) == len(profile.items))
    db.delete_profile(copy_id)
    check("profile deleted", len(db.list_profiles()) == 1)

    # ------------------------------------------------------------ hard filters
    good = Posting(title="Senior Data Engineer", company="Acme", location="Remote, UK", remote="remote",
                   description="Python, Airflow, dbt, AWS. Salary £85,000 - £95,000.", posted_at="2026-08-28T00:00:00+00:00")
    unpaid = Posting(title="Intern", company="Foo", description="This is an unpaid internship")
    blocked = Posting(title="Engineer", company="BadCorp Ltd", description="Python")
    check("clean posting survives filters", hard_filter(good, profile.preferences) == "")
    check("excluded keyword filters", "unpaid" in hard_filter(unpaid, profile.preferences))
    check("blocked company filters", "BadCorp" in hard_filter(blocked, profile.preferences))

    # --------------------------------------------------------------- scoring
    noise = Posting(title="Warehouse Operative", company="Bar", location="Leeds", remote="onsite",
                    description="Lifting boxes, forklift licence required.")
    corpus = Corpus([good.as_text(), noise.as_text(), profile.as_text()])
    vec = corpus.vector(profile.as_text())
    good_score, breakdown = score_posting(good, profile, corpus, vec)
    noise_score, _ = score_posting(noise, profile, corpus, vec)
    check("relevant posting outranks noise", good_score > noise_score, f"{good_score} vs {noise_score}")
    check("breakdown has all components", set(breakdown) == {"skills", "similarity", "title", "seniority", "location", "salary", "recency"})
    check("score is 0-100", 0 <= good_score <= 100)

    # ------------------------------------------------------- harvest pipeline
    class FakeSource(JobSource):
        spec = SourceSpec(kind="fake", label="Fake", help="test only", fields=[])

        def fetch(self, query: str = "", limit: int = 100):
            for posting in (good, noise, unpaid, blocked):
                if matches_query(posting, query):
                    yield self._posting(
                        title=posting.title, company=posting.company, location=posting.location,
                        remote=posting.remote, description=posting.description, posted_at=posting.posted_at,
                        url=f"https://example.test/{posting.title}",
                    )

    SOURCES["fake"] = FakeSource
    db.add_source("Fake board", "fake", {})
    report = harvest(db, profile, llm=offline)
    check("harvest stored survivors", report.stored == 2, f"stored={report.stored}")
    check("harvest filtered the rest", report.filtered == 2, f"filtered={report.filtered}")
    check("harvest recorded no errors", not report.errors, str(report.errors))

    # dedupe: harvesting again must not create duplicates
    harvest(db, profile, llm=offline)
    check("dedupe by fingerprint", len(db.list_postings()) == 2, str(len(db.list_postings())))

    count = rescore(db, profile)
    check("rescore covered all postings", count == 2)
    rows = db.scored_rows(profile.id)
    check("scored rows ordered by score", rows[0]["final"] >= rows[1]["final"])
    check("top match is the relevant one", rows[0]["title"] == "Senior Data Engineer", rows[0]["title"])

    db.set_status(rows[0]["id"], profile.id, "shortlist", "looks good")
    refreshed = db.scored_rows(profile.id)[0]
    check("tracker status persists", refreshed["status"] == "shortlist")
    check("tracker notes persist", refreshed["notes"] == "looks good")

    db.set_setting("llm_config", {"provider": "offline", "model": "x"})
    check("settings round-trip", db.get_setting("llm_config")["provider"] == "offline")

    # keyword query filter
    filtered_report = harvest(db, profile, query='"data engineer"')
    check("query filter narrows results", filtered_report.fetched == 1, f"fetched={filtered_report.fetched}")

# ------------------------------------------------------- careers-page strategies
from unittest.mock import patch

import jobhunt.sources.web as web

JSONLD_PAGE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Platform Engineer",
 "description":"<p>Python and Kubernetes.</p>","datePosted":"2026-08-01",
 "jobLocationType":"TELECOMMUTE","employmentType":"FULL_TIME","url":"/jobs/42",
 "hiringOrganization":{"@type":"Organization","name":"Widgets Ltd"},
 "jobLocation":{"@type":"Place","address":{"addressLocality":"Leeds","addressCountry":"UK"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"GBP",
   "value":{"@type":"QuantitativeValue","minValue":60000,"maxValue":75000,"unitText":"YEAR"}}}
</script></head><body></body></html>"""

LINKS_PAGE = """<html><body><table>
<tr><td><a href="/careers/senior-dev">Senior Developer</a></td><td>Remote, UK</td></tr>
<tr><td><a href="/careers/designer">Product Designer</a></td><td>London, UK</td></tr>
<tr><td><a href="/about">About us</a></td><td>not a job</td></tr></table></body></html>"""


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


def _fetch_page(html: str):
    from jobhunt.sources import build_source

    with patch.object(web, "polite_get", lambda *a, **k: _Response(html)):
        source = build_source("careers_page", {"url": "https://widgets.test/careers", "company": "Widgets"})
        return list(source.fetch(limit=10))


jsonld_jobs = _fetch_page(JSONLD_PAGE)
check("careers page reads JSON-LD", len(jsonld_jobs) == 1 and jsonld_jobs[0].title == "Platform Engineer")
check("JSON-LD salary parsed", jsonld_jobs and (jsonld_jobs[0].salary_min, jsonld_jobs[0].salary_max) == (60000.0, 75000.0))
check("JSON-LD remote flag", jsonld_jobs and jsonld_jobs[0].remote == "remote")
check("JSON-LD company overrides default", jsonld_jobs and jsonld_jobs[0].company == "Widgets Ltd")

link_jobs = _fetch_page(LINKS_PAGE)
check("careers page link heuristic finds jobs", len(link_jobs) == 2, str([j.title for j in link_jobs]))
check("link heuristic skips non-job links", all("About" not in j.title for j in link_jobs))
check("link heuristic picks up nearby location", link_jobs and link_jobs[0].location == "Remote, UK")

# ------------------------------------------------------------------- adapters
from jobhunt.sources.registry import SOURCE_CLASSES

check("every adapter declares a spec", all(getattr(c, "spec", None) for c in SOURCE_CLASSES))
check("adapter kinds are unique", len({c.spec.kind for c in SOURCE_CLASSES}) == len(SOURCE_CLASSES))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("All checks passed.")
