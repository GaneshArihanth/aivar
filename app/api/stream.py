"""Server-Sent Events feed for the dashboard.

SSE rather than WebSockets: the traffic is entirely one-way (server → browser),
`EventSource` reconnects on its own, and it needs no handshake handling. A
WebSocket would add a protocol upgrade and a keepalive implementation for no
gain.

Events arrive over Redis pub/sub rather than from process-local state, so a
threshold crossed by *any* uvicorn worker reaches *every* connected browser.
With in-process fan-out, a dashboard would show only the events that happened
to land on the worker it connected to.
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)

router = APIRouter(tags=["status"])

HEARTBEAT_SECONDS = 15


@router.get("/events/stream")
async def event_stream(request: Request) -> StreamingResponse:
    async def generate():
        pubsub = gateway.client.pubsub()
        await pubsub.subscribe(keys.EVENTS_CHANNEL)
        log.info("sse.client_connected")

        # Tells the browser to wait 2s before reconnecting after a drop.
        yield "retry: 2000\n\n"
        yield f"event: connected\ndata: {json.dumps({'ok': True})}\n\n"

        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
                )
                if message is None:
                    # A comment line keeps intermediaries from closing an idle
                    # connection, and costs one line every 15 seconds.
                    yield ": heartbeat\n\n"
                    continue

                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                try:
                    event_type = json.loads(data).get("type", "message")
                except (ValueError, AttributeError):
                    event_type = "message"
                yield f"event: {event_type}\ndata: {data}\n\n"
        except asyncio.CancelledError:  # pragma: no cover - client went away
            raise
        finally:
            await pubsub.unsubscribe(keys.EVENTS_CHANNEL)
            await pubsub.aclose()
            log.info("sse.client_disconnected")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # stops nginx buffering the stream
        },
    )
