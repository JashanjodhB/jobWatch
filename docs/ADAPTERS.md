# Adapters — platform quirks

What each adapter needs, and the way each platform breaks *silently*. Every
entry here was paid for once by probing a live endpoint; the point of the file
is that nobody pays twice.

The universal rule first: **a wrong config usually returns HTTP 200 and an empty
list, not an error.** Always check the posting count against company size before
believing a board.

## Config keys

| Adapter | Required | Optional |
|---|---|---|
| `greenhouse` | `board_token` | `embed` |
| `lever` | `company` | |
| `ashby` | `board_name` | `include_unlisted` |
| `smartrecruiters` | `company` | `q`, `limit` |
| `workday` | `host`, `tenant`, `site` | `limit`, `locale`, `search_text`, `applied_facets` |
| `eightfold` | `base`, `domain` | `path`, `num`, `query`, `sort_by` |
| `oracle` | `pod`, `site_number` | `site_url`, `keyword`, `limit`, `sort_by` |
| `jibe` | `base` | `limit`, `query`, `sort_by` |
| `phenom` | `base` | `query`, `size`, `country`, `lang`, `site_type` |
| `workable` | `account` | |
| `rippling` | `board` | |
| `bamboohr` | `tenant` | |
| `html` | `url`, `job_selector` | `base_url`, `title_selector`, `link_selector`, `location_selector`, `max_pages`, `page_param`, `page_start`, `page_step` |
| `browser` | `url`, `xhr_contains` | `base_url`, `items_path`, `field_map`, `job_selector`, `frame_contains`, `wait_ms`, `user_agent` |
| `direct.amazon` | `base` | `query`, `limit` |
| `direct.apple` | `base` | `query`, `locale`, `filters` |
| `direct.uber` | `base` | `query`, `limit`, `params` |

## SmartRecruiters — `q` is the only way to bound a big board

`company` is the whole required config, and unfiltered it pulls the entire
board: Eurofins is 2533 postings and Bosch 4791, both far past the 20-page
(2000 posting) guard, and both failed ~90 consecutive polls until 2026-09-01.

`q` is full-text over the whole posting rather than the title, which is what
makes it safe to use: `q=intern` cuts Eurofins to 633 (7 pages) and Bosch to
1259 (13) while still returning postings whose title only ever says "Intern".
Do not tighten it to `q=internship` — that takes Eurofins to 22 and drops them.

## Ashby — one untitled posting used to cost the whole board

Boards serve rows with an empty `title` (phonely has carried one since
2026-08-25). The adapter drops such a row and keeps the rest, and raises only
when *every* row is untitled, which is what a renamed field looks like. Same
rule Workday uses for a missing `externalPath`.

## Workday

**The status code tells you which half of the config is wrong**, and this is the
single most useful fact in this file:

- `404` — the tenant is real, the **site** segment is wrong. Brute-forcing a
  site wordlist is worth it.
- `422` — the **tenant** does not exist. A wordlist is wasted; find the real one.

Any `*.wdN.myworkdayjobs.com` subdomain resolves, so DNS success proves nothing.

**Workday has a second domain, `myworkdaysite.com`**, which moves the tenant out
of the hostname and into the path:

    https://wd5.myworkdaysite.com/[en-US/]recruiting/{tenant}/{site}

Same CXS API, same adapter — only the config is read differently. Wells Fargo,
Microchip, Sysco and Devon Energy all live here, and before discovery knew the
shape they fell through to a low-confidence Greenhouse guess and 404'd.

A full scan of a large tenant is ~46 requests. Put `min_interval_seconds` in the
source config so the tier interval cannot run that every 60s from a residential
IP (§13.5). `externalPath` comes back relative — `absolute_url()` handles it.

**`total` saturates at 2000, and `search_text` is not a filter you can trust on
a big tenant.** Both facts bit Hitachi and Leidos, which failed ~90 consecutive
polls each before 2026-09-01:

- Workday full-text matches `internal` and `international`, so `search_text:
  intern` returns essentially the whole board.
- `total` then reports exactly `2000` while `offset: 2100` still returns rows —
  the result set has **no knowable end**, so the `MAX_PAGES` guard is right to
  fire and no page budget makes the scan complete.
- `limit` is capped at 20 on both those tenants (25 → HTTP 400), so a larger
  page size is not a way out either. `SAFE_LIMIT` exists for this.

**Reach for `applied_facets` instead.** A `searchText: ""` response carries a
`facets` array listing every facet parameter, its values and their counts; the
employer's own intern category is in there, usually under `jobFamilyGroup` or
`workerSubType`. That gives a set that is bounded *and* complete — Hitachi's
`jobFamilyGroup=Intern_Group` is 156 of 156 in 8 pages, Leidos'
`jobFamilyGroup=Internship` is 20 of 20 in one — where no text query is either.

Two cautions. Facet ids are opaque per-tenant GUIDs, so re-read them from the
`facets` array rather than copying between tenants; and a facet id that stops
matching returns an **empty board, not an error**, so watch the posting count
for a collapse. Values within one facet parameter are OR'd, different parameters
are AND'd, and `UNIQUE(company_slug, adapter)` means you get one Workday source
per company — you cannot union two facet queries by adding a second row.

## Eightfold — two dialects

Some tenants serve `/api/apply/v2/jobs`, others `/api/pcsx/search` (Microsoft,
Qualcomm). Select with the `path` key. The pcsx dialect differs three ways:
results wrapped in a `data` envelope, camelCase fields (`displayJobId`,
`positionUrl`, `postedTs`), and **`num` is ignored — always 10 per page**, so a
short page is *not* end-of-results. Pagination must follow `count`.

## Oracle Recruiting Cloud

Identify a tenant by `/sites/CX_<n>` in its careers URL.

**Check whether the careers URL is already an Oracle host before doing any
work.** Community feeds hand these over constantly
(`egup.fa.us2.oraclecloud.com`, `fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com`),
and when the host ends in `oraclecloud.com` that host *is* the pod — there is no
302 to follow and nothing to resolve. `discover.py` handles this case
automatically now; it previously stubbed the pod out unconditionally, which
reported 32 verifiable tenants as unresolvable.

**Only when the URL is on the company's own domain is the pod undiscoverable.**
`careers.<company>.com` does 302 to an Oracle host, but the redirect lands on
`/hcmUI/CandidateExperience/errors/404`, never the API. Resolve it once by hand:
request `/hcmRestApi/resources/latest/recruitingCEJobRequisitions` on the company
domain and read the `oraclecloud.com` host out of the 302. `jobwatch discover`
reports platform and site number and leaves `pod` blank for these.

`expand=requisitionList.secondaryLocations` is **required** — without it
`TotalJobsCount` is still correct but `requisitionList` is omitted entirely,
which is indistinguishable from an empty board. `limit` is honoured to 200+, so
a whole board is one or two requests.

**`keyword` is much weaker than it looks — check what it actually removes.**
On Marriott's pod `keyword=intern` returned 12828 of the board's 12872 reqs, so
the adapter walked 8000 requisitions over 113–314s and still tripped the 40-page
guard; `keyword=internship` returns 215 and completes in two. Compare the
filtered count against the unfiltered one *before* seeding a source. A count
that comes back as ">= some large number" is the warning, not the confirmation.

Known pods: amex `egug.fa.us2`, oracle `eeho.fa.us2`, honeywell
`ibqbjb.fa.ocs`, marriott `ejwl.fa.us2`. jpmorgan-chase, ford and fortinet
expose the CX_ number but not the pod.

## Raw iCIMS — WAF-gated, and the jobs are in an iframe

`careers-<tenant>.icims.com`. Distinct from the Jibe front end below, and the
harder of the two.

**Every path answers 405, including invented ones.** That is not routing, it is
an AWS WAF human-verification challenge served through CloudFront — the body is
`Human Verification` plus an `awsWafCookieDomainList` script. No plain-HTTP
adapter can get past it, so `/api/jobs` returning nothing here means nothing.

Headless Chromium clears the challenge without any special handling. But the
listings render in an `in_iframe=1` **child frame**, so `page.content()` returns
only the wrapper, and requesting the iframe URL directly does not help: iCIMS
strips the parameter and redirects back to the wrapper, which re-embeds it. The
`browser` adapter's `frame_contains` exists for exactly this.

```yaml
adapter: browser
config:
  url: https://careers-<tenant>.icims.com/jobs/search?ss=1&searchKeyword=intern
  frame_contains: "in_iframe=1"
  base_url: https://careers-<tenant>.icims.com
  job_selector: "li.iCIMS_JobCardItem"
  title_selector: "div.title h3"
  link_selector: "div.title a"
  wait_ms: 4000
```

**Do not add a `location_selector`.** The right-hand header cell is the location
on some tenants (Daktronics) and the posting date on others (Peraton, GDMS). A
date parsed as a location is worse than no location: `location_require` rejects
a posting that *has* locations and matches none of them, so it would silently
drop genuine US roles. With the field absent the posting is never rejected on
location.

## Jibe (iCIMS front end)

    GET {base}/api/jobs?keywords=intern&page=1&limit=100&sortBy=relevance

Jibe is a careers-site front end, not an ATS — each record names the real one in
`ats_code`. `apply_url` comes back absolute and already points at the ATS.

- `limit` caps at **100**. `limit=200` returns HTTP 200 with an empty `jobs`
  list, not an error, so asking for more loses the entire page.
- `page` is **1-based**; page 0 re-serves page 1 and double-counts.
- `totalCount` really is the total, not a page size. Do not "fix" pagination on
  that misreading.

**Not every iCIMS site is a Jibe site.** Raw iCIMS portals
(`earlycareers-arm.icims.com`, `careers-rambus.icims.com`) have no `/api/jobs`,
render client-side, and list nothing useful in HTML. Test for `/api/jobs`, not
for the string `icims.com`.

## Phenom People

`POST {base}/widgets`. **The trap is `ddoKey`.** The page's own first call uses
`eagerLoadRefineSearchSession`, which answers
`{"refineSearch":{"tokenAvailable":...}}` with **no postings** — capture that and
the board looks empty. Only `ddoKey: "refineSearch"` returns jobs, and it needs
no token or session.

`Origin`/`Referer` must be the tenant's own host or some tenants 403. `size` is
honoured past 100, `from` is an absolute offset, `applyUrl` is absolute and
points at the ATS behind Phenom (often Workday).

Live on ge-healthcare, chewy, cvs-health, genentech, lowes, mitre. Cisco, Yelp
and UPS expose Phenom hosts carrying 7, 3 and 1 featured roles while their real
boards are elsewhere — check the count.

## Radancy / TalentBrew — use `html`, no adapter

Server-rendered; a Playwright sniff of four tenants found zero job JSON.
`GET {host}/search-jobs?k=intern`, paginate `?p=N`, 15 rows a page.

**The keyword must be in the query string.** `/search-jobs/intern?p=2` silently
drops the filter and pages the whole unfiltered board — l3harris went 110 → 1930
that way. Every tenant exposes `data-total-results` on
`<section id="search-results">`, which is how to verify a config.

Two skins: classic `li > a > h2` + `span.job-location`, and a newer one
(McKesson) using `a.search-results__job-title-link` +
`span.search-results__job-location`.

Live on l3harris, mckesson, charles-schwab, intuit, unitedhealth, blackrock,
comcast.

## Workable, Rippling, BambooHR — one request each

All three are single unauthenticated GETs with no pagination. Each has one trap:

- **Workable** — `GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true`.
  **`details=true` is what produces the `jobs` array**; without it the board
  looks empty rather than erroring. The account handle spells dots out, so
  pony.ai is `pony-dot-ai`. Link to `url`, not `application_url` — the latter is
  the bare form. A newer `POST /api/v3/accounts/{account}/jobs` pages through
  `results`/`nextPage`, but v1 returns the whole board in one call.
- **Rippling** — `GET https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs`.
  The response is a **bare top-level array**, not an object with a `jobs` key.
  Title is `name`, and `department`/`workLocation` are `{id, label}` objects
  rather than strings. No posting date is exposed at all.
- **BambooHR** — `GET https://{tenant}.bamboohr.com/careers/list`, postings under
  `result`, total under `meta.totalCount`. **The payload carries no URL**; the
  apply link must be assembled as `https://{tenant}.bamboohr.com/careers/{id}`.
  Title is `jobOpeningName`. Read `location`, not `atsLocation` — the latter is
  usually all-nulls.

**JazzHR (`{tenant}.applytojob.com`) has no usable endpoint.** `/apply/jobs.json`
is 404 and `/apply/jobs.rss` is 410 Gone. 22 companies sit here; they would need
the `html` adapter per tenant.

## Avature — invisible to fingerprinting

The markup carries no ATS token; the platform only shows up in the page's own
asset URLs. Server-rendered, and `jobRecordsPerPage` is accepted then ignored,
so `jobOffset` is the only way through the list — that is what `html`'s
`page_param`/`page_start`/`page_step` exist for.

IBM is Avature behind a JS challenge: plain HTTP gets 202 and a zero-byte body,
so it runs on `browser` and needs `uv sync --extra browser` plus
`playwright install chromium`.

## Finding the ATS for a new company

Ranked by yield per request:

1. **Guess the handle, do not fetch the page.** Probing greenhouse / lever /
   ashby / smartrecruiters with the slug and its dash-stripped variants resolved
   50 of 145 companies on its own, one request each. Do this first.
2. **Fingerprint the careers page with a browser User-Agent.** The honest
   `jobwatch/2.0` UA is 403'd by most WAFs, which makes live boards look dead.
3. **Then try the platforms `discover.py` supports but does not fingerprint** —
   Jibe, Eightfold pcsx, Workday site names. `_URL_RULES` now reads greenhouse,
   lever, ashby, smartrecruiters, workable, rippling and bamboohr straight off
   the hostname, and there are dedicated builders for Workday (both domains),
   Oracle and raw iCIMS. `_PAGE_RULES` — the markup pass — still covers only
   greenhouse/lever/ashby/smartrecruiters, so a platform that is invisible in
   the URL still falls through to a low-confidence `html` guess.
4. **Platform named but no endpoint found?** Load the careers page under
   Playwright with `page.on("response")` filtered to `resource_type in
   ("xhr","fetch")`. Zero results is itself the answer: server-rendered, so
   reach for `html`.

**Verifying is not being right.** Two failure modes that pass a "did it answer"
check: *stub* boards (bytedance 2 postings, caci 14, when both have thousands
elsewhere) and *wrong-entity* boards (sonyinteractive.com fingerprints to
greenhouse `teamlfg`, one PlayStation studio, not SIE). Eyeball counts against
company size and read the apply URLs.

**A stale `notes:` on a disabled company is not evidence.** On 2026-08-19 six of
seven disabled companies turned out reachable — Google was marked "client-side
rendered" and is server-rendered; Citadel's "a plain GET never reaches the page"
was a User-Agent screen; Qualcomm had left Workday for Eightfold entirely.
Re-probe before believing a note.

**Tesla is the one genuinely blocked source.** Its edge answers 403 to plain HTTP
*and* to headless Chromium. Do not retry without a headed, non-automated profile.

## Still unreached

- **JazzHR** (~22 companies) — both known endpoints are dead, see above.
- **TikTok** — the single biggest company in the feed at ~200 active listings.
  `lifeattiktok.com` is a Next.js app that under headless Chromium loads
  correctly (107KB, right title) but fires **zero** job requests: only the
  document, two chunk scripts and an image. `/api/v1/search/job/posts` answers
  405 to POST and 200 with a zero-byte `text/plain` body to GET. Same signature
  as Tesla — it needs a headed, non-automated profile. The `bytedance` registry
  entry is a 2-posting SmartRecruiters stub and is not a substitute.
- **Taleo** (unitedhealth), **SuccessFactors** (sap, qorvo), **gr8people**
  (electronic-arts), **Jobvite**, **Paylocity**, **Breezy**, **Avature**, and
  **YC WorkAtAStartup** — all small, one or two companies each.
- **~136 companies on their own domains**, each needing individual
  fingerprinting. This is the long tail and the least value per hour.
