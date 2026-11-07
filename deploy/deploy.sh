#!/usr/bin/env bash
# Deploy/update from your Mac to the server over SSH (rsync), then rebuild and restart.
#   ./deploy/deploy.sh user@server [/opt/waldgeist]
# Server-side .env, data/ (database, backups) and logs/ are never overwritten or deleted.
set -euo pipefail

target="${1:?usage: $0 user@server [/opt/waldgeist]}"
dir="${2:-/opt/waldgeist}"
cd "$(dirname "$0")/.."

echo "==> preparing $target:$dir"
ssh "$target" "sudo mkdir -p '$dir' && sudo chown \$(id -u):\$(id -g) '$dir'"

echo "==> syncing files"
rsync -az --delete --exclude-from=deploy/rsync-exclude.txt ./ "$target:$dir/"

echo "==> restarting"
ssh -t "$target" "cd '$dir' && if [ -f .env ]; then sudo docker compose up -d --build --remove-orphans && sudo docker compose ps; else echo 'First deploy: run  cd $dir && sudo ./install.sh'; fi"
