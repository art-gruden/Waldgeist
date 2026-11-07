#!/usr/bin/env bash
# Waldgeist (Enchanted Companions) — install on an Ubuntu server right after cloning.
#
#   sudo git clone https://github.com/<you>/Waldgeist.git /opt/waldgeist
#   cd /opt/waldgeist && sudo ./install.sh
#
# Non-interactive (e.g. from Ansible/CI):
#   sudo BOT_TOKEN=123:abc GROUP_CHAT_ID=-100123 ./install.sh
#
# What it does:
#   1. installs Docker + Compose v2 if they are missing (nothing else);
#   2. creates .env (random DB password; asks for the bot token and group ID);
#   3. prepares data/ and logs/, installs a systemd unit (start at boot);
#   4. builds the image and starts bot + PostgreSQL + daily backups.
# It does NOT touch Galene, its config, ports or the firewall: the bot opens no ports.
# Safe to re-run: existing .env values, the database and backups are kept.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx  %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo: sudo ./install.sh"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || warn "Tested on Ubuntu 22.04/24.04, found ${PRETTY_NAME:-unknown}"

env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }
env_set() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}
interactive() { [[ -t 0 ]]; }

# ---------------------------------------------------------------- 1. Docker
say "Docker"
if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y docker.io
    systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
    apt-get update
    apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin \
        || die "Install the Docker Compose v2 plugin and re-run."
fi
docker --version
docker compose version

say "Galene (left untouched)"
if systemctl list-unit-files 2>/dev/null | grep -qi '^galene'; then
    echo "galene.service: $(systemctl is-active galene 2>/dev/null || true)"
else
    echo "No galene systemd unit found — nothing to check."
fi
echo "The bot publishes no ports: no conflict with Galene's HTTPS/TURN ports."

# ------------------------------------------------------------------ 2. .env
say "Configuration (.env)"
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env from .env.example"
fi
chmod 600 .env

if [[ -z "$(env_get POSTGRES_PASSWORD)" ]]; then
    env_set POSTGRES_PASSWORD "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "Generated a random database password."
fi

token="${BOT_TOKEN:-$(env_get BOT_TOKEN)}"
if [[ -z "$token" ]] && interactive; then
    read -r -s -p "Bot token from @BotFather: " token
    echo
fi
[[ -n "$token" ]] || die "BOT_TOKEN is required: put it into $APP_DIR/.env or run: sudo BOT_TOKEN=... ./install.sh"
[[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "This does not look like a bot token (expected 123456:ABC...)."
env_set BOT_TOKEN "$token"

group="${GROUP_CHAT_ID:-$(env_get GROUP_CHAT_ID)}"
if [[ -z "$group" ]] && interactive; then
    echo "Group chat ID (negative number). Unknown yet? Press Enter — later send /chatid in the group."
    read -r -p "GROUP_CHAT_ID: " group
fi
if [[ -n "$group" ]]; then
    [[ "$group" =~ ^-[0-9]+$ ]] || die "GROUP_CHAT_ID must be a negative number like -1001234567890"
    env_set GROUP_CHAT_ID "$group"
fi

if [[ -n "${ADMIN_TG_IDS:-}" ]]; then
    env_set ADMIN_TG_IDS "$ADMIN_TG_IDS"
fi
[[ -n "$(env_get ADMIN_TG_IDS)" ]] || warn "ADMIN_TG_IDS is empty: nobody can use admin commands."

if command -v curl >/dev/null 2>&1; then
    if me="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${token}/getMe" 2>/dev/null)"; then
        username="$(printf '%s' "$me" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
        echo "Telegram token OK: @${username}"
    else
        warn "Could not verify the token with Telegram (wrong token or no internet)."
    fi
fi

# ------------------------------------------------------- 3. folders, systemd
say "Folders and content"
mkdir -p data/pgdata data/backups logs assets/images
chown -R 10001:10001 logs        # the bot runs as uid 10001 inside the container
chmod 700 data
[[ -f content/uk/affirmations.yaml ]] \
    || warn "content/uk/affirmations.yaml is missing: guests of Omniya's pet get no affirmations until you add it."

say "systemd unit (start at boot)"
sed "s#/opt/waldgeist#${APP_DIR}#g" deploy/enchanted-companions.service \
    > /etc/systemd/system/enchanted-companions.service
systemctl daemon-reload
systemctl enable enchanted-companions.service >/dev/null
echo "enchanted-companions.service enabled"

# ------------------------------------------------------------ 4. build & run
say "Build and start"
docker compose build --pull
docker compose up -d --remove-orphans

printf 'Waiting for the bot to become healthy'
for _ in $(seq 1 30); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q bot)" 2>/dev/null || echo starting)"
    [[ "$status" == "healthy" ]] && break
    printf '.'
    sleep 5
done
echo
docker compose ps
echo
docker compose logs --tail 15 bot || true

say "Done"
if [[ -z "$(env_get GROUP_CHAT_ID)" ]]; then
    echo "Next: add the bot to the group as ADMIN, send /chatid there (from an admin account),"
    echo "      then: sudo GROUP_CHAT_ID=-100... ./install.sh   (or edit .env and: sudo systemctl restart enchanted-companions)"
fi
echo "Logs:    cd $APP_DIR && docker compose logs -f bot"
echo "Update:  cd $APP_DIR && sudo ./update.sh"
echo "Backup:  cd $APP_DIR && docker compose exec backup sh /backup.sh now"
