#!/bin/bash
# pg_dump of the production database: daily (hunter-backup.timer) and before every migration (deploy.sh).
# Restore: docker compose exec -T db pg_restore -U hunter -d hunter --clean --if-exists < backups/<file>.dump
set -euo pipefail
cd /opt/deal-hunter
mkdir -p backups
out="backups/hunter-$(date -u +%Y%m%dT%H%MZ)-${1:-daily}.dump"
docker compose exec -T db pg_dump -U hunter -d hunter -Fc > "$out.tmp"
mv "$out.tmp" "$out"
find backups -name 'hunter-*.dump' -mtime +"${HUNTER_BACKUP_KEEP_DAYS:-14}" -delete
echo "Backup: $out ($(du -h "$out" | cut -f1))"
