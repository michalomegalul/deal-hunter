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
docker image prune -f >/dev/null
echo "Deployed $latest"
