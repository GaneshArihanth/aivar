"""structlog configuration.

Includes a hard redaction pass for raw API keys. A generated key exists in
exactly one place — the body of the response that created it — and a stray
``log.info("auth failed", key=raw)`` would quietly undo that guarantee. The
processor below scrubs any ``sk-agent-…`` literal from every log record
regardless of which field it arrives in.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

API_KEY_RE = re.compile(r"sk-agent-[A-Za-z0-9_\-]{8,}")
REDACTED = "sk-agent-***REDACTED***"


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return API_KEY_RE.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v) for v in value)
    return value


def redact_api_keys(_logger: Any, _name: str, event_dict: dict) -> dict:
    return {k: _scrub(v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_api_keys,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
