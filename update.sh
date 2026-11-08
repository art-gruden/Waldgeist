#!/usr/bin/env bash
# Update to the latest version from GitHub and restart:  cd /opt/waldgeist && sudo ./update.sh
# .env, data/ (database, backups) and logs/ are not tracked by git and stay untouched.
# Database migrations run automatically when the bot container starts.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./update.sh"; exit 1; }

echo "==> backup before update"
docker compose exec -T backup sh /backup.sh now || echo "!! backup skipped (stack not running?)"

echo "==> git pull"
before="$(git -c safe.directory="$APP_DIR" rev-parse --short HEAD)"
git -c safe.directory="$APP_DIR" pull --ff-only
after="$(git -c safe.directory="$APP_DIR" rev-parse --short HEAD)"
echo "$before -> $after"

echo "==> rebuild and restart"
docker compose build --pull
docker compose up -d --remove-orphans
# config/, content/ and assets/ are mounted into the container: restart so the bot
# re-reads texts, affirmations and images even when the image itself did not change
docker compose restart bot
docker compose ps
