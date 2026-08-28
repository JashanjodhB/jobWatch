# jobwatch

Detects new internship postings at large companies within ~60 seconds, sends a
Discord message and/or an email with a direct apply link, and serves a local web
UI for triage.

Zero recurring cost: no API keys, no metered services, no paid tiers. Every
source is an endpoint a normal browser hits when a human loads a public careers
page.

Built to the specification in `02-jobwatch-architecture-v2.md`.

## Setup

Eight steps, about ten minutes. Commands are PowerShell; on Linux or macOS the
only differences are `cp` for `copy` and forward slashes.

### 1. Install

The one prerequisite is [uv](https://docs.astral.sh/uv/). It provisions Python
3.12 itself, so whatever Python you have installed does not matter.

```powershell
winget install --id=astral-sh.uv        # or: irm https://astral.sh/uv/install.ps1 | iex

git clone https://github.com/JashanjodhB/jobWatch.git
cd jobWatch
uv sync
```

### 2. Decide how you want to be told

```powershell
copy .env.example .env
```

Set up **Discord**, **email**, or both. `.env` is gitignored and must never be
committed.

**Discord** — the fast one. Arrives on your phone in seconds, takes a minute:

1. In Discord, pick a server you own, or make one: **+** in the left sidebar →
   *Create My Own* → *For me and my friends*.
2. **Server Settings** → **Integrations** → **Webhooks** → **New Webhook**.
3. Choose the channel it posts to, then **Copy Webhook URL**.
4. Paste it into `.env`:

   ```ini
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/abcdef...
   ```

**Email** — the durable one. Searchable and archivable, no app needed. For
Gmail:

1. Turn on 2-Step Verification on the Google account (App Passwords do not
   exist without it).
2. Go to <https://myaccount.google.com/apppasswords>, create one named
   `jobwatch`, and copy the 16 characters. **Your normal Google password will
   not work** — Google rejects it and the error does not explain why.
3. Uncomment the SMTP block in `.env` and fill it in:

   ```ini
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_SECURITY=starttls
   SMTP_USERNAME=you@gmail.com
   SMTP_PASSWORD=abcd efgh ijkl mnop
   EMAIL_FROM=jobwatch <you@gmail.com>
   EMAIL_TO=you@gmail.com
   ```

   Spaces in the app password are fine — they are Google's display formatting
   and are stripped. `EMAIL_TO` accepts several addresses separated by commas.
   Settings for Outlook, Fastmail, SES, and a local relay are listed in
   `.env.example`.

### 3. Prove an alert can actually reach you

```powershell
uv run jobwatch notify-test
```

It sends one fabricated posting through every channel you configured and reports
each as delivered, failed with the reason, or not configured. Check Discord and
your inbox — including Spam and Gmail's Promotions tab the first time. Nothing is
written to the database, so run it as often as you like.

Do not skip this. It is much easier to debug a webhook or an app password now
than at 2am when a posting you wanted went nowhere.

### 4. Create the database

```powershell
uv run jobwatch migrate
uv run jobwatch seed
uv run jobwatch status
```

`status` should list roughly 25 companies, each marked `~ awaiting first poll`.

### 5. Confirm the job boards still answer

```powershell
uv run python verify_sources.py config/companies.yaml
```

ATS choices change, board tokens get renamed, Workday tenants get
re-provisioned. This calls every endpoint and tells you which are still good.

### 6. Dry run the whole pipeline

```powershell
uv run jobwatch run --once --dry-run --no-web
```

Fetches every board for real, runs classification, and prints what it *would*
send without sending anything or marking anything alerted. This is also the tool
you tune filters with.

### 7. Start it for real

```powershell
.\deploy\jobwatch.ps1 -Hidden
```

**The first real run sends one digest per source.** Every posting on every board
is recorded and classified, and the matches arrive batched into a single message
rather than one message each — a cold start costs you one notification per
source, not thousands. Alerting individually would rate-limit the webhook and
destroy trust on day one; alerting *nothing*, which is what this used to do,
quietly buries every real internship a newly added company brings with it.

Set `classification.on_seed: silent` in `settings.yaml` for the old behaviour, or
`alert` to send them individually (only sane for a single hand-added company).
If you already have a silent backlog, `jobwatch backfill` recovers it.

```powershell
.\deploy\jobwatch.ps1 -Status      # running? since when? which log?
.\deploy\jobwatch.ps1 -Stop
.\deploy\jobwatch.ps1 -Follow      # start, then tail the log
```

It runs until you stop it or the machine reboots — **there is no auto-start, so
run it again after a reboot**. Logs land in `logs\`. A PID file refuses a second
copy, because two pollers on one database would alert twice for every posting.
Stopping it is always safe: every alert is a committed database row before it is
a message, so a kill mid-send loses nothing and duplicates nothing.

If you later want it to survive logout and reboot, `docs/OPERATIONS.md` has the
Task Scheduler and NSSM recipes. On Linux, use `deploy/jobwatch.service`.

### 8. Use it

Open <http://127.0.0.1:8080>. The screen that earns its keep is **Review**: each
ambiguous title is alerted *and* queued, and your verdict makes every future
posting with that title automatic, forever, at zero cost.

`M` match · `R` reject · `1`–`4` tag a category · `J` skip · `K` back · `U` undo

When something looks wrong, `docs/OPERATIONS.md` is the troubleshooting manual —
sources breaking, notifications not arriving, and what each outbox error means.

## Commands

| Command | What it does |
|---|---|
| `jobwatch run` | Scheduler + web UI. The service. |
| `jobwatch run --dry-run` | Prints what would send instead of sending it |
| `jobwatch run --once` | One scheduler pass, then exit |
| `jobwatch status` | Source health summary |
| `jobwatch notify-test` | Send a sample alert through every configured channel and report what each did |
| `jobwatch test <slug> [--adapter X]` | Fetch one source once, show parsed postings and how each would classify |
| `jobwatch classify "<title>"` | Show how a title classifies, and why |
| `jobwatch discover <careers_url>` | Identify the ATS behind a careers page and verify it live |
| `jobwatch discover-bulk` | Find companies you are not watching yet, from community internship feeds |
| `jobwatch replay <merge_key>` | Re-queue an alert |
| `jobwatch backfill` | Alert on postings a silent cold start swallowed; `--tier`, `--company`, `--match-only`, `--limit`, `--dry-run` |
| `jobwatch export-config` | Write the DB's companies and filters back to YAML for git |
| `jobwatch backup <path>` | Consistent snapshot of the database |
| `jobwatch migrate` / `seed` | Schema and config bootstrap |

### Finding companies you are missing

`discover-bulk` reads community-maintained CS internship trackers, subtracts
everything already in your registry, and probes what is left to find the ATS
behind it. The feeds are used to learn **company names only** — no posting from
an aggregator ever enters the pipeline, because `merge_key` and `dedup_key` are
derived from the source's company, and a multi-employer feed would collapse
distinct employers into one alert.

```bash
# See what is missing, without making a single probe request
jobwatch discover-bulk --term "Summer 2027" --dry-run

# Probe the 40 best candidates and write them to a staging file
jobwatch discover-bulk --term "Summer 2027"
```

Output goes to `config/companies-discovered.yaml`, which nothing loads. Review
it, move the blocks you want into `config/companies.yaml`, then `jobwatch seed`.

It is built to be run on a schedule. Every company it probes is written to
`data/discovery-ledger.json`, so a second run skips them and spends its requests
on companies the feed has not offered before. Runs append to the staging file
rather than overwriting it.

Politeness is the constraint that shaped this command (§13.5). Feed listings
carry the direct ATS URL, so most companies are fingerprinted straight from the
URL with no page fetch; verification stops after one page, so confirming a large
Workday tenant costs one request instead of forty-six; and `--limit` (default
40) caps the work per run. Reported posting counts are therefore a floor —
`>=20` means "at least 20", not "20".

| Flag | Effect |
|---|---|
| `--term "Summer 2027"` | Only companies hiring for that term (repeatable) |
| `--category Software` | Only that feed category (repeatable) |
| `--limit N` | Companies to probe this run (default 40) |
| `--min-listings N` | Skip companies with fewer active listings |
| `--dry-run` | List what would be probed, probe nothing |
| `--retry-failed` | Re-probe companies that failed earlier |
| `--tier hot` | Tier written into the generated blocks |
| `--feed URL` | Use a different feed (repeatable) |

## What ships working

24 verified sources across 25 enabled companies, ~5,400 postings tracked on a
cold start.

| Adapter | Companies |
|---|---|
| `greenhouse` | Stripe, Databricks, Anthropic, Figma, Coinbase, DoorDash, Roblox, Cloudflare, Datadog, Airbnb, Scale AI, Jane Street, Hudson River Trading |
| `workday` | NVIDIA, Salesforce, Adobe, Capital One |
| `ashby` | OpenAI, Perplexity |
| `lever` | Palantir |
| `eightfold` | Netflix, NetApp, Microsoft, Qualcomm, Lam Research |
| `oracle` | American Express, Texas Instruments, onsemi |
| `jibe` | AMD |
| `direct.*` | Amazon, Apple, Uber |
| `html` | Google, Two Sigma, Arm |
| `browser` | Meta, Citadel, IBM |
| `smartrecruiters` | Visa, SanDisk |

**Tesla is the only company that ships disabled.** Its edge bot check answers
403 to everything — plain HTTP, and headless Chromium too — so the page never
renders and there is nothing to intercept.

The other six that used to ship disabled were re-probed on 2026-08-19 and all
six turned out to be reachable, four of them without a browser at all:

| Company | Was | Is now |
|---|---|---|
| Microsoft | browser, dead XHR | `eightfold` — moved onto Eightfold's "pcsx" flavour |
| Qualcomm | `workday`, 422 on every site name | `eightfold` — it left Workday entirely |
| Google | browser, "client-side rendered" | `html` — it is server-rendered, 20 a page |
| Two Sigma | browser, ATS unknown | `html` — Avature, server-rendered, 54 roles |
| Citadel | browser, no selectors | `browser` — Cloudflare screens the UA, so `user_agent` is set |
| Meta | browser, no `items_path` | `browser` — one GraphQL response carries all 378 |

Two of those needed adapter work rather than just config. `eightfold` now takes a
`path` and understands the pcsx dialect — a `data` envelope, camelCase fields,
and a page size that ignores `num`, so pagination follows `count` instead of
stopping at the first short page. `html` now takes `page_param`/`page_start`/
`page_step`, which is what walks Avature's `jobOffset` and Google's `page`.

## Semiconductors

Nine more were added on 2026-08-19 alongside the NVIDIA/Intel/Micron/Broadcom/
Marvell/Analog Devices/Applied Materials/KLA/Qualcomm group already in the
registry. All `cold`; hit counts are what each returned when probed:

| Company | Adapter | Hits |
|---|---|---|
| NXP | `workday` (`nxp.wd3`, site `careers`) | 338 |
| onsemi | `oracle` (pod `hctz.fa.us2`, site `CX_1001`) | 310 |
| Arm | `html` (Radancy) | 325 |
| SanDisk | `smartrecruiters` (`Sandisk`) | 292 |
| Texas Instruments | `oracle` (pod `edbz.fa.us2`, site `CX`) | 254 |
| Astera Labs | `greenhouse` (`asteralabs`) | 170 |
| Cadence | `workday` (`cadence.wd1`, `External_Careers`) | 155 |
| Lam Research | `eightfold` (pcsx) | 21 |
| AMD | `jibe` | 10 |

`jibe` is the one new adapter. Jibe is a careers-site front end rather than an
ATS — each record names the ATS behind it in `ats_code`, `icims` for AMD — so it
is written as a platform adapter and is the registry's first reach into iCIMS.
Two traps are baked into it: `limit` caps at **100** and asking for 200 returns
an empty list rather than an error, and `totalCount` is the real total despite
looking like a page size on a search that happens to have ten hits.

Three quirks worth knowing about the rest. TI's site number is a bare `CX`, not
`CX_<n>`. Astera Labs' Greenhouse token is `asteralabs`, not `astera-labs`.
And **Arm's keyword search does not filter** — `/search-jobs/intern`, `?k=` and
`?keyword=` all return the same unfiltered board — so it pulls all 9 pages and
leaves the filtering to the classifier.

Identified but not added, for want of an adapter: **Qorvo** (SuccessFactors),
**Synopsys** (Radancy + Avature), **Rambus** and **Arm's own iCIMS portal**
(raw iCIMS, client-rendered). Microchip answers 403 to plain HTTP and to
Chromium alike; GlobalFoundries, Skyworks, Seagate, ASML, Infineon, Ambarella
and Credo showed no ATS fingerprint on their careers pages at all.

`oracle` covers Oracle Recruiting Cloud, identified by `/sites/CX_<n>` in a
careers URL. Its API is public and unauthenticated with a real `PostedDate`, but
the pod that serves it is an opaque host that discovery cannot resolve — request
`/hcmRestApi/resources/latest/recruitingCEJobRequisitions` on the company domain
and read the `oraclecloud.com` host out of the 302. `jobwatch discover` reports
the platform and the site number, and leaves `pod` for you to paste in.

Notable registry corrections found by actually calling the endpoints: DoorDash's
board token is `doordashusa`, Hudson River Trading's is `hrttalentcommunity`,
Jane Street is on Greenhouse rather than needing a bespoke adapter, and Netflix
is on Eightfold rather than Lever.

## Configuration

- `config/companies.yaml` — the registry. Seeded into SQLite on first run; the
  UI owns it afterwards. Seeding is additive and never resets a source's
  `seeded` flag.
- `config/filters.yaml` — seed classification rules, same lifecycle.
- `config/settings.yaml` — intervals, timeouts, bind address.
- `.env` (or `/etc/jobwatch.env`, mode 600) — secrets. Never committed.

Per-source `min_interval_seconds` puts a floor under a source's poll interval
regardless of tier. Use it for anything that costs many requests per poll — a
full Workday scan of NVIDIA is ~46 requests, which has no business running every
60 seconds from a residential IP.

## Notifications

Setup steps are above; this is what the machinery guarantees.

Two channels, both optional, both independent. Set either or both in `.env`:

| | Set | Notes |
|---|---|---|
| Discord | `DISCORD_WEBHOOK_URL` | ~1 minute: Server Settings → Integrations → Webhooks. Arrives in seconds. |
| Email | `SMTP_HOST` + `EMAIL_TO` (plus `SMTP_USERNAME`/`SMTP_PASSWORD`) | Gmail needs 2FA and a 16-char App Password — the account password is rejected. |

An alert is delivered to **every** configured channel and is only marked `sent`
once all of them have taken it. Which channels have already accepted a row is
recorded on the row, so a retry after a partial failure re-sends only to the one
that failed — a flaky mail server never means a duplicate Discord ping.

A channel with no credentials is skipped, not failed. With *no* channel
configured, alerts still queue durably and the outbox row records why nothing
went out.

`jobwatch notify-test` sends a fabricated posting through each channel without
touching the database — no queueing, nothing marked alerted.

## Web UI

Six screens: feed, review queue, filter workbench, companies, health, outbox.
Served from the same process as the poller, supervised so that a route
exception can never stop the scheduler.

**Never expose it to the internet.** It has no login by design — network-level
access control is the authentication. It binds to `127.0.0.1` by default; in
production bind it to the Tailscale interface and allow the port on
`tailscale0` only. `docs/OPERATIONS.md` has the exact `ufw` rules.

The review queue is keyboard-first: `M` match, `R` reject, `1`–`4` tag, `J`
skip, `K` back, `U` undo. Each verdict is permanent and global — every future
posting with that normalized title classifies instantly, forever, at zero cost.

## Tests

```bash
uv run pytest        # 466 tests, no network
uv run ruff check src tests
```

Every adapter is tested against a captured real response in `tests/fixtures/`.
Re-capture them with `uv run python tests/capture_fixtures.py`.

The highest-value tests are in `tests/test_pipeline.py`: cold-start silence,
cross-source merge gating, and a property test asserting that any ordering of
polls across any number of sources containing a given requisition produces
exactly one alert.

## Layout

```
config/           companies.yaml, filters.yaml, settings.yaml
src/jobwatch/
  normalize.py    title normalization + the two identity keys — the correctness core
  pipeline.py     fetch → persist → seed-or-classify → gate on merge_key → enqueue
  scheduler.py    tier intervals, backoff, due-source selection
  adapters/       greenhouse, lever, ashby, workday, smartrecruiters, eightfold,
                  oracle, jibe, phenom, workable, rippling, bamboohr,
                  html, browser, direct/{amazon,apple,uber}
  classify/       regex rules + the human verdict cache
  notify/         durable outbox in front of the channels: discord, email
  web/            FastAPI + Jinja2 + vendored HTMX, no build step
deploy/           jobwatch.ps1 (Windows start/stop), systemd unit, backup script
docs/             OPERATIONS.md (running the service), ADAPTERS.md (platform quirks)
CLAUDE.md         orientation for Claude Code sessions
```
