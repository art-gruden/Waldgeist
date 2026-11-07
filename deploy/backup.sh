#!/bin/sh
# Daily PostgreSQL backup (runs inside the "backup" container).
#   BACKUP_TIME       local time of the daily dump, HH:MM (default 03:30)
#   BACKUP_KEEP_DAYS  how many days of dumps to keep (default 14)
# One-off backup now:  docker compose exec backup sh /backup.sh now
set -eu
BACKUP_TIME="${BACKUP_TIME:-03:30}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
mkdir -p /backups

dump() {
    file="/backups/enchanted-$(date +%Y%m%d-%H%M%S).dump"
    if pg_dump --format=custom --no-owner --file="$file.tmp"; then
        mv "$file.tmp" "$file"
        echo "$(date -Iseconds) backup ok: $(basename "$file") ($(du -h "$file" | cut -f1))"
    else
        rm -f "$file.tmp"
        echo "$(date -Iseconds) backup FAILED" >&2
    fi
    find /backups -name 'enchanted-*.dump' -type f -mtime +"$BACKUP_KEEP_DAYS" -print -delete
}

if [ "${1:-}" = "now" ]; then
    dump
    exit 0
fi

echo "$(date -Iseconds) backups daily at $BACKUP_TIME, keeping $BACKUP_KEEP_DAYS days"
while true; do
    now=$(date +%s)
    target=$(date -d "$(date +%Y-%m-%d) $BACKUP_TIME" +%s)
    [ "$target" -le "$now" ] && target=$((target + 86400))
    sleep $((target - now))
    dump
done
