# Build notes — 2026-09-01

## State: complete and verified

All five requirements are implemented and working end to end.

- `smoke_test.py` — 51 checks, all passing (no network, no API key).
- All six Streamlit pages run clean under `streamlit.testing.v1.AppTest`.
- Live-fetch verified against Greenhouse, Lever, Remotive, RemoteOK, Arbeitnow.
- Careers-page JSON-LD and link-heuristic strategies verified offline.
- ~4,200 lines of Python across 22 modules.

## Verified by hand (live network)

| Source | Result |
|---|---|
| Greenhouse (`stripe`) | jobs + locations parsed |
| Lever (`leverdemo`) | jobs + structured salary (£85,000) parsed |
| Remotive | jobs + remote flags parsed |
| RemoteOK | jobs parsed |
| Arbeitnow | jobs parsed |
| Ashby / Workable / Adzuna / RSS | code paths written, not exercised against a live token |

## Not done (deliberate — see README "Known limits")

1. `agent.rank_batch` — cheap one-call triage over many titles. Implemented and
   sound, but no UI button wired to it. Would go on the Search page between
   steps 2 and 3, to narrow a big harvest before spending per-posting calls.
2. PDF/DOCX CV import — text paste only right now. Would need `pypdf` /
   `python-docx` in `pages/1_Profile.py`'s import tab.
3. Async harvesting — the run is serial per source, so many sources with
   `follow_links` on is slow. A `ThreadPoolExecutor` in `scoring/pipeline.py::harvest`
   would fix it; the per-host rate limiter in `sources/base.py` is already
   thread-safe and would keep it polite.
4. Live tokens for Ashby/Workable/Adzuna were never tested — worth a `Test fetch`
   click on the Sources page before relying on them.

## Things worth knowing before changing this

- **Dedupe is fingerprint-based** (`Posting.compute_fingerprint`: normalized
  company + title + location prefix). Changing that formula orphans existing
  rows — clear the postings table if you touch it.
- **Postings are global; scores and tracker rows are per profile.** Deleting a
  profile does not delete postings.
- **`parse_salary` refuses to guess** — it needs a currency marker or a salary
  heading before it will accept a bare number. That is what stops "a team of 25
  people" becoming a £52,000 salary. Keep that guard if you extend it.
- **Streamlit's `use_container_width` is deprecated** in the installed version;
  this codebase uses `width="stretch"` / `width="content"` throughout.
