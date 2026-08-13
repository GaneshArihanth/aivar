"""The proxy endpoint — every LLM call passes through here.

Request lifecycle:

    authenticate → estimate → RESERVE ⇄ (substitute) → forward → SETTLE
                                  │                                 │
                                  └── reject ──────────────┐        ├─ velocity
                                                           ▼        ▼
                                                    ledger + events

The ordering is the whole point: the budget decision happens *before* the
upstream call is dispatched. A rejected request costs nothing because it never
leaves this process — which is what makes this enforcement rather than
reporting.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.errors import error_body
from app.config import settings
from app.core import budget, events, policy, runaway, upstream
from app.core.budget import Decision
from app.core.money import format_usd, format_usd_precise, micros_to_float
from app.core.pricing import DEFAULT_MAX_TOKENS, pricing
from app.core.security import AgentContext, authenticate_agent
from app.core.tokenizer import count_prompt_tokens
from app.db.repositories import ledger as ledger_repo
from app.redisx import keys

log = structlog.get_logger(__name__)

router = APIRouter(tags=["proxy"])


def _remaining_headers(reservation: budget.Reservation, request_id: str) -> dict:
    return {
        "X-Budget-Request-Id": request_id,
        "X-Budget-Agent-Remaining-USD": format_usd(
            max(0, reservation.agent_limit - reservation.agent_spend)
        ),
        "X-Budget-Team-Remaining-USD": format_usd(
            max(0, reservation.team_limit - reservation.team_spend)
        ),
        "X-Budget-Session-Remaining-USD": format_usd(
            max(0, reservation.session_limit - reservation.session_spend)
        ),
    }


def _rejection_response(
    reservation: budget.Reservation,
    *,
    agent: AgentContext,
    request_id: str,
    session_id: str,
    period: str,
) -> JSONResponse:
    """Translate a refusal into the documented error contract."""
    resets_at = keys.period_resets_at(period).isoformat()
    scope = reservation.detail or "agent"

    if reservation.status is Decision.BLOCKED:
        body = error_body(
            "agent_paused_runaway",
            f"Agent '{agent.agent_name}' is paused after tripping the runaway "
            "detector. A human must review and release it.",
            scope="agent",
            scope_id=str(agent.agent_id),
            requires="human_review",
            unblock_endpoint=f"/admin/agents/{agent.agent_id}/unblock",
        )
        return JSONResponse(status_code=status.HTTP_423_LOCKED, content=body)

    if reservation.status is Decision.FROZEN:
        # 503 rather than 402: nothing is wrong with the request or the budget,
        # the system has been deliberately stopped. A well-behaved client
        # should retry later rather than treat this as a permanent refusal.
        body = error_body(
            "dispatch_frozen",
            "Dispatch is frozen"
            + (
                " for this team."
                if reservation.detail == "team"
                else " across the whole system."
            )
            + " No requests are being forwarded until an operator lifts it.",
            scope=reservation.detail,
            frozen=True,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body,
            headers={"Retry-After": "30"},
        )

    if reservation.status is Decision.RATE_LIMITED:
        window = "requests" if reservation.detail == "rpm" else "tokens"
        body = error_body(
            "rate_limited",
            f"Agent '{agent.agent_name}' has used its {window}-per-minute allowance. "
            "The budget is untouched — this is a pacing limit.",
            scope=reservation.detail,
            used_this_minute=reservation.rate_used,
            retry_after_seconds=60 - int(time.time()) % 60,
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=body,
            headers={"Retry-After": str(60 - int(time.time()) % 60)},
        )

    if reservation.status is Decision.SESSION_EXHAUSTED:
        body = error_body(
            "session_budget_exhausted",
            "Session budget exhausted; this session is now closed. The agent's "
            "monthly budget is unaffected — open a new session to continue.",
            scope="session",
            scope_id=session_id,
            limit_usd=format_usd(reservation.session_limit),
            consumed_usd=format_usd(reservation.session_spend),
            session_closed=True,
        )
        return JSONResponse(status_code=status.HTTP_402_PAYMENT_REQUIRED, content=body)

    if reservation.status is Decision.SESSION_CLOSED:
        body = error_body(
            "session_closed",
            "This session has been closed. Start a new session to continue.",
            scope="session",
            scope_id=session_id,
            session_closed=True,
        )
        return JSONResponse(status_code=status.HTTP_402_PAYMENT_REQUIRED, content=body)

    spend, limit, name = {
        "team": (reservation.team_spend, reservation.team_limit, agent.team_name),
        "agent": (reservation.agent_spend, reservation.agent_limit, agent.agent_name),
    }.get(scope, (reservation.agent_spend, reservation.agent_limit, agent.agent_name))

    body = error_body(
        "budget_exhausted",
        f"{scope.capitalize()} monthly budget exhausted. Request rejected before "
        "dispatch.",
        scope=scope,
        scope_id=str(agent.team_id if scope == "team" else agent.agent_id),
        scope_name=name,
        limit_usd=format_usd(limit),
        consumed_usd=format_usd(spend),
        period=period,
        resets_at=resets_at,
    )
    return JSONResponse(status_code=status.HTTP_402_PAYMENT_REQUIRED, content=body)


@router.post("/v1/chat/completions", summary="Budget-enforced chat completions")
async def chat_completions(
    request: Request,
    agent: AgentContext = Depends(authenticate_agent),
) -> Response:
    started = time.perf_counter()

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_body(
                "invalid_request_body",
                "Request body is not valid JSON.",
            ),
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_body(
                "invalid_request_body",
                "Request body must be a JSON object.",
            ),
        )

    request_id = uuid.uuid4().hex
    period = keys.monthly_period()

    # A session is the unit the per-session budget applies to. Clients that do
    # not supply one get a fresh session per call, which makes the session cap
    # a de-facto per-call cap — a safe default rather than an unbounded one.
    session_id = (
        request.headers.get("X-Session-Id")
        or body.get("session_id")
        or f"auto-{uuid.uuid4().hex[:16]}"
    )

    requested_model = body.get("model") or agent.preferred_model
    messages = body.get("messages") or []

    # Opt-in to dispatching this one call to the model's real endpoint while the
    # rest of the system stays on the mock. The Demo page uses it to prove the
    # ledger against real provider token counts.
    #
    # Gated on a server-side setting so the decision to spend real money is the
    # operator's, not the caller's. When mock mode is off the flag is moot —
    # everything is already going to real endpoints.
    live_dispatch = bool(
        settings.demo_allow_live
        and (
            request.headers.get("X-Budget-Live-Dispatch", "").lower()
            in ("1", "true", "yes")
            or body.get("live_dispatch") is True
        )
    )

    # max_tokens is the output ceiling the whole reservation is sized against.
    # Absent, it defaults. Present but unusable (zero, negative, unparseable)
    # it is a client error, refused here: reserving against a fiction would
    # under-cover the call, and quietly substituting a default would change the
    # meaning of a value the caller set deliberately.
    raw_max_tokens = body.get("max_tokens")
    if raw_max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS
    else:
        try:
            max_tokens = int(raw_max_tokens)
        except (TypeError, ValueError):
            max_tokens = 0
        if max_tokens <= 0:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=error_body(
                    "invalid_max_tokens",
                    "max_tokens must be a positive integer — it is the output "
                    "ceiling this request's budget reservation is sized against.",
                    received=raw_max_tokens,
                ),
            )

    prompt_tokens = count_prompt_tokens(messages, requested_model)

    # Named to avoid shadowing the `policy` module imported above; the two
    # are different things and one silently hiding the other is a trap.
    budget_policy = await budget.load_policy(agent.agent_id, agent.team_id)

    # ------------------------------------------------------ reserve (+ swap)
    chain = _resolve_chain(agent, requested_model)
    reservation: budget.Reservation | None = None
    served_model = requested_model
    estimate_micros = 0
    substitution_reason = ""

    for index, candidate in enumerate(chain):
        price = pricing.get(candidate)
        if price is None:
            continue
        estimate_micros = price.estimate_micros(prompt_tokens, max_tokens)
        is_final = index == len(chain) - 1 or not agent.allow_substitution

        reservation = await budget.reserve(
            team_id=agent.team_id,
            agent_id=agent.agent_id,
            session_id=session_id,
            request_id=request_id,
            model=candidate,
            estimate_micros=estimate_micros,
            policy=budget_policy,
            allow_substitution=agent.allow_substitution,
            final_attempt=is_final,
            # Worst case, matching how the cost estimate is built.
            tokens=prompt_tokens + max_tokens,
            period=period,
        )

        if reservation.allowed:
            served_model = candidate
            break

        # Budget refusals are retried against the next model down. Both
        # "approaching the limit" and "this model no longer fits" are answered
        # by trying something cheaper — hard-blocking while an affordable model
        # exists would refuse work the budget can still pay for.
        retryable = reservation.status in (
            Decision.PRESSURE,
            Decision.EXHAUSTED,
            Decision.SESSION_EXHAUSTED,
        )
        if retryable and not is_final:
            scope = reservation.detail or "agent"
            verb = "pressure" if reservation.status is Decision.PRESSURE else "exhausted"
            substitution_reason = (
                f"{scope}_budget_{verb}_{int(reservation.pct_for(scope) * 100)}pct"
            )
            continue

        # BLOCKED / SESSION_CLOSED / final refusal — no model choice changes it.
        break

    if reservation is None:
        # No candidate in the chain had a price — in practice, a model the
        # caller named that is not in the catalog. Without a price there is no
        # estimate, without an estimate there is no reservation, and dispatching
        # an unpriceable call would put unmetered spend through the proxy.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body(
                "model_not_found",
                f"Model '{requested_model}' is not in the pricing catalog, so its "
                "cost cannot be estimated and it will not be dispatched.",
                requested_model=requested_model,
                available_models=pricing.model_ids(),
            ),
        )

    if not reservation.allowed:
        await _record_rejection(
            reservation, agent, request_id, session_id, period,
            requested_model, prompt_tokens, estimate_micros, started,
        )
        return _rejection_response(
            reservation, agent=agent, request_id=request_id,
            session_id=session_id, period=period,
        )

    substituted = served_model != requested_model
    if substituted:
        await events.emit(
            events.Event(
                type="model.substituted",
                severity="info",
                scope="agent",
                scope_id=str(agent.agent_id),
                message=(
                    f"'{agent.agent_name}' rerouted {requested_model} → "
                    f"{served_model} ({substitution_reason})"
                ),
                payload={
                    "agent_id": agent.agent_id,
                    "agent_name": agent.agent_name,
                    "requested_model": requested_model,
                    "served_model": served_model,
                    "reason": substitution_reason,
                },
            ),
            persist=False,
        )

    for scope in reservation.warned:
        await _emit_warning(scope, agent, reservation, period)

    # ------------------------------------------------------------- dispatch
    upstream_payload = {**body, "model": served_model}
    upstream_payload.pop("session_id", None)
    # Ours, not the provider's — it would be rejected as an unknown field.
    upstream_payload.pop("live_dispatch", None)

    served_price = pricing.require(served_model)
    try:
        result = await upstream.chat_completion(
            served_price, upstream_payload, force_model_endpoint=live_dispatch
        )
    except (
        upstream.UpstreamTimeout,
        upstream.UpstreamError,
        upstream.UpstreamNotConfigured,
    ) as exc:
        # The call produced no usage, so the hold is returned in full. Charging
        # for a provider failure would let a broken upstream drain a budget
        # without generating a single token.
        released = await budget.release(
            team_id=agent.team_id, agent_id=agent.agent_id, session_id=session_id,
            request_id=request_id, reason="upstream_failure", period=period,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        await ledger_repo.record_call(
            request_id=request_id, agent_id=agent.agent_id, team_id=agent.team_id,
            session_id=session_id, period=period, requested_model=requested_model,
            served_model=served_model, substituted=substituted,
            prompt_tokens=prompt_tokens, completion_tokens=0,
            estimated_micros=estimate_micros, actual_micros=0,
            decision="upstream_error", latency_ms=latency_ms,
        )
        log.warning(
            "proxy.upstream_failed",
            request_id=request_id, agent=agent.agent_name,
            released_micros=released, error=str(exc),
        )
        if isinstance(exc, upstream.UpstreamNotConfigured):
            # A routing/credential problem, not a provider outage — say which,
            # so the operator fixes the catalog instead of hunting the provider.
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content=error_body(
                    "model_not_dispatchable",
                    str(exc),
                    model=served_model,
                    budget_released_usd=format_usd(released),
                ),
            )
        is_timeout = isinstance(exc, upstream.UpstreamTimeout)
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT
            if is_timeout
            else status.HTTP_502_BAD_GATEWAY,
            content=error_body(
                "upstream_timeout" if is_timeout else "upstream_error",
                "The upstream provider failed. No budget was consumed.",
                budget_released_usd=format_usd(released),
            ),
        )

    # --------------------------------------------------------------- settle
    completion = result.body
    actual_prompt = result.prompt_tokens or prompt_tokens
    actual_completion = result.completion_tokens

    price = served_price
    if result.has_usage:
        actual_micros = price.actual_micros(actual_prompt, actual_completion)
    else:
        # A provider that reports no usage gets charged the reservation. The
        # alternative — charging nothing — would make "omit usage" a way to get
        # free inference.
        actual_micros = estimate_micros
        log.warning("proxy.usage_missing", request_id=request_id, model=served_model)

    settlement = await budget.settle(
        team_id=agent.team_id, agent_id=agent.agent_id, session_id=session_id,
        request_id=request_id, actual_micros=actual_micros, period=period,
    )

    await runaway.record_spend(
        team_id=agent.team_id,
        agent_id=agent.agent_id,
        agent_name=agent.agent_name,
        amount_micros=actual_micros,
        monthly_limit_micros=budget_policy.monthly_micros,
        fraction=budget_policy.runaway_fraction,
    )

    latency_ms = int((time.perf_counter() - started) * 1000)
    await ledger_repo.record_call(
        request_id=request_id, agent_id=agent.agent_id, team_id=agent.team_id,
        session_id=session_id, period=period, requested_model=requested_model,
        served_model=served_model, substituted=substituted,
        prompt_tokens=actual_prompt, completion_tokens=actual_completion,
        estimated_micros=estimate_micros, actual_micros=actual_micros,
        decision="substituted" if substituted else "allowed",
        latency_ms=latency_ms,
    )

    headers = {
        "X-Budget-Request-Id": request_id,
        "X-Session-Id": session_id,
        "X-Budget-Model-Requested": requested_model,
        "X-Budget-Model-Served": served_model,
        "X-Budget-Cost-USD": format_usd_precise(actual_micros),
        "X-Budget-Agent-Remaining-USD": format_usd(
            max(0, reservation.agent_limit - settlement.agent_spend)
        ),
        "X-Budget-Team-Remaining-USD": format_usd(
            max(0, reservation.team_limit - settlement.team_spend)
        ),
    }
    # Only meaningful when a per-session cap exists; a limit of 0 means "no
    # session cap", and reporting "0.00 remaining" would read as the opposite.
    if reservation.session_limit > 0:
        headers["X-Budget-Session-Remaining-USD"] = format_usd(
            max(0, reservation.session_limit - settlement.session_spend)
        )
    if substituted:
        headers["X-Budget-Substitution-Reason"] = substitution_reason

    return JSONResponse(content=completion, headers=headers)


def _resolve_chain(agent: AgentContext, requested_model: str) -> list[str]:
    """Models to try, in order.

    Anchored on the model the caller actually asked for: if that differs from
    the agent's configured preference, the configured chain does not apply and
    alternatives are derived from the catalog instead.

    The stored chain is filtered rather than trusted wholesale. It was written
    at some point in the past, and since then a model may have been deactivated
    or cross-provider permission withdrawn — a stale entry would burn a
    reservation attempt on something that cannot serve the request.
    """
    if not agent.allow_substitution:
        return [requested_model]

    if agent.fallback_chain and agent.fallback_chain[0] == requested_model:
        chain = list(agent.fallback_chain)
    else:
        chain = [
            requested_model,
            *(
                m.model_id
                for m in pricing.cheaper_alternatives(
                    requested_model, cross_provider=agent.allow_cross_provider
                )
            ),
        ]

    usable = policy.usable_chain(
        chain,
        allow_cross_provider=agent.allow_cross_provider,
        allow_substitution=agent.allow_substitution,
    )
    return usable or [requested_model]


async def _emit_warning(
    scope: str, agent: AgentContext, reservation: budget.Reservation, period: str
) -> None:
    spend, limit, name, scope_id = {
        "team": (
            reservation.team_spend, reservation.team_limit,
            agent.team_name, agent.team_id,
        ),
        "agent": (
            reservation.agent_spend, reservation.agent_limit,
            agent.agent_name, agent.agent_id,
        ),
    }[scope]
    await events.emit(
        events.Event(
            type="budget.warning",
            severity="warning",
            scope=scope,
            scope_id=str(scope_id),
            message=(
                f"{scope.capitalize()} '{name}' has consumed "
                f"{reservation.pct_for(scope) * 100:.0f}% of its "
                f"${format_usd(limit)} budget for {period}"
            ),
            payload={
                "scope": scope,
                "scope_id": scope_id,
                "name": name,
                "consumed_usd": micros_to_float(spend),
                "limit_usd": micros_to_float(limit),
                "pct": round(reservation.pct_for(scope), 4),
                "period": period,
            },
        )
    )


async def _record_rejection(
    reservation: budget.Reservation,
    agent: AgentContext,
    request_id: str,
    session_id: str,
    period: str,
    requested_model: str,
    prompt_tokens: int,
    estimate_micros: int,
    started: float,
) -> None:
    decision = {
        Decision.EXHAUSTED: "rejected_budget",
        Decision.SESSION_EXHAUSTED: "rejected_session",
        Decision.SESSION_CLOSED: "rejected_session",
        Decision.BLOCKED: "rejected_runaway",
    }.get(reservation.status, "rejected_budget")

    await ledger_repo.record_call(
        request_id=request_id, agent_id=agent.agent_id, team_id=agent.team_id,
        session_id=session_id, period=period, requested_model=requested_model,
        served_model=None, substituted=False, prompt_tokens=prompt_tokens,
        completion_tokens=0, estimated_micros=estimate_micros, actual_micros=0,
        decision=decision, latency_ms=int((time.perf_counter() - started) * 1000),
    )

    scope = reservation.detail or "agent"
    spend, limit = {
        "team": (reservation.team_spend, reservation.team_limit),
        "agent": (reservation.agent_spend, reservation.agent_limit),
        "session": (reservation.session_spend, reservation.session_limit),
    }.get(scope, (reservation.agent_spend, reservation.agent_limit))

    severity = "critical" if reservation.status is Decision.BLOCKED else "warning"
    await events.emit(
        events.Event(
            type=f"budget.{decision}",
            severity=severity,
            scope=scope,
            scope_id=str(agent.agent_id),
            message=(
                f"Rejected '{agent.agent_name}': {reservation.status.value.lower()} "
                f"on {scope} scope"
            ),
            payload={
                # request_id joins the event to its ledger row, so the detail
                # overlay can show the exact call that was refused rather than
                # only that something was.
                "request_id": request_id,
                "agent_id": agent.agent_id,
                "agent_name": agent.agent_name,
                "team_id": agent.team_id,
                "team_name": agent.team_name,
                "status": reservation.status.value,
                "scope": scope,
                "session_id": session_id,
                "requested_model": requested_model,
                # The numbers behind the refusal: which rule bound, and by how
                # much. Reading "exhausted" without them means opening three
                # other pages to find out what the limit even was.
                "consumed_usd": micros_to_float(spend),
                "limit_usd": micros_to_float(limit),
                "would_have_cost_usd": micros_to_float(estimate_micros),
                "period": period,
            },
        )
    )
