"""Event bus: PostgreSQL for the audit trail, Redis pub/sub for the dashboard.

Two sinks, on purpose:

* ``budget_events`` in PostgreSQL is durable history — what fired, when, and
  who released it. This is what an incident review reads afterwards.
* A Redis pub/sub fan-out drives the dashboard's SSE stream. Pub/sub rather
  than each worker polling means a threshold crossing recorded by *any*
  uvicorn worker reaches *every* connected browser.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BudgetEvent
from app.db.session import session_scope
from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)

Severity = Literal["info", "warning", "critical"]


@dataclass
class Event:
    type: str
    severity: Severity
    scope: str
    scope_id: str
    message: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


async def emit(
    event: Event,
    *,
    session: AsyncSession | None = None,
    persist: bool = True,
) -> None:
    """Publish an event and (optionally) record it durably.

    Publication failures are logged, never raised: a dashboard that misses an
    update is an inconvenience, but an exception here would fail the request
    that triggered it — and the request path's job is budget enforcement, not
    telemetry delivery.
    """
    if persist:
        row = BudgetEvent(
            severity=event.severity,
            scope=event.scope,
            scope_id=str(event.scope_id),
            type=event.type,
            actor=event.actor,
            payload=event.payload,
            message=event.message,
        )
        if session is not None:
            # Join the caller's transaction so the event commits with the
            # change that caused it.
            session.add(row)
        else:
            # The proxy path has no ambient session — it deliberately holds no
            # database connection across the upstream call. Open a short-lived
            # one here rather than dropping the event: warnings and rejections
            # are precisely the history an incident review needs.
            try:
                async with session_scope() as own_session:
                    own_session.add(row)
            except Exception as exc:  # pragma: no cover
                log.error("events.persist_failed", type=event.type, error=str(exc))

    try:
        await gateway.client.publish(keys.EVENTS_CHANNEL, event.to_json())
    except Exception as exc:  # pragma: no cover - degradation path
        log.warning("events.publish_failed", type=event.type, error=str(exc))

    logger = log.bind(event_type=event.type, scope=event.scope, scope_id=event.scope_id)
    if event.severity == "critical":
        logger.error(event.message or event.type, **event.payload)
    elif event.severity == "warning":
        logger.warning(event.message or event.type, **event.payload)
    else:
        logger.info(event.message or event.type, **event.payload)


# --------------------------------------------------------------- convenience


async def emit_agent_created(session: AsyncSession, agent_payload: dict) -> None:
    await emit(
        Event(
            type="agent.created",
            severity="info",
            scope="agent",
            scope_id=str(agent_payload["id"]),
            message=f"Agent '{agent_payload['name']}' created",
            payload=agent_payload,
        ),
        session=session,
    )


async def emit_config_invalidate(agent_id: int | None = None) -> None:
    """Tell every worker to drop cached auth/limit state for an agent."""
    await emit(
        Event(
            type="config.invalidate",
            severity="info",
            scope="agent" if agent_id else "global",
            scope_id=str(agent_id or "*"),
            payload={"agent_id": agent_id},
        ),
        persist=False,
    )
