#!/bin/bash
# Last-resort push straight from the host, for when the app can't send one itself (database down,
# broken image, failed deploy). Usage: notify.sh "title" ["message"]
set -uo pipefail
cd /opt/deal-hunter

env_val() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -E "s/^[\"']//; s/[\"']$//"; }

url=$(env_val HUNTER_NTFY_URL)
topic=$(env_val HUNTER_NTFY_TOPIC)
curl -fsS -m 20 \
  -H "Authorization: Bearer $(env_val NTFY_TOKEN)" \
  -H "Title: deal-hunter: $1" -H "Priority: 4" -H "Tags: warning" \
  -d "${2:-see journalctl on CT 121}" \
  "${url:-http://192.168.4.36}/${topic:-deals}" >/dev/null
