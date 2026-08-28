# CLAUDE.md

Self-hosted internship monitor. Polls public careers endpoints, classifies
postings, alerts to Discord/email, serves a local triage UI. Zero recurring
cost: no API keys, no paid tiers, nothing authenticated (§2).

## Running anything here

`uv` is installed but **not on PATH**, and the system Python is 3.11 — not this
project's 3.12. Two working forms:

```powershell
.venv\Scripts\python.exe -m pytest              # simplest; no PATH fiddling
.venv\Scripts\jobwatch.exe status

$env:PATH="$env:APPDATA\Python\Python311\Scripts;$env:PATH"; uv run pytest
```

**Set `JOBWATCH_DB` or pass `--db` for every CLI command.** It defaults to
`./data/jobs.db` *relative to cwd*, so a command run from elsewhere silently
creates a new empty database instead of erroring — the symptom is `status`
reporting zero postings while the service tracks thousands.

```powershell
$env:JOBWATCH_DB="data/jobs.db"
```

## Commands

`run` (scheduler + web UI; `--dry-run`, `--once`) · `status` · `test <slug>`
(fetch one source, print parsed postings) · `classify "<title>"` · `discover
<careers_url>` · `discover-bulk` · `notify-test` · `replay <merge_key>` ·
`backfill` (alert on what a silent cold start swallowed; always `--dry-run`
first) · `migrate` / `seed` · `export-config` · `adapters` · `backup`.

Full table with descriptions: README.md "Commands".

## Architecture

```
fetch (adapters) → normalize → persist → seed-or-classify → gate on merge_key → outbox → channels
```

- `normalize.py` — title normalization and the two identity keys. The
  correctness core. `dedup_key` = per-source posting identity;
  `merge_key` = company + normalized title, which is what collapses the same
  job seen across several sources into one alert.
- `pipeline.py` — the sequence above, plus source state (backoff, fallback flip).
- `scheduler.py` — tier intervals, backoff, due-source selection. Re-reads the
  DB each tick, so registry changes need no restart.
- `adapters/` — one module per ATS platform, registry in `base.py`.
- `classify/` — regex rules, then a human verdict cache.
- `notify/` — durable outbox in front of Discord and email.
- `web/` — FastAPI + Jinja2 + vendored HTMX. No build step.

`§N` in code comments and docstrings points at section N of
`02-jobwatch-architecture-v2.md`.

## Invariants worth not breaking

1. **An adapter never returns `[]` because parsing failed** (§13.10). An empty
   list is indistinguishable from a company with no open internships and fails
   silently for months. Raise `AdapterError` with a payload snippet; use
   `require_list()` from `adapters/base.py`, which is the guard for exactly this.
2. **A missing optional extra degrades one adapter, never the process** (§3).
   `selectolax` and `playwright` are imported lazily inside `fetch()`, which
   raises `AdapterUnavailable`.
3. **Never configure `fallback_adapter` without a working `fallback_config`.**
   `using_fallback` clears only on a *successful* poll, so a fallback that can
   never succeed permanently blocks a healthy primary. On any
   `stuck_on_fallback` alarm read `poll_log` before believing its diagnosis —
   it accuses the primary, and the fallback is usually the actual defect.
4. **Cold start must not flood — which is not the same as being silent.** A
   newly seeded source classifies its whole board and sends the matches as ONE
   digest (`classification.on_seed`, default `digest`). It was silent until
   2026-08-26, and silence cost more than the storm would have: seeding is per
   *source*, so the 08-25 bulk expansion buried 2,375 live internships that
   nothing ever revisited. `replay` cannot reach them — it only re-queues outbox
   rows, which a seeded job never got. `jobwatch backfill` is what recovers a
   silent backlog; it is safe to re-run because it skips any merge_key whose
   jobs have `alerted_at` set.
5. **No aggregators as posting sources.** Both identity keys derive from the
   *source's* `company_slug`, so a multi-employer feed collapses distinct
   employers into one alert. `discover-bulk` uses community feeds for company
   *discovery* only. LinkedIn was investigated twice and ruled out — blanket
   `Disallow` in robots.txt. Don't re-explore it.

## Adding an adapter

Model on `adapters/oracle.py` or `adapters/eightfold.py`. One method,
`async def fetch(ctx: FetchContext) -> FetchResult`. Then:

1. `@register("name")` on the class.
2. Add the module name to `_CORE_MODULES` in `adapters/__init__.py`
   (`direct/` modules self-register via `pkgutil`, no list to edit).
3. Optionally add a `_DEFAULT_FALLBACKS` entry in `base.py`.
4. Capture a real response into `tests/fixtures/<name>.json` and add a row to
   `CASES` in `tests/test_adapters.py` — that one row gets you the happy-path
   and schema-drift tests for free.

Platform quirks, config keys, and what breaks silently: `docs/ADAPTERS.md`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest        # 466 tests, ~16s, no network anywhere
.venv\Scripts\python.exe -m ruff check src tests
```

Network is never touched: adapters are tested against captured fixtures through
`httpx.MockTransport` helpers in `tests/conftest.py` (`json_client`,
`sequence_client`, `text_client`). Re-capture fixtures with
`tests/capture_fixtures.py`.

The highest-value tests are in `tests/test_pipeline.py`: cold-start silence,
cross-source merge gating, and a property test that any ordering of polls across
any number of sources containing a requisition yields exactly one alert.

## Config

`config/companies.yaml` is the **only** seeded registry.
`companies-expansion.yaml` and `companies-discovered.yaml` are stale staging
files — read for dedup, never loaded, not pending work. `filters.yaml` and
`settings.yaml` are the other two.

`jobwatch seed` is additive and safe. `seed --force` overwrites every row from
YAML, so never reach for it just to add a company. Neither touches scheduling
state — after fixing a stuck source, clear `using_fallback`,
`consecutive_failures` and `backoff_until` on the row by hand.

## Which file answers which question

These are long; read the section you need, not the whole file.

| Question | File |
|---|---|
| Setup, commands, what ships working | `README.md` (391 lines) |
| Running it as a service, health screen, a source broke, backups | `docs/OPERATIONS.md` (325 lines) |
| Why the design is this way; the `§N` references | `02-jobwatch-architecture-v2.md` (774 lines) |
| A specific ATS platform's quirks and config keys | `docs/ADAPTERS.md` |
