"""Uniform error envelope.

Every error the proxy returns — budget, session, runaway, auth — uses the same
shape, so a client can branch on ``error.type`` without special-casing which
layer rejected it:

    {"error": {"type": "...", "message": "...", ...context}}
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def error_body(error_type: str, message: str, **context: Any) -> dict:
    return {"error": {"type": error_type, "message": message, **context}}


def http_error(
    status_code: int, error_type: str, message: str, **context: Any
) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail=error_body(error_type, message, **context)
    )
