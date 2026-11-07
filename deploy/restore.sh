#!/usr/bin/env bash
# Restore the database from a backup file:  sudo ./deploy/restore.sh data/backups/enchanted-YYYYMMDD-HHMMSS.dump
# The bot is stopped during the restore and started again afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."

dump="${1:-}"
if [[ -z "$dump" || ! -f "$dump" ]]; then
    echo "usage: $0 data/backups/enchanted-YYYYMMDD-HHMMSS.dump" >&2
    ls -1t data/backups/*.dump 2>/dev/null | head -5 >&2 || true
    exit 1
fi

set -a; source .env; set +a
db="${POSTGRES_DB:-enchanted}"
user="${POSTGRES_USER:-ec}"

read -r -p "Restore '$dump' into database '$db'? Current data will be replaced. [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "cancelled"; exit 1; }

docker compose stop bot
docker compose exec -T db pg_restore --clean --if-exists --no-owner -U "$user" -d "$db" < "$dump"
docker compose start bot
echo "restored from $dump"
