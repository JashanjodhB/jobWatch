# jobwatch — operations

Everything you need when the thing is running and something is wrong.

---

## First run on the server

```bash
sudo useradd --system --home /opt/jobwatch --shell /usr/sbin/nologin jobwatch
sudo mkdir -p /opt/jobwatch /var/lib/jobwatch
sudo chown jobwatch:jobwatch /var/lib/jobwatch

git clone <your-repo> /opt/jobwatch && cd /opt/jobwatch
uv sync

sudo cp .env.example /etc/jobwatch.env
sudo chmod 600 /etc/jobwatch.env
sudo nano /etc/jobwatch.env            # webhook and/or SMTP settings, set JOBWATCH_DB

sudo -u jobwatch .venv/bin/jobwatch migrate
sudo -u jobwatch .venv/bin/jobwatch seed
sudo -u jobwatch .venv/bin/jobwatch status
```

Before the first *real* poll, confirm the registry is still accurate. ATS
choices change, board tokens get renamed, Workday tenants get re-provisioned:

```bash
uv run python verify_sources.py config/companies.yaml --verbose
```

Then a dry run, which exercises the whole pipeline and prints what it would
send without sending anything:

```bash
uv run jobwatch run --once --dry-run --no-web
```

**The first real run seeds silently.** Every posting currently on every board is
recorded and marked already-known, and nothing is sent. This takes a minute or
two and produces `alerted: 0`. That is correct — alerts begin with the first
genuinely new posting. If a first run ever sends anything, stop and investigate:
something is wrong with seeding, and a webhook that receives 3,000 messages gets
rate-limited and the system loses your trust on day one (§13.1).

```bash
sudo cp deploy/jobwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobwatch
journalctl -u jobwatch -f
```

---

## Running it on Windows

No systemd, no service account. `deploy/jobwatch.ps1` starts the same process in
the background from your own session:

```powershell
.\deploy\jobwatch.ps1 -Hidden           # detached, no console window
.\deploy\jobwatch.ps1 -Hidden -DryRun   # same, but prints alerts instead of sending
.\deploy\jobwatch.ps1 -Status
.\deploy\jobwatch.ps1 -Stop
.\deploy\jobwatch.ps1 -Follow           # start, then tail the log
```

| | |
|---|---|
| Logs | `logs\jobwatch-<date>-<time>.log`, one file per start, plus `jobwatch-stderr.log` |
| PID file | `logs\jobwatch.pid`, holding `pid\|start-time\|log-path` |
| Survives logout | No. It is a child of your session. |
| Survives reboot | No. Start it again. |
| Second copy | Refused. Two pollers on one database alert twice for every posting. |

The PID file records the process start time as well as the number, because
Windows reuses PIDs and a stale file must never be able to kill an unrelated
process. If the file is stale, `-Status` reports not running and clears it.

`-Stop` force-kills. That is safe by design: every alert is a committed outbox
row before it is a message, so a kill mid-send loses nothing and duplicates
nothing, and SQLite is in WAL mode.

If you later want it to survive logout and reboot, the two options are Task
Scheduler (trigger *At log on*, action `powershell.exe -NoProfile -File
<repo>\deploy\jobwatch.ps1 -Hidden`) or a real service via NSSM. Neither is
configured here.

---

## Network access

**The UI has no login. Network-level access control is the authentication.**

```bash
# Bind uvicorn to the Tailscale address
tailscale ip -4                        # e.g. 100.x.y.z
sudo nano /opt/jobwatch/config/settings.yaml
#   web:
#     bind_host: "100.x.y.z"
#     public_base_url: "http://100.x.y.z:8080"

sudo ufw default deny incoming
sudo ufw allow in on tailscale0 to any port 8080 proto tcp
sudo ufw allow ssh
sudo ufw enable
sudo ufw status verbose                 # 8080 must appear ONLY on tailscale0
```

Verify from a machine on the LAN but not on the tailnet:

```bash
curl -m 5 http://<lan-ip>:8080/          # must time out or refuse
curl -m 5 http://100.x.y.z:8080/healthz  # must return "ok" from the tailnet
```

Never port-forward 8080. Never put it behind a public reverse proxy.

---

## Reading the health screen

| Column | What it means | What to do |
|---|---|---|
| **Latest** vs **7d baseline** | today's posting count against the rolling median | a drop to 0 with a healthy baseline is the silent-breakage signature — the endpoint still answers, the parse broke |
| **Fails** | consecutive failures | 5+ means the source has flipped to its fallback |
| **State: fallback** | running the fallback adapter | fine short-term; over 7 days means the primary is gone for good — update the registry |
| **State: awaiting seed** | never polled | its first poll will alert nothing, by design |

The daily drift check also posts alarms to Discord as red embeds. They report
once per occurrence, not once per day, and re-arm if the condition clears and
returns.

---

## When a source breaks

```bash
jobwatch test stripe                 # fetch once, print what came back
jobwatch test stripe --raw           # full posting records as JSON
jobwatch classify "Software Engineer Intern, Summer 2027"
```

`jobwatch test` shows the parsed postings, their normalized titles, and how each
would classify — without persisting anything. It distinguishes the three cases
that look identical from the outside:

* **HTTP 404** — the board token, tenant, or site name is wrong. Open the
  careers page, DevTools → Network → Fetch/XHR, search for a job, and read the
  real handle off the request URL.
* **`AdapterError: expected a list under 'jobs'`** — the response shape changed.
  Re-capture the fixture (`uv run python tests/capture_fixtures.py greenhouse`),
  run the tests, and fix the mapping.
* **0 postings, no error** — usually a wrong handle rather than an empty board.
  Drift detection will alarm on this within a day.

To find a replacement source:

```bash
jobwatch discover https://www.company.com/careers
```

It identifies the platform, verifies the guess against the live endpoint, and
prints a registry block ready to paste. The Companies screen has the same thing
with a Save button.

---

## Adding companies

Use the Companies screen, or edit `config/companies.yaml` and re-run
`jobwatch seed`. Seeding is **additive**: it inserts what is missing and leaves
existing rows alone, because the UI owns them once they exist. `--force` also
overwrites descriptive fields from YAML — but never scheduling state and never
the `seeded` flag, since resetting that would re-alert an entire board.

A newly added source seeds silently on its first poll, exactly like a first run.

To push UI changes back to git:

```bash
jobwatch export-config      # writes config/*.export.yaml
```

Review the diff, move them over the originals, commit.

### Keeping the request rate sane

A source's poll interval comes from its company's tier, but
`min_interval_seconds` in the source config puts a floor under it. Use it for
anything that costs many requests per poll:

```yaml
config:
  host: https://nvidia.wd5.myworkdayjobs.com
  tenant: nvidia
  site: NVIDIAExternalCareerSite
  search_text: intern
  min_interval_seconds: 300     # ~46 requests per scan; 60s would be 46/min
```

Requests come from a residential IP that cannot be rotated. Getting it blocked
by an ATS provider is effectively unrecoverable (§13.5).

---

## Tuning what gets alerted

The review queue is the mechanism, not the filters. Every ambiguous title is
alerted *and* queued; your verdict makes the next occurrence automatic, forever.
Clearing the queue takes seconds:

`M` match · `R` reject · `1`–`4` tag a category · `J` skip · `K` back · `U` undo

Reach for the filter workbench only when a *pattern* is wrong rather than a
title. The live preview re-classifies the last 90 days both ways and shows
exactly which postings would flip, so you can see before saving that a new
`exclude_any` would have suppressed four internships you wanted.

Saving any rule purges every rules-derived cache entry (human verdicts survive),
which is what makes the edit take effect on titles the old rules had seen.

---

## Notifications not arriving

```bash
jobwatch notify-test                 # every configured channel, one sample alert
jobwatch notify-test --channel email # just the one you are debugging
```

It reports each channel as delivered, failed with the reason, or not configured,
and it does not touch the database — nothing is queued and nothing is marked
alerted, so it is safe to run as often as you like.

Then:

1. Open the **Outbox** screen. It lists the channels and whether each has
   credentials. Queued rows mean the worker is behind; failed rows carry the
   error, prefixed with the channel that produced it.
2. A row can be queued *and* partly delivered — the `→ discord` note under its
   kind means Discord took it and email has not yet. A retry only re-sends to
   the channel that has not taken it.
3. `jobwatch replay <merge_key>` re-queues from the shell. Replay clears the
   delivery record, so it goes out to everything again.

Nothing sends directly from the pipeline — every alert is a committed row first,
so killing the process mid-send loses nothing and duplicates nothing.

### Discord

`failed` with HTTP 401/403/404 means the webhook URL is wrong or was deleted.
Those fail immediately rather than retrying five times into a wall. Fix
`.env` (or `/etc/jobwatch.env`), restart, then hit **Replay**.

### Email

| Error in the outbox row | Cause | Fix |
|---|---|---|
| `SMTP rejected the credentials (535)` | Gmail account password used, or 2FA not enabled | Enable 2-Step Verification, generate an App Password at myaccount.google.com/apppasswords, use those 16 characters |
| `ConnectionRefusedError` / timeouts | wrong port for the security mode | starttls→587, ssl→465. Do not use 465 with `SMTP_SECURITY=starttls` |
| `SMTPNotSupportedError` | server wants implicit TLS, config says STARTTLS (or vice versa) | flip `SMTP_SECURITY` between `starttls` and `ssl` |
| `the server refused the addresses` | `EMAIL_FROM` is not an address the server will send as | set `EMAIL_FROM` to the mailbox you authenticated as |
| nothing arrives, no error | delivered but filed away | check Spam and, on Gmail, the Promotions tab; filter on the `[jobwatch]` subject prefix and star it |

Credential and recipient failures are marked permanent and stop retrying
immediately; connection failures and 4xx deferrals retry with backoff up to
`notifications.max_attempts`.

Email volume is one message per hot-tier posting and one digest per interval for
warm and cold. During an August surge that is tens of messages a day, well inside
Gmail's limits — but it is worth a filter that files them under a label.

---

## Backup and restore

```bash
sudo cp deploy/backup.sh /usr/local/bin/jobwatch-backup
sudo chmod +x /usr/local/bin/jobwatch-backup
sudo crontab -e
#   17 4 * * * /usr/local/bin/jobwatch-backup >> /var/log/jobwatch-backup.log 2>&1
```

Restore:

```bash
sudo systemctl stop jobwatch
sudo -u jobwatch bash -c 'gunzip -c /var/lib/jobwatch/backups/jobs-<stamp>.db.gz > /var/lib/jobwatch/jobs.db'
sudo systemctl start jobwatch
```

**A restore never causes an alert storm.** Any source whose rows are missing
re-seeds silently on its next poll. What you actually lose by not having a
backup is `title_verdicts` — every human classification decision you ever made.
That is the table worth protecting.

---

## Optional: the browser fallback

Six companies in the shipped registry (Google, Meta, Microsoft, Tesla, Citadel,
Two Sigma) have no usable unauthenticated JSON endpoint and ship **disabled**.
They need Playwright:

```bash
uv sync --extra browser
uv run playwright install chromium
```

Then enable them on the Companies screen. Two Sigma additionally needs the real
XHR substring in `xhr_contains`, read off DevTools on its careers page.

Without the extra installed the service starts normally and skips those sources
with a log line — a missing extra degrades one adapter, it never crashes the
process.

---

## Cost

Zero, forever. Electricity only. No API keys, no metered services, no free tier
that can lapse into billing. If you ever find yourself signing up for something
to add a source, stop — scrape the public page or drop the company (§2).
