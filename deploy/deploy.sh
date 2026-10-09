#!/bin/bash
# Pull-based deploy: if GitHub has a newer release than the one running, deploy it.
# Run by hunter-deploy.timer every 5 minutes. Nothing on GitHub can reach into the server.
set -euo pipefail

REPO=michalomegalul/deal-hunter
DIR=/opt/deal-hunter
cd "$DIR"

latest=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | jq -r .tag_name)
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
docker compose up -d --wait db
docker compose run --rm hunter db migrate
docker compose up -d --remove-orphans      # (re)start every service on the new image
echo "$latest" > .deployed
# Update this script from the release for next time. `mv` swaps the file, so the
# copy bash is running right now is not affected.
curl -fsSL "https://raw.githubusercontent.com/$REPO/$latest/deploy/deploy.sh" -o deploy.sh.new \
  && chmod +x deploy.sh.new && mv deploy.sh.new deploy.sh
docker image prune -f >/dev/null
echo "Deployed $latest"
