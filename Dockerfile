# Enchanted Companions bot image. Runs as an unprivileged user and opens no ports.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
RUN pip install --no-deps .

# Defaults baked into the image; docker-compose mounts the live, editable copies over them.
COPY config ./config
COPY content ./content
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin ec \
    && mkdir -p /app/logs /app/assets/images \
    && chown -R ec:ec /app/logs \
    && chmod 0755 /usr/local/bin/entrypoint.sh

USER ec

# The bot writes a heartbeat every 30 s; no heartbeat for 2 minutes = unhealthy.
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,sys,time; p='/tmp/ec-heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 120 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
