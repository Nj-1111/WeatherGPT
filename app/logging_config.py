"""Logging setup — nothing else in the app calls basicConfig. Every log line carries the request ID for tracing; set WEATHERGPT_LOG_JSON=true for machine-readable output."""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

from app.config import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {key: value for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and key != "request_id"}


class _TextFormatter(logging.Formatter):
    """Renders structured extras inline; without this they are invisible outside JSON mode."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = _extras(record)
        if extras:
            line += " " + " ".join(f"{key}={value!r}" for key, value in extras.items())
        return line


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter("%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    # uvicorn installs its own handlers; route them through ours so format stays uniform.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
