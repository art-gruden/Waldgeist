#!/bin/sh
# Container start: apply database migrations, then run the bot (PID 1 gets SIGTERM → graceful stop).
set -eu
cd /app
alembic upgrade head
exec python -m enchanted_companions
