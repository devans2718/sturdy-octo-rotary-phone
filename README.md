# Job Hunt Copilot

A Streamlit app for running a job search properly: keep one master profile
("experience bank"), harvest postings from boards that all work differently,
rank them with a mix of deterministic scoring and an AI agent, and track what
you apply to. The model backend is swappable — Anthropic, OpenAI, a local
Ollama/LM Studio model, or nothing at all.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The app works with **no API key**: the deterministic half (harvesting, hard
filters, scoring, tracking) runs entirely offline. Add a provider in
**⚙️ Settings** to switch the AI half on.

---

## What it does

| Page | Purpose |
|---|---|
| **👤 Profile** | Build and edit the experience bank. Multiple profiles, duplicate/delete, CV import via the agent, JSON export/import, search preferences and scoring weights. |
| **🔌 Sources** | Add and test job sources. Ten adapters across three tiers. |
| **🔎 Search** | Harvest → hard-filter → deterministic score → AI pass over the shortlist. |
| **📊 Matches** | Ranked table, per-posting score breakdown, agent write-up, cover-letter drafting, application tracker, CSV export, whole-search strategy review. |
| **⚙️ Settings** | Point the app at any model endpoint; test the connection; back up your data. |

---

## Architecture

```
app.py                  Streamlit entry point + dashboard
pages/                  One file per screen
jobhunt/
  models.py             Profile, ProfileItem, Posting, Score  (storage-free dataclasses)
  db.py                 SQLite persistence; dedupe on posting fingerprint
  normalize.py          HTML→text, salary/date parsing, remote & seniority detection, tokenizer
  agent.py              Every LLM task, each with its own JSON schema
  state.py              Streamlit glue: cached DB, active profile, sidebar
  llm/                  Swappable model backends
    base.py             LLMProvider ABC + tolerant JSON recovery
    anthropic_provider.py    official `anthropic` SDK
    openai_compatible.py     any /chat/completions endpoint
    offline.py               no-network stub
    registry.py         PROVIDERS + UI presets
  sources/              Job source adapters
    base.py             JobSource ABC, per-host rate limiting, query matching
    ats_boards.py       Greenhouse, Lever, Ashby, Workable
    aggregators.py      Remotive, RemoteOK, Arbeitnow, Adzuna
    web.py              RSS/Atom, generic company careers page
    registry.py         SOURCES + starter set
  scoring/
    deterministic.py    Hard filters + weighted components + TF-IDF corpus
    pipeline.py         harvest / rescore / ai_pass / blend
smoke_test.py           Offline end-to-end test of everything above
```

### Sources: three tiers, because boards are not alike

1. **ATS boards** (Greenhouse, Lever, Ashby, Workable) — first-party, public
   JSON APIs, cleanest data. Ashby even exposes structured compensation, which
   the adapter prefers over regex.
2. **Aggregators** (Remotive, RemoteOK, Arbeitnow, Adzuna) — reach, at the cost
   of consistency. Some support server-side search; the rest are filtered
   locally by `matches_query`.
3. **The long tail** — an RSS/Atom adapter, and a generic careers-page adapter
   that tries three strategies in order of reliability:
   1. schema.org `JobPosting` JSON-LD (deterministic, exact),
   2. link + table heuristics (deterministic, approximate),
   3. the AI agent reading the page text (flexible, costs a call).

Adding a board means one class with a `spec` and a `fetch()` — the Sources page
renders its config form automatically from `spec.fields`.

### Scoring: deterministic first, AI second

**Hard filters** remove postings entirely (excluded keywords/companies, salary
floor, on-site-only, staleness). A visa-blocked role should disappear, not
merely rank low.

**Deterministic score** (free, runs on everything, fully explainable):

| Component | Signal |
|---|---|
| `skills` | saturating overlap between your experience bank and the posting |
| `similarity` | TF-IDF cosine between your profile and the posting, IDF over the harvest |
| `title` | overlap with your target titles |
| `seniority` | distance on intern→lead, parsed from title and body |
| `location` | work-mode preference × geography, with remote clearing geo constraints |
| `salary` | parsed range vs your target, undisclosed treated as a mild negative |
| `recency` | ~21-day half-life |

Weights are per-profile and tunable in the UI. The Matches page shows each
component's value, weight and contribution for any posting.

**AI pass** (paid, runs on the shortlist you choose): reads the full posting
against your master CV and returns a fit score, verdict, strengths, concrete
gaps, red flags, seniority read, keywords to mirror, and suggested bullets
drawn only from experience you actually have.

The two are combined by `blend(deterministic, ai, ai_weight)`. Set `ai_weight`
to 0 to ignore the model entirely, or 1 to let it decide. Postings the agent
hasn't seen keep their deterministic score, so the list never becomes
inconsistent mid-run.

### Swapping the model

Everything above the provider layer calls exactly two methods: `complete()` and
`complete_json()`. To add a backend, subclass `LLMProvider` in `jobhunt/llm/`,
implement `complete`, and register it in `registry.py::PROVIDERS`.

Built-in presets: Anthropic API, OpenAI API, Ollama, LM Studio, vLLM/custom,
and Offline. The OpenAI-compatible provider covers most self-hosted gateways
(llama.cpp, OpenRouter, Together, Groq …) — just change the base URL.

Each agent task ships a JSON schema that is passed to the API *and* restated in
the prompt, and responses go through a tolerant JSON recovery function. Strong
models use structured output; weaker local models still produce usable JSON.

---

## Configuration

Keys can come from the Settings page (stored in the SQLite file) or from the
environment — see `.env.example`. Environment variables are used when the
matching field is left blank.

```
ANTHROPIC_API_KEY     OPENAI_API_KEY
LOCAL_LLM_BASE_URL    LOCAL_LLM_API_KEY
JOBHUNT_DB            default: data/jobhunt.db
```

**Note:** keys saved through the Settings page are stored in plain text in the
local SQLite file, and the "back up profiles and sources" download includes any
source credentials (e.g. an Adzuna key). Prefer environment variables if that
matters to you, and don't commit `data/`.

---

## Testing

```bash
python smoke_test.py
```

Runs 51 checks — parsing, JSON recovery, profile CRUD, dedupe, hard filters,
score ordering, the harvest pipeline, the tracker, and both deterministic
careers-page strategies — against a temporary database, the offline provider
and a fake source. No network, no API key.

The Streamlit pages are checked with `streamlit.testing.v1.AppTest`:

```python
from streamlit.testing.v1 import AppTest
AppTest.from_file("pages/4_Matches.py").run()
```

---

## Scraping responsibly

Requests are rate-limited to one per second per host, run one-at-a-time per
host, and identify themselves with a descriptive User-Agent. Public ATS APIs
are designed to be read; **company careers pages are not necessarily** — check
the site's terms and `robots.txt` before pointing the careers-page adapter at
one, and keep `limit` modest. This tool is for finding jobs for yourself, not
for bulk data collection.

---

## Known limits

- CV import takes pasted text only; there is no PDF/DOCX extractor yet.
- Salary parsing deliberately returns nothing when unsure — a wrong number
  poisons the score worse than a missing one does.
- Deduplication keys on normalized company + title + location, so the same role
  posted with materially different titles will appear twice.
- `agent.rank_batch` (a cheap one-call triage over many titles) is implemented
  and tested but not yet wired into a UI button.
- The harvest is synchronous: many sources with `follow_links` enabled will be
  slow.
