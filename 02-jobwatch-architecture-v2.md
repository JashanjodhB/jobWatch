# jobwatch — Architecture & Build Specification (v2)

> **How to use this document:** This is the spec for Claude Code. Point Claude Code at this file and work through the build phases in Section 14 one at a time. Each phase has explicit acceptance criteria — do not advance until they pass. Section 13 (Anti-Requirements) lists mistakes that are destructive or unrecoverable; treat it as binding.

**Changes from v1:** No paid dependencies of any kind — the LLM classifier is removed entirely and replaced by a human review queue in the web UI. Companies now have *multiple* sources, with the company's own career site preferred over third-party ATS boards. A local web UI is now a first-class component. Discord is the only notification channel.

---

## 1. Purpose and scope

**What this is:** A self-hosted service that detects new internship postings at large companies within ~60 seconds of publication, pushes a Discord notification with a direct apply link, and exposes a local web UI for triage, tuning, and health monitoring.

**Target roles:** software engineering, AI/ML engineering, data engineering, and adjacent (infra, platform, quant dev, research engineering). Internships and co-ops. Not new-grad full-time, not PhD-required research residencies, not business/PM.

**Deployment target:** A dedicated Ubuntu Server box on a residential connection (see `01-laptop-server-setup.md`). Single process. Development happens on a separate machine over VS Code Remote-SSH.

### Non-goals
- No application autofill or auto-submit.
- No multi-user support or authentication beyond network-level access control.
- No horizontal scaling. One process handles the entire load comfortably.
- No public internet exposure. The web UI is reachable only over Tailscale.

### Success criteria
1. Median latency from posting appearing on a source to Discord notification: **under 90 seconds for hot-tier companies**, under 20 minutes for warm tier.
2. **Zero duplicate alerts** for the same requisition — including when the same job is found by two different sources for the same company.
3. **Zero alerts on cold start**, on database restore, or when reconciling a backlog after an outage.
4. **Zero recurring cost.** Electricity only.
5. A broken source is detected and surfaced in the UI within 24 hours.
6. Idle usage under 400MB RSS and 5% of one CPU core, web UI included.

---

## 2. The zero-cost invariant

**Binding constraint: this system must have no paid dependencies, no API keys that meter usage, and no free tiers that can lapse into billing.**

Every data source must be an endpoint that a normal browser hits when a human loads a public careers page. If reaching a source requires registering for an API key, agreeing to commercial terms, or a paid plan, it is out of scope — scrape the public page instead or drop the company.

### Explicitly excluded

| Excluded | Reason | What we do instead |
|---|---|---|
| Anthropic / OpenAI APIs | Metered | Rules + human review queue (§8) |
| LinkedIn API | Paid, restricted, aggressive anti-automation | Never used. Not a source. |
| Indeed / Glassdoor APIs | Deprecated or paid partner-only | Company sources directly |
| SerpAPI, RapidAPI job aggregators | Paid per-call | Direct + ATS adapters |
| Adzuna, JSearch, Coresignal | Paid tiers | Direct + ATS adapters |
| iCIMS API | Requires paid partnership | `html` adapter against the public iCIMS board |
| Managed DB, hosted queue, hosted cron | Recurring cost | SQLite, in-process asyncio |
| Paid proxies | Recurring, expensive | Residential IP + polite backoff (§13.5) |

### Permitted (public, unauthenticated)

Greenhouse job boards, Lever postings, Ashby job boards, Workday CXS endpoints, SmartRecruiters public postings, and any company's own careers-page JSON endpoint. All of these are the same endpoints a browser calls when rendering a public page. None require a key.

**Rule for adding a source:** open the careers page in a browser with an empty profile and no login. If the postings render, the endpoint behind them is fair game. If you had to sign in or sign up, it is not.

---

## 3. Technology decisions

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Adapter ecosystem; matches the roles being applied for. |
| Package manager | `uv` | Fast, lockfile-based, manages the venv. |
| HTTP | `httpx` (async) | HTTP/2, pooling, real timeout semantics. |
| Concurrency | `asyncio` | Pure network I/O. |
| Scheduler | Hand-rolled async loop | Per-source backoff state needs custom logic. |
| State | SQLite + WAL | Single writer, tiny dataset, trivially backed up. |
| Web framework | `FastAPI` + `Jinja2` | Server-rendered. Shares the event loop with the poller. |
| Web server | `uvicorn` (embedded) | Runs as an asyncio task in the same process. |
| Frontend | **HTMX, vendored** | No build step, no npm, no node on the server. |
| CSS | Hand-written, single file | ~400 lines. No framework. |
| Config | YAML (seed) + SQLite (runtime) | UI must be able to edit rules and companies. |
| Validation | `pydantic` v2 | Boundary validation for every adapter response. |
| HTML parsing | `selectolax` | Fallback adapters only. |
| Headless fallback | `playwright` | Optional extra; protected sites only. |
| Logging | `structlog` → JSON → journald | `journalctl -u jobwatch -f` is the ops surface. |

**Dependency discipline.** Core service runs on `httpx`, `pydantic`, `pyyaml`, `structlog`, `fastapi`, `uvicorn`, `jinja2`. Everything else is an optional extra; a missing extra degrades the affected adapter, never crashes the process.

**Vendor HTMX** (`static/vendor/htmx.min.js`, ~14KB) rather than loading from a CDN. The server should not depend on a third party being up to render its own dashboard.

---

## 4. Repository layout

```
jobwatch/
├── pyproject.toml
├── README.md
├── config/
│   ├── companies.yaml          # seed registry — imported to DB on first run
│   ├── filters.yaml            # seed classification rules
│   └── settings.yaml           # intervals, tiers, timeouts
├── src/jobwatch/
│   ├── main.py                 # entrypoint; runs scheduler + web as asyncio tasks
│   ├── config.py               # pydantic models, YAML load, DB seeding
│   ├── db.py                   # schema, migrations, all SQL
│   ├── models.py               # RawPosting, FetchResult, Company, Source
│   ├── http.py                 # shared client, backoff, conditional requests
│   ├── scheduler.py            # tier logic, due-source selection, concurrency
│   ├── normalize.py            # title/location normalization, dedup + merge keys
│   ├── pipeline.py             # fetch → normalize → persist → classify → enqueue
│   ├── adapters/
│   │   ├── base.py             # SourceAdapter protocol, registry, fallback chain
│   │   ├── greenhouse.py
│   │   ├── lever.py
│   │   ├── ashby.py
│   │   ├── workday.py
│   │   ├── smartrecruiters.py
│   │   ├── direct/             # one module per company-owned careers endpoint
│   │   │   ├── __init__.py     # registry keyed by company slug
│   │   │   ├── amazon.py
│   │   │   ├── apple.py
│   │   │   ├── microsoft.py
│   │   │   └── ...
│   │   ├── html.py             # generic CSS-selector fallback
│   │   └── browser.py          # playwright fallback (optional extra)
│   ├── classify/
│   │   ├── rules.py            # regex engine
│   │   └── verdicts.py         # title → verdict cache, human decisions
│   ├── notify/
│   │   ├── outbox.py           # durable queue, retry
│   │   └── discord.py
│   ├── health.py               # heartbeat, drift detection
│   ├── discover.py             # careers URL → candidate source config
│   ├── web/
│   │   ├── app.py              # FastAPI app factory, routes
│   │   ├── routes/             # feed, review, filters, companies, health, outbox
│   │   ├── templates/          # Jinja2
│   │   └── static/
│   │       ├── app.css
│   │       └── vendor/htmx.min.js
│   └── cli.py
└── tests/
    ├── fixtures/               # captured real responses, one per adapter
    └── test_*.py
```

---

## 5. Sources: multiple per company, direct preferred

This is the largest structural change from v1. A company no longer has *an* adapter; it has an **ordered list of sources**, all of which are polled, with results unioned.

### Why both

A company's own careers site and its ATS board are not always the same set of postings. Some roles appear on the corporate site first and propagate to the board minutes or hours later. Some never appear on the board at all. Polling both and unioning costs one extra request per cycle and closes a real gap.

The ordering matters for **attribution**, not exclusion: when the same requisition arrives from two sources, the alert links to the higher-priority source (the company's own site), because that's the canonical apply path.

### Source priority

1. **`direct`** — the company's own careers site JSON endpoint. Preferred when one exists.
2. **ATS adapter** — `greenhouse`, `lever`, `ashby`, `workday`, `smartrecruiters`.
3. **`html`** — CSS selectors against the public board.
4. **`browser`** — Playwright, last resort.

Each source independently carries its own backoff state, conditional-request headers, and fallback adapter. One source failing does not disable the company.

### Cross-source deduplication

Two sources will report the same job with different requisition IDs. This makes a second key necessary.

```python
def dedup_key(company_slug: str, source_name: str, posting: RawPosting) -> str:
    """Identity WITHIN a source. Prevents re-processing."""
    if posting.req_id:
        basis = f"{company_slug}|{source_name}|req|{posting.req_id}"
    else:
        basis = f"{company_slug}|{source_name}|syn|{normalize_title(posting.title)}"
    return sha256(basis)[:32]

def merge_key(company_slug: str, posting: RawPosting) -> str:
    """Identity ACROSS sources. Gates alerting."""
    basis = f"{company_slug}|{normalize_title(posting.title)}"
    return sha256(basis)[:32]
```

**The alerting rule: one notification per `merge_key`, ever.** A row may be newly inserted (new `dedup_key`) while its `merge_key` has already been alerted from another source — in that case, persist the row, attach the additional source URL, and send nothing.

`merge_key` deliberately excludes location and requisition ID. One role posted in six cities across two sources is one alert.

`normalize_title` must, case-insensitively: strip years (`2026`, `2027`), strip season words adjacent to a year, strip requisition numbers, strip trailing parenthetical location fragments, normalize dash variants, collapse whitespace, and lowercase. These must all normalize identically:

```
"2027 Summer Intern - Software Engineer (Austin, TX)"
"Summer Intern – Software Engineer"
"Software Engineer Intern, Summer 2027 [REQ-88213]"
→ "intern software engineer"
```

Write the tests for this before the function. It is the correctness core of the whole system.

### Building a `direct` adapter — the reusable recipe

1. Open the company's careers page in a browser, DevTools → Network → filter **Fetch/XHR**.
2. Type a search term or change a filter. Watch for a request returning JSON containing job titles.
3. Right-click → **Copy as cURL**. This captures the exact method, headers, and body.
4. Strip the request to the minimum that still returns 200: usually just `Accept: application/json`, `Content-Type` if it's a POST, and a plain User-Agent. **Drop all cookies.** If it stops working without cookies, note that — it likely needs the `browser` adapter.
5. Identify the pagination parameter and the total-count field.
6. Save the response to `tests/fixtures/direct_<slug>.json`.
7. Write the pydantic model from the fixture, then the adapter.
8. Confirm the apply URL — many endpoints return a path fragment that must be joined to a base URL, not a full link.

Step 4 is the important one. If it works with no auth and no cookies, it's a public endpoint and safe to poll politely.

---

## 6. Data model

SQLite, WAL, `synchronous=NORMAL`, `busy_timeout=5000`. Timestamps are ISO-8601 UTC strings.

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE companies (
    slug              TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL,
    tier              TEXT NOT NULL CHECK(tier IN ('hot','warm','cold')),
    enabled           INTEGER NOT NULL DEFAULT 1,
    careers_url       TEXT,
    notes             TEXT
);

-- One row per (company, source). This is what the scheduler iterates.
CREATE TABLE sources (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_slug           TEXT NOT NULL REFERENCES companies(slug) ON DELETE CASCADE,
    adapter                TEXT NOT NULL,
    adapter_config         TEXT NOT NULL,        -- JSON
    priority               INTEGER NOT NULL,     -- 1 = canonical apply path
    enabled                INTEGER NOT NULL DEFAULT 1,
    fallback_adapter       TEXT,
    fallback_config        TEXT,
    -- scheduling state
    last_attempt_at        TEXT,
    last_success_at        TEXT,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    backoff_until          TEXT,
    using_fallback         INTEGER NOT NULL DEFAULT 0,
    -- conditional requests
    etag                   TEXT,
    last_modified          TEXT,
    -- drift detection
    baseline_posting_count INTEGER,
    seeded                 INTEGER NOT NULL DEFAULT 0,
    UNIQUE(company_slug, adapter)
);

CREATE INDEX idx_sources_due ON sources(enabled, backoff_until);

CREATE TABLE jobs (
    dedup_key         TEXT PRIMARY KEY,
    merge_key         TEXT NOT NULL,
    company_slug      TEXT NOT NULL REFERENCES companies(slug),
    source_id         INTEGER NOT NULL REFERENCES sources(id),
    req_id            TEXT,
    title             TEXT NOT NULL,
    normalized_title  TEXT NOT NULL,
    locations         TEXT NOT NULL,            -- JSON array
    url               TEXT NOT NULL,
    source_posted_at  TEXT,                     -- UNTRUSTED, display only
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    classification    TEXT NOT NULL DEFAULT 'pending'
                        CHECK(classification IN ('pending','match','reject','review')),
    class_source      TEXT,                     -- 'rules' | 'manual' | 'seed'
    category          TEXT,
    alerted_at        TEXT,
    -- application tracking (Phase 10)
    app_status        TEXT DEFAULT 'none'
                        CHECK(app_status IN ('none','saved','applied','interview','offer','rejected')),
    app_updated_at    TEXT
);

CREATE INDEX idx_jobs_merge   ON jobs(merge_key);
CREATE INDEX idx_jobs_company ON jobs(company_slug);
CREATE INDEX idx_jobs_review  ON jobs(classification) WHERE classification = 'review';
CREATE INDEX idx_jobs_feed    ON jobs(first_seen_at DESC);

-- Records that a merge_key has been alerted. Separate table so it survives
-- any future change to how job rows are stored.
CREATE TABLE alerted_merges (
    merge_key    TEXT PRIMARY KEY,
    company_slug TEXT NOT NULL,
    alerted_at   TEXT NOT NULL,
    dedup_key    TEXT NOT NULL
);

-- Human verdicts, cached globally by normalized title.
-- This is what replaces the LLM classifier.
CREATE TABLE title_verdicts (
    normalized_title TEXT PRIMARY KEY,
    verdict          TEXT NOT NULL CHECK(verdict IN ('match','reject')),
    category         TEXT,
    source           TEXT NOT NULL CHECK(source IN ('rules','manual')),
    decided_at       TEXT NOT NULL,
    sample_title     TEXT,     -- an original title, for UI context
    sample_company   TEXT
);

-- Editable classification rules. Seeded from filters.yaml, then UI-owned.
CREATE TABLE filter_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK(kind IN
                 ('require_any','role_any','exclude_any','location_exclude')),
    pattern    TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    note       TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, pattern)
);

CREATE TABLE outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_key   TEXT NOT NULL,
    payload     TEXT NOT NULL,                  -- JSON
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    status      TEXT NOT NULL DEFAULT 'queued'
                  CHECK(status IN ('queued','sent','failed')),
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);

CREATE INDEX idx_outbox_queued ON outbox(status) WHERE status = 'queued';

CREATE TABLE poll_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL,
    at            TEXT NOT NULL,
    outcome       TEXT NOT NULL,   -- 'ok'|'not_modified'|'error'|'empty'
    posting_count INTEGER,
    new_count     INTEGER,
    duration_ms   INTEGER,
    error         TEXT
);

CREATE INDEX idx_poll_log_at ON poll_log(at);
```

**Retention:** prune `poll_log` beyond 30 days at startup. **Never prune `jobs`, `alerted_merges`, or `title_verdicts`.** Those three tables are why the system doesn't repeat itself.

---

## 7. The pipeline

Order is load-bearing.

```
scheduler selects due sources (respects tier interval, backoff, concurrency cap)
  → adapter.fetch()  [timeout, conditional headers, semaphore]
  → 304? record poll_log, done
  → normalize each posting → dedup_key + merge_key
  → INSERT ... ON CONFLICT(dedup_key) DO UPDATE SET last_seen_at, locations
  → collect ONLY rows the INSERT actually created
  → if source.seeded == 0:
        mark all new rows classification='reject', class_source='seed'
        record their merge_keys in alerted_merges (suppressed)
        set seeded=1
        ALERT NOTHING
  → else for each genuinely new row:
        classify (rules → verdict cache → 'review')
        if classification == 'match' or 'review':
            if merge_key already in alerted_merges: attach source, send nothing
            else: insert alerted_merges, enqueue to outbox
  → outbox worker delivers to Discord
```

Seeding must also apply when a source is newly added to an existing company, and when the database is restored and a source's rows are missing.

---

## 8. Classification without an API

Three stages. No network calls, no cost.

### Stage 1 — verdict cache
Look up `normalized_title` in `title_verdicts`. Hit → done. This is checked *first*, so a human decision made once is never revisited, and it overrides the rules.

### Stage 2 — rules
Evaluate `filter_rules` from the database (seeded from `config/filters.yaml`):

- Any `exclude_any` match → **reject**
- No `require_any` match → **reject**
- `require_any` hit **and** `role_any` hit → **match**
- `require_any` hit but no `role_any` hit → **review**

Rules also write to `title_verdicts` with `source='rules'` for clear match/reject outcomes, so repeat titles skip evaluation entirely.

### Stage 3 — human review queue
`review` items do two things simultaneously:

1. **They are alerted anyway**, flagged in Discord with a `?` marker.
2. They appear in the web UI review queue.

Alerting unreviewed items is deliberate. A false positive costs three seconds of your attention; a false negative costs an internship. The review queue exists to make the *next* occurrence of that title automatic, not to gate the current one.

Your verdict in the UI writes `title_verdicts` with `source='manual'`. Every future posting with that normalized title classifies instantly, forever, at zero cost. After a few weeks of use the queue empties to near-nothing — the space of distinct internship titles at any fixed set of companies is small and repeats heavily.

**This is strictly better than the LLM approach it replaces:** the verdicts are exactly right rather than approximately right, they're permanent, and they cost nothing.

`config/filters.yaml` seed:

```yaml
require_any:
  - '\bintern(ship)?\b'
  - '\bco-?op\b'
  - '\bsummer analyst\b'
  - '\bindustrial placement\b'
  - '\bapprentice\b'
  - '\b20\d\d\s+summer\b'

role_any:
  - '\bsoftware\b'
  - '\bengineer(ing)?\b'
  - '\b(swe|sde|mle)\b'
  - '\bmachine learning\b'
  - '\b(ai|ml)\b'
  - '\bdata\b'
  - '\b(backend|frontend|full[- ]?stack)\b'
  - '\b(infrastructure|platform|systems)\b'
  - '\bquantitative (developer|technologist)\b'
  - '\bresearch engineer\b'

exclude_any:
  - '\bph\.?d\b'
  - '\breturning intern\b'
  - '\bmba\b'
  - '\b(sales|marketing|recruit(ing|er))\b'
  - '\bnew ?grad\b'
  - '\bmanager\b'

location_exclude:
  - 'India'
  - 'Poland'
  - 'Ireland'
```

---

## 9. Notification — Discord only

Single channel: a Discord webhook. No Telegram, no SMS, no third-party push service. Free, no account limits, sub-second delivery, and it works on desktop and mobile with a single app you already have.

All alerts pass through the `outbox` table. Nothing sends directly from the pipeline — this is what makes delivery survive a restart mid-send.

**Retry:** exponential backoff, up to 5 attempts, then `status='failed'`. Failed rows are visible and replayable in the web UI, which serves as the fallback surface in place of a second channel.

**Rate limiting:** Discord webhooks allow roughly 5 requests per 2 seconds per webhook. Cap at 2/second and queue the excess. During an August surge you will hit this.

**Format** — use a Discord embed, color-coded by classification:

```
🟢 Stripe · Software Engineering Intern, Summer 2027
   San Francisco · Seattle · NYC
   Detected 14s ago · via careers.stripe.com
   [Apply]
```

Green embed for `match`, amber for `review` (with a "needs review" footer). Include a link back to the local UI item for one-tap triage from your phone.

**Batching by tier:** hot tier sends individually and immediately. Warm and cold accumulate into a digest embed flushed every 30 minutes.

**Optional secondary (off by default):** SMTP email via an existing Gmail account with an app password. Costs nothing, but Discord plus the UI is sufficient; leave it disabled unless you find Discord unreliable.

---

## 10. Web UI

### Purpose and access

A single-operator dashboard for triage and tuning. Read-heavy, checked several times a day, frequently from a phone.

**Binding: never expose this to the public internet.** Bind uvicorn to the Tailscale interface address, or to `127.0.0.1` fronted by `tailscale serve`. `ufw` allows the UI port on `tailscale0` only. No login, because network-level access control is the authentication.

**Process model:** the web app runs as an asyncio task in the same process as the scheduler, sharing the event loop and the SQLite connection manager. This avoids multi-writer lock contention. Wrap the uvicorn task so an unhandled web error can never take down the poller — the scheduler is the product, the UI is an accessory.

> **Fallback:** if the shared-process model causes problems, split into two systemd units against the same WAL database. Readers are safe; make the UI's writes serialized through a short transaction with `busy_timeout` set. Do this only if forced.

### Screens

**1. Feed** — the default view. Reverse-chronological matched postings. Leftmost column is **detection age**, not posting date, because age-since-detection is the number that actually matters. Filters: company, category, tier, date range, application status. One-tap apply link. Marking a job "applied" is one click.

**2. Review queue** — the classification workhorse. Titles awaiting a verdict, shown one at a time as a card with the original title, company, location, and the rule evaluation that produced the ambiguity. Two primary actions: **match** or **reject**, with an optional category tag.

This screen is keyboard-first: `J`/`K` to move, `M` to match, `R` to reject, `1`–`4` to tag a category, `U` to undo. A verdict writes `title_verdicts` immediately and advances. You should be able to clear thirty items in under a minute without touching the mouse.

**3. Filter workbench** — edit `filter_rules` with a live preview. As you type a pattern, the panel shows: how many of the last 90 days' titles it matches, which currently-matched jobs would become rejected, and which rejected jobs would become matched. This turns regex tuning from guesswork into a diff. Include an "export to filters.yaml" button so rules can be committed to git.

**4. Companies** — the registry. Add, edit, disable. Per-company source list with priority ordering. A **Test fetch** button that runs the adapter once and shows parsed postings without persisting anything. A **Discover** input: paste a careers URL, get a candidate source config to review and save.

**5. Health** — per-source status table: adapter, last success, consecutive failures, whether it's on its fallback, posting count vs. 7-day baseline. Sources with drift alarms sort to the top and are visually distinct. Includes the poll-log tail for debugging.

**6. Outbox** — queued, sent, and failed notifications with a replay button.

### Visual direction

The subject is a stream of time-critical arrivals that you scan quickly and act on. Design it as an **arrivals board**, not a generic admin panel. That metaphor is specific to what this actually is, and it earns the dense-row layout, the prominence of elapsed time, and the amber signal color.

Compact token system:

```css
--ink:        #0F1626;  /* deep navy base */
--surface:    #1A2334;  /* rows, cards */
--surface-hi: #243049;  /* hover, active row */
--text:       #E8EAF0;
--muted:      #7A8499;
--signal:     #F5A524;  /* amber: new, unreviewed, needs attention */
--confirmed:  #5EC1A0;  /* green: matched, applied */
--alarm:      #E5484D;  /* red: source failure, drift */
```

Type: a condensed grotesque for board rows (Archivo Narrow or Roboto Condensed, self-hosted — no CDN), a mono for timestamps, requisition IDs, and regex patterns (JetBrains Mono), system stack for body copy. Self-host everything; the server must render its own dashboard without external dependencies.

**Signature element:** the elapsed-time column. Every row leads with `14s` / `3m` / `2h` in mono, updating live via a lightweight ticker, right-aligned in a fixed-width gutter with a hairline rule separating it from the content. It's the one number the whole system exists to minimize, so it gets the most visually distinctive treatment. Everything else stays quiet.

Keep decoration to zero. No gradients, no shadows, no rounded cards. Hairline rules and generous vertical rhythm carry the structure.

**Quality floor:** responsive down to a 380px phone viewport (this will be read in bed at 7am). Visible keyboard focus rings. `prefers-reduced-motion` respected — the elapsed-time ticker becomes a poll-on-refresh rather than an animated count.

**Copy rules:** label things by what you control, not how the system works. "Sources" not "adapters" in user-facing labels. Buttons state what happens: "Mark applied," not "Submit." Empty states are invitations: an empty review queue reads "Nothing to review — rules are handling everything," not "No data."

### Routes

```
GET  /                      → feed
GET  /review                → review queue
POST /review/{key}/verdict  → record manual verdict (HTMX partial)
GET  /filters               → workbench
POST /filters/preview       → live diff (HTMX partial)
POST /filters               → save rule
GET  /filters/export        → filters.yaml download
GET  /companies             → registry
POST /companies/{slug}/test → test fetch, no persist (HTMX partial)
POST /discover              → careers URL → candidate config
GET  /health                → source status
GET  /outbox                → queue
POST /outbox/{id}/replay    → resend
POST /jobs/{key}/status     → application status
```

---

## 11. Adapters and their fallbacks

After `consecutive_failures >= 5`, the scheduler flips that source to `using_fallback=1`, retries the primary once daily, and flips back on success.

| Adapter | Mechanism | Fallback 1 | Fallback 2 |
|---|---|---|---|
| `direct/*` | Company's own careers JSON endpoint | `html` on the careers page | `browser` |
| `greenhouse` | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs` | Embed endpoint variant | `html` on the public board |
| `lever` | `GET api.lever.co/v0/postings/{company}?mode=json` | `html` on `jobs.lever.co/{company}` | — |
| `ashby` | Public job board endpoint | `html` on the board page | — |
| `workday` | `POST {host}/wday/cxs/{tenant}/{site}/jobs` | Same without `searchText`, paginate fully | `browser` |
| `smartrecruiters` | Public postings endpoint | `html` | — |
| `html` | `selectolax` + configured CSS selectors | — | `browser` |
| `browser` | Playwright, **intercept the XHR response** rather than scraping DOM | — | Disable source, alarm in UI |

Every adapter validates through a pydantic model and raises `AdapterError` with a payload snippet on mismatch. A schema change must surface as a loud specific error, never a silent empty list.

**Workday specifics:** requires `Accept: application/json` and `Content-Type: application/json` or it returns HTML. `postedOn` is a human string (`"Posted Today"`, `"Posted 30+ Days Ago"`) — **never parse it for newness.** Pagination via `offset`/`limit`; some tenants cap `limit` at 20. Apply URLs are built from `externalPath`, not provided whole.

**Endpoint shapes drift.** Hardcode nothing beyond what a committed fixture proves.

---

## 12. Observability

**Heartbeat.** Ping healthchecks.io (free tier) at the end of each scheduler tick, but **only if at least one source succeeded**. A process that is alive but failing everything must not report healthy.

**Drift detection**, daily, per source. Compare today's `posting_count` against the rolling 7-day median in `baseline_posting_count`:

- Count → **zero** when baseline was >5: **alarm immediately.** This is the silent-breakage signature.
- Count drops >60%: warn.
- `consecutive_failures > 0` for 24h: alarm.
- `using_fallback=1` for over 7 days: warn — the primary is probably gone for good; update the registry.

Alarms surface in the UI health screen *and* as a distinctly-styled Discord message.

**CLI** (the UI covers most of this, but keep it for headless debugging):

```
jobwatch status
jobwatch test <slug> [--adapter greenhouse]
jobwatch classify "<title>"
jobwatch replay <merge_key>
jobwatch discover <careers_url>
jobwatch export-config          # DB → YAML, for git
```

---

## 13. Anti-requirements

Ranked by cost of the mistake.

1. **Never alert on cold start.** Seeding must be silent, per source. A first run firing 3,000 Discord messages will get the webhook rate-limited and destroy your trust in the system on day one.
2. **Never alert twice for the same `merge_key`.** With multiple sources per company this is now the *default* failure — the same job legitimately arrives from two places. `alerted_merges` is checked before every enqueue, without exception.
3. **Never trust source timestamps for newness.** Workday's `postedOn` is a human string; several adapters omit dates; some backfill on edit. **Your own seen-set is the only truth about what is new.**
4. **Never delete from `jobs`, `alerted_merges`, or `title_verdicts`.** Postings vanish and reappear routinely from pagination glitches and recruiter edits.
5. **Never retry-storm.** Requests come from a residential IP that cannot be rotated. Exponential backoff with jitter, capped concurrency, honest User-Agent, conditional requests. Getting your home IP blocked by an ATS provider is effectively unrecoverable.
6. **Never add a source that requires an API key, account, or payment.** See §2. If you find yourself signing up for something, stop.
7. **Never expose the web UI to the internet.** Tailscale interface only. There is no login.
8. **Never let a web error kill the scheduler.** The UI is an accessory to the poller, not a peer.
9. **Never let one source stall the loop.** Every fetch gets a timeout and a try/except. One hanging request must not delay the others.
10. **Never swallow a parse error into an empty list.** An adapter returning `[]` because the schema changed looks exactly like a company with no jobs. Raise, and let drift detection catch it.
11. **Never commit secrets.** The Discord webhook URL lives in `/etc/jobwatch.env`, mode 600, gitignored.

---

## 14. Build phases

Sequential. Each phase must pass its acceptance criteria before the next begins.

### Phase 0 — Skeleton
`uv` scaffolding, pydantic config models, SQLite schema and migration runner, YAML→DB seeding, structlog, CLI stub, `SourceAdapter` protocol, `FakeAdapter` returning fixture data.
**Accept:** schema matches §6 exactly; `jobwatch status` runs against an empty DB; `pytest` passes with the fake adapter.

### Phase 1 — Minimum viable alerter
Greenhouse and Lever adapters. `normalize_title`, `dedup_key`, `merge_key`. Per-source cold-start seeding. Rules-only classification. Direct Discord send. Fixed 2-minute loop over ~20 companies.
**Accept:** first run seeds silently, zero alerts; an injected fake posting produces exactly one Discord message; running twice produces no second alert; `normalize_title` unit tests cover every case in §5.

### Phase 2 — Scheduler and resilience
Tier intervals, per-source backoff with jitter, conditional requests, concurrency semaphore, timeouts, `poll_log`.
**Accept:** 304s handled without reprocessing; 5 minutes of network loss produces backoff not a crash loop; restart resumes with zero duplicate alerts.

### Phase 3 — Outbox
Move Discord delivery behind the `outbox` table. Retry with backoff, rate limiting, tier-based digest batching.
**Accept:** killing the process mid-send results in exactly-once delivery on restart; an invalid webhook URL produces a `failed` row rather than a crash.

### Phase 4 — Multi-source and direct adapters
`sources` table wired into the scheduler. Cross-source merge-key gating. Two or three `direct` adapters built with the §5 recipe.
**Accept:** a company configured with both `direct` and `greenhouse` reporting the same job produces **one** alert, linking to the `direct` URL; disabling the direct source still yields the greenhouse result; both sources' rows persist.

### Phase 5 — Web UI shell
FastAPI + Jinja2 + vendored HTMX. Feed and Health screens. Tailscale-only binding. Full visual system from §10.
**Accept:** reachable over Tailscale from a phone, not reachable from the LAN by IP; feed renders 500 jobs without lag; elapsed-time column updates live; readable at 380px width; scheduler survives a deliberately raised exception in a route handler.

### Phase 6 — Review queue and verdict cache
`title_verdicts` wired as classification stage 1. Review screen with the keyboard bindings from §10.
**Accept:** a manual verdict causes the next posting with that normalized title to classify instantly with `class_source='manual'`; thirty items can be cleared keyboard-only; the cache survives restart.

### Phase 7 — Filter workbench
Rules editable from the UI with live preview diffing against historical titles. YAML export.
**Accept:** editing a pattern shows an accurate would-match / would-unmatch diff against the last 90 days; export produces a valid `filters.yaml` that round-trips through the seeder.

### Phase 8 — Workday and remaining ATS
Workday with full pagination and fixtures from at least three tenants. Ashby, SmartRecruiters, generic `html`. `discover` implemented and surfaced in the UI.
**Accept:** all three Workday fixtures parse; a tenant capping `limit` at 20 pages fully; `discover` correctly identifies the source type for 8 of 10 test companies.

### Phase 9 — Observability
Heartbeat, drift detection, alarms in UI and Discord, outbox screen, remaining CLI.
**Accept:** stubbing an adapter to return `[]` raises a drift alarm within one cycle; heartbeat does not ping when every source fails.

### Phase 10 — Expansion and polish
More `direct` adapters. `browser` fallback as an optional extra. Application status tracking in the feed. Seasonal interval scaling. Registry expansion toward 200+ companies.
**Accept:** service starts normally with Playwright **not** installed, skipping browser-tier sources with a clear log line; intervals shift correctly when the clock is set to different months.

---

## 15. Testing strategy

- **Fixtures over live calls.** Every adapter tests against a captured real response in `tests/fixtures/`. No network in the test suite — this is what lets you develop entirely on your dev laptop.
- **Dedup is the highest-value surface.** Property-test that any sequence of poll results across any number of sources containing a given requisition produces exactly one alert — regardless of ordering, repetition, source count, or intervening disappearance.
- **Cold-start test.** Seeding a source with 500 postings produces zero outbox rows.
- **Cross-source test.** Two sources reporting overlapping jobs with different req IDs produce alerts equal to the number of distinct merge keys.
- **Schema-drift test.** Feed each adapter a mutated fixture with a renamed field; assert `AdapterError`, not `[]`.
- **`--dry-run` flag** running the full pipeline and printing what would send. Primary tool for tuning filters against real data from the dev laptop.

---

## 16. Configuration reference

`config/companies.yaml`:

```yaml
- slug: stripe
  display_name: Stripe
  tier: hot
  careers_url: https://stripe.com/jobs/search
  sources:
    - adapter: greenhouse
      priority: 1
      config: {board_token: stripe}
      fallback_adapter: html
      fallback_config:
        url: https://stripe.com/jobs/search
        job_selector: "a[data-job-id]"

- slug: nvidia
  display_name: NVIDIA
  tier: hot
  careers_url: https://www.nvidia.com/en-us/about-nvidia/careers/
  sources:
    - adapter: direct.nvidia        # company's own site, canonical apply path
      priority: 1
      config: {}
      fallback_adapter: browser
    - adapter: workday              # polled too; results unioned
      priority: 2
      config:
        host: https://nvidia.wd5.myworkdayjobs.com
        tenant: nvidia
        site: NVIDIAExternalCareerSite
        search_text: intern
```

`config/settings.yaml`:

```yaml
tiers:
  hot:  {interval_seconds: 60,   batch_notifications: false}
  warm: {interval_seconds: 600,  batch_notifications: true}
  cold: {interval_seconds: 3600, batch_notifications: true}

seasonal_multipliers:
  8: 1.0
  9: 1.0
  10: 1.0
  11: 1.0
  1: 1.0
  default: 3.0

http:
  timeout_seconds: 20
  max_concurrency: 12
  user_agent: "jobwatch/2.0 (personal internship monitor)"
  backoff_base_seconds: 30
  backoff_max_seconds: 3600
  fallback_after_failures: 5

classification:
  on_review: alert          # alert | queue_only

notifications:
  discord_webhook_env: DISCORD_WEBHOOK_URL
  max_per_second: 2
  digest_interval_minutes: 30

web:
  bind_host: "100.x.x.x"    # Tailscale interface address
  bind_port: 8080
```

`/etc/jobwatch.env` (mode 600, never committed):

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
HEALTHCHECK_URL=https://hc-ping.com/...
JOBWATCH_DB=/var/lib/jobwatch/jobs.db
```

---

## 17. Consolidated fallback matrix

| Component | Primary | Fallback 1 | Fallback 2 | Detection signal |
|---|---|---|---|---|
| Source (per company) | Company's own careers endpoint | ATS adapter (union, not replacement) | `html`, then `browser` | 5 consecutive failures |
| Adapter correctness | pydantic validation | — | Disable source, alarm in UI | Posting count → 0 vs. baseline |
| Classification | Verdict cache | Regex rules | Alert anyway + review queue | No rule matches |
| Notification | Discord webhook | Outbox retry ×5 | Web UI outbox screen + replay | `status='failed'` rows |
| Web UI process | Shared asyncio task | Separate systemd unit, same WAL DB | CLI only | Lock contention or crashes |
| State | SQLite WAL | Nightly `.backup` | Off-machine copy (git / B2) | Backup script exit code |
| Host | Home laptop | Cloudflare Workers, hot tier only | GitHub Actions, cold tier only | healthchecks.io silence |
| Liveness | healthchecks.io | cron → email | — | — |

**Escalation rule for the host row:** do not pre-build the Cloudflare fallback. Track outage minutes for two months and migrate the hot tier only if you can point to a specific posting you missed.
