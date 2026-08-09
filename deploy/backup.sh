#!/usr/bin/env bash
# Nightly snapshot of the jobwatch database.
#
#   sudo cp deploy/backup.sh /usr/local/bin/jobwatch-backup
#   sudo chmod +x /usr/local/bin/jobwatch-backup
#   sudo crontab -e
#     17 4 * * * /usr/local/bin/jobwatch-backup >> /var/log/jobwatch-backup.log 2>&1
#
# Uses the SQLite backup API rather than `cp`, so it is consistent against a
# live WAL database. The exit code is the detection signal in the §17 matrix:
# have cron mail you on failure, or wire it to a healthchecks.io ping.
#
# What is actually irreplaceable here is `jobs`, `alerted_merges` and
# `title_verdicts`. Lose those and the next start re-seeds silently, which is
# safe — but every human verdict you ever recorded is gone.

set -euo pipefail

DB="${JOBWATCH_DB:-/var/lib/jobwatch/jobs.db}"
DEST="${JOBWATCH_BACKUP_DIR:-/var/lib/jobwatch/backups}"
KEEP_DAYS="${JOBWATCH_BACKUP_KEEP_DAYS:-30}"
VENV="${JOBWATCH_VENV:-/opt/jobwatch/.venv}"

if [[ ! -f "$DB" ]]; then
  echo "jobwatch-backup: no database at $DB" >&2
  exit 1
fi

mkdir -p "$DEST"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$DEST/jobs-$stamp.db"

"$VENV/bin/jobwatch" --db "$DB" backup "$target"
gzip -9 "$target"

# Integrity check the snapshot before trusting it.
if ! gzip -t "$target.gz"; then
  echo "jobwatch-backup: $target.gz failed its gzip check" >&2
  exit 1
fi

find "$DEST" -name 'jobs-*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "jobwatch-backup: wrote $target.gz ($(du -h "$target.gz" | cut -f1))"

# Off-machine copy. Uncomment one; a backup on the same disk as the database
# is not a backup.
# rclone copy "$target.gz" b2:my-bucket/jobwatch/
# rsync -a "$target.gz" user@other-host:/backups/jobwatch/
