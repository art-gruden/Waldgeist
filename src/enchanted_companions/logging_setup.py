"""Structured JSON logging to stdout and a rotating file. Secrets are masked."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import UTC, datetime
from pathlib import Path

_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_DSN_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")


def mask_secrets(text: str) -> str:
    text = _TOKEN_RE.sub("<bot-token>", text)
    return _DSN_PASSWORD_RE.sub(r"\1***\3", text)


class JsonFormatter(logging.Formatter):
    _STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_secrets(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in self._STANDARD and not key.startswith("_"):
                payload[key] = value if isinstance(value, int | float | bool | type(None)) else str(value)
        if record.exc_info:
            payload["exc"] = mask_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    formatter = JsonFormatter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:  # read-only filesystem etc. — stdout is enough
            root.warning("file logging disabled: %s", exc)

    # aiogram logs every update at INFO; keep the signal readable
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
