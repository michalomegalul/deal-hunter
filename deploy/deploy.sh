#!/bin/bash
# Pull-based deploy: if GitHub has a newer release than the one running, deploy it.
# Run by hunter-deploy.timer every 5 minutes. Nothing on GitHub can reach into the server.
set -euo pipefail

REPO=michalomegalul/deal-hunter
DIR=/opt/deal-hunter
cd "$DIR"

# GitHub being unreachable is not a failed deploy (that would page every 5 minutes); just try later.
latest=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | jq -r .tag_name) || exit 0
current=$(cat .deployed 2>/dev/null || true)
if [[ -z "$latest" || "$latest" == "null" || "$latest" == "$current" ]]; then
  exit 0
fi

# A release is published before its image finishes building; try again on the next tick.
if ! docker manifest inspect "ghcr.io/$REPO:$latest" >/dev/null 2>&1; then
  echo "Release $latest found, image not built yet; will retry"
  exit 0
fi

# Bounded retry/backoff: a release that fails to deploy (bad migration, broken image, ...) must
# not re-run backup/migrate/restart every 5 minutes forever (that is how a failed deploy used to
# page every tick). .deploy_attempt remembers the version we last tried, how many times, and
# when; back off exponentially (5, 10, 20, 40... min, capped at 2h) between attempts at the same
# version. A different latest tag (a newer release) always resets this and is tried right away.
attempt_file=.deploy_attempt
attempts=0
last_attempt=0
if [[ -f "$attempt_file" ]]; then
  read -r att_version att_count att_epoch < "$attempt_file" || true
  if [[ "${att_version:-}" == "$latest" ]]; then
    attempts="${att_count:-0}"
    last_attempt="${att_epoch:-0}"
  fi
fi
now=$(date +%s)
if (( attempts > 0 )); then
  backoff_shift=$(( attempts > 5 ? 5 : attempts - 1 ))
  backoff=$(( 300 * (1 << backoff_shift) ))
  (( backoff > 7200 )) && backoff=7200
  if (( now - last_attempt < backoff )); then
    echo "Deploy of $latest failed $attempts time(s) before; backing off until $((last_attempt + backoff))"
    exit 0
  fi
fi
echo "$latest $((attempts + 1)) $now" > "$attempt_file"

echo "Deploying $latest (was: ${current:-nothing})"
# The compose file from the release itself, so stack changes ship with the code.
curl -fsSL "https://raw.githubusercontent.com/$REPO/$latest/deploy/compose.yml" -o compose.yml.new
mv compose.yml.new compose.yml

export HUNTER_VERSION="$latest"
docker compose --profile cli pull --quiet
# One-off `docker compose run --rm hunter ...` without HUNTER_VERSION uses :latest; point it here.
docker tag "ghcr.io/$REPO:$latest" "ghcr.io/$REPO:latest"
docker compose up -d --wait db
./backup.sh "pre-$latest"
docker compose run --rm hunter db migrate
docker compose up -d --remove-orphans      # (re)start every service on the new image
echo "$latest" > .deployed
rm -f "$attempt_file"      # this version made it; forget the failure history for next time
# Host scripts and systemd units ship with the release too. `mv` swaps each file, so the copy of
# this script bash is running right now is not affected.
raw="https://raw.githubusercontent.com/$REPO/$latest/deploy"
for f in deploy.sh backup.sh notify.sh; do
  curl -fsSL "$raw/$f" -o "$f.new" && chmod +x "$f.new" && mv "$f.new" "$f"
done
units_changed=0
for u in hunter-deploy.service hunter-deploy.timer hunter-backup.service hunter-backup.timer \
         hunter-watchdog.service hunter-watchdog.timer hunter-notify-failure@.service; do
  curl -fsSL "$raw/$u" -o "$u.new"
  if ! cmp -s "$u.new" "/etc/systemd/system/$u"; then
    mv "$u.new" "/etc/systemd/system/$u"
    units_changed=1
  else
    rm "$u.new"
  fi
done
if (( units_changed )); then
  systemctl daemon-reload
  systemctl enable --now hunter-deploy.timer hunter-backup.timer hunter-watchdog.timer
fi
# Drop old release images for this app (never :latest, never one a running container still
# uses) so superseded versions don't accumulate on disk. Best-effort: never fail the deploy
# over it (a container that still has an old layer checked out, say).
running_ids=$(docker compose ps -q | xargs -r docker inspect --format '{{.Image}}' 2>/dev/null | sort -u)
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' "ghcr.io/$REPO" | while read -r ref id; do
  [[ "$ref" == "ghcr.io/$REPO:latest" || "$ref" == "ghcr.io/$REPO:$latest" ]] && continue
  grep -qx "$id" <<< "$running_ids" && continue
  docker rmi "$ref" >/dev/null 2>&1 || true
done || true
echo "Deployed $latest"
