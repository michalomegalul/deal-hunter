#!/bin/bash
# pg_dump of the production database: daily (hunter-backup.timer) and before every migration (deploy.sh).
# Restore: docker compose exec -T db pg_restore -U hunter -d hunter --clean --if-exists < backups/<file>.dump
set -euo pipefail
cd /opt/deal-hunter
mkdir -p backups
out="backups/hunter-$(date -u +%Y%m%dT%H%MZ)-${1:-daily}.dump"

# A crash loop that fills the disk with core dumps (see the Nov 2026 outage) must not also let a
# backup run wedge itself into the last bit of free space. Require at least 500 MB free AND at
# least 3x the size of the last dump (dumps tend to grow, not shrink), whichever is bigger.
min_free_mb=500
last_dump=$(find backups -maxdepth 1 -name 'hunter-*.dump' -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn | head -1 | cut -d' ' -f2- || true)
required_kb=$((min_free_mb * 1024))
if [[ -n "${last_dump:-}" ]]; then
  last_kb=$(du -k "$last_dump" | cut -f1)
  triple_kb=$((last_kb * 3))
  if (( triple_kb > required_kb )); then
    required_kb=$triple_kb
  fi
fi
avail_kb=$(df -Pk backups | tail -1 | awk '{print $4}')
if (( avail_kb < required_kb )); then
  echo "Backup aborted: only ${avail_kb}KB free on $(pwd), need ~${required_kb}KB" \
       "(500MB floor or 3x last dump). Free disk space before the next attempt." >&2
  exit 1
fi

# Remove a partial dump if pg_dump or the move fails partway through, so it never accumulates.
trap 'rm -f -- "$out.tmp"' EXIT

docker compose exec -T db pg_dump -U hunter -d hunter -Fc > "$out.tmp"
mv "$out.tmp" "$out"
find backups -name 'hunter-*.dump' -mtime +"${HUNTER_BACKUP_KEEP_DAYS:-14}" -delete
echo "Backup: $out ($(du -h "$out" | cut -f1))"
