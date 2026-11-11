#!/bin/bash
# Last-resort push straight from the host, for when the app can't send one itself (database down,
# broken image, failed deploy). Usage: notify.sh "title" ["message"]
set -uo pipefail
cd /opt/deal-hunter

# Cooldown: the same failure (same title+message, e.g. the same unit failing repeatedly) must not
# page more than once per hour. Without this, a stuck backup/deploy/watchdog unit re-fires every
# 10 minutes forever (exactly what happened during the Nov 2026 outage). The first occurrence of
# a given message always pushes immediately; only repeats within the window are suppressed.
state_dir=/run/deal-hunter-notify
mkdir -p "$state_dir" 2>/dev/null || state_dir=/var/tmp/deal-hunter-notify
mkdir -p "$state_dir" 2>/dev/null || true
key=$(printf '%s' "${1:-}
${2:-}" | md5sum | cut -d' ' -f1)
state_file="$state_dir/$key"
cooldown_sec=$((60 * 60))
now=$(date +%s)
if [[ -f "$state_file" ]]; then
  last=$(cat "$state_file" 2>/dev/null || echo 0)
  if [[ "$last" =~ ^[0-9]+$ ]] && (( now - last < cooldown_sec )); then
    echo "notify.sh: suppressing duplicate within cooldown: $1" >&2
    exit 0
  fi
fi
echo "$now" > "$state_file" 2>/dev/null || true

env_val() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -E "s/^[\"']//; s/[\"']$//"; }

url=$(env_val HUNTER_NTFY_URL)
topic=$(env_val HUNTER_NTFY_TOPIC)
curl -fsS -m 20 \
  -H "Authorization: Bearer $(env_val NTFY_TOKEN)" \
  -H "Title: deal-hunter: $1" -H "Priority: 4" -H "Tags: warning" \
  -d "${2:-see journalctl on CT 121}" \
  "${url:-http://192.168.4.36}/${topic:-deals}" >/dev/null
