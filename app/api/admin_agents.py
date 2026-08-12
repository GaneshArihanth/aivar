"""Agent administration — the API behind the dashboard's agent management.

The raw API key appears in exactly two responses in this whole system: the 201
from ``POST /admin/agents`` and the 200 from ``.../rotate-key``. It is never
persisted, never re-readable, and scrubbed from logs by
``app.logging_setup.redact_api_keys``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.api import schemas
from app.api.errors import http_error
from app.core import budget, events, policy, security
from app.core.pricing import pricing
from app.core.money import format_usd, micros_to_float, usd_to_micros
from app.db.models import Agent, Policy
from app.db.repositories import agents as agent_repo
from app.db.repositories import budgets as budget_repo
from app.db.repositories import catalog as catalog_repo
from app.db.repositories import ledger as ledger_repo
from app.db.session import get_session
from app.redisx import keys
from app.redisx.client import gateway

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/admin/agents",
    tags=["admin:agents"],
    dependencies=[Depends(security.require_admin)],
)


# --------------------------------------------------------------- serialisation


async def _to_out(session: AsyncSession, agent: Agent) -> schemas.AgentOut:
    monthly, per_session = await agent_repo.get_agent_budgets(session, agent.id)
    chain = agent.policy.fallback_chain if agent.policy else [agent.preferred_model]
    return schemas.AgentOut(
        id=agent.id,
        name=agent.name,
        team_id=agent.team_id,
        team_name=agent.team.name,
        preferred_model=agent.preferred_model,
        allow_substitution=agent.allow_substitution,
        status=agent.status,
        key_prefix=agent.key_prefix,
        created_at=agent.created_at,
        monthly_budget_usd=micros_to_float(monthly.limit_micros if monthly else 0),
        session_budget_usd=micros_to_float(
            per_session.limit_micros if per_session else 0
        ),
        fallback_chain=chain,
        chain_is_custom=agent.policy.is_custom if agent.policy else False,
        allow_cross_provider=agent.allow_cross_provider,
        rpm_limit=agent.rpm_limit,
        tpm_limit=agent.tpm_limit,
        runaway_hourly_fraction=(
            monthly.runaway_hourly_fraction if monthly else None
        ),
    )


# --------------------------------------------------------------------- create


@router.post(
    "",
    response_model=schemas.AgentCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent and issue its API key",
)
async def create_agent(
    body: schemas.AgentCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> schemas.AgentCreatedResponse:
    """Create an agent, its monthly and per-session budgets, and its policy.

    Duplicate submissions (an impatient double-click) are stopped by the
    partial unique index on ``(team_id, name)``, which returns 409 rather than
    creating a second agent. That is preferred over an Idempotency-Key cache
    here, because replaying this endpoint's response would mean caching a raw
    API key somewhere outside the response body.
    """
    try:
        created = await agent_repo.create_agent(
            session,
            name=body.name,
            team_id=body.team_id,
            monthly_micros=usd_to_micros(body.monthly_budget_usd),
            session_micros=usd_to_micros(body.session_budget_usd),
            preferred_model=body.preferred_model,
            allow_substitution=body.allow_substitution,
            runaway_hourly_fraction=body.runaway_hourly_fraction,
        )
    except agent_repo.TeamNotFound:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            "team_not_found",
            f"No team with id {body.team_id}.",
            field="team_id",
        ) from None
    except agent_repo.ModelNotFound:
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "model_not_found",
            f"Model '{body.preferred_model}' is not in the catalog or is inactive.",
            field="preferred_model",
        ) from None
    except agent_repo.AgentNameTaken:
        raise http_error(
            status.HTTP_409_CONFLICT,
            "agent_name_taken",
            f"Team already has an agent named '{body.name}'.",
            field="name",
        ) from None

    agent_out = await _to_out(session, created.agent)

    # Mirror the new limits into Redis so the very first request this agent
    # makes is enforced, rather than the one after some cache warms up.
    await budget_repo.warm_limit_cache(
        created.agent.team_id,
        created.agent.id,
        keys.monthly_period(),
        team_limit=None,
        agent_limit=created.monthly_micros,
    )

    await events.emit_agent_created(session, agent_out.model_dump(mode="json"))

    await session.commit()

    log.info(
        "agent.created",
        agent_id=created.agent.id,
        team_id=created.agent.team_id,
        name=created.agent.name,
        monthly_usd=format_usd(created.monthly_micros),
        key_prefix=created.agent.key_prefix,  # prefix only — never the key
    )

    return schemas.AgentCreatedResponse(agent=agent_out, api_key=created.raw_api_key)


# ----------------------------------------------------------------------- read


@router.get("", response_model=list[schemas.AgentOut])
async def list_agents(
    session: AsyncSession = Depends(get_session),
) -> list[schemas.AgentOut]:
    return [await _to_out(session, a) for a in await agent_repo.list_agents(session)]


@router.get("/{agent_id}", response_model=schemas.AgentOut)
async def get_agent(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> schemas.AgentOut:
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )
    return await _to_out(session, agent)


# --------------------------------------------------------------------- update


@router.patch("/{agent_id}", response_model=schemas.AgentOut)
async def update_agent(
    agent_id: int,
    body: schemas.AgentUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> schemas.AgentOut:
    """Partial update.

    Lowering a budget below what the agent has already spent this period is
    allowed and takes effect immediately — the agent will be refused on its
    next call. The dashboard warns before submitting; the API does not block
    it, because "stop this agent now" is a legitimate thing to want.
    """
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )
    if agent.status == "blocked" and body.status is not None:
        raise http_error(
            status.HTTP_409_CONFLICT,
            "agent_blocked",
            "This agent was paused by the runaway detector. Release it via "
            f"POST /admin/agents/{agent_id}/unblock so the reason is recorded.",
        )

    if body.name is not None and body.name != agent.name:
        if await agent_repo.name_is_taken(
            session, agent.team_id, body.name, exclude_id=agent.id
        ):
            raise http_error(
                status.HTTP_409_CONFLICT,
                "agent_name_taken",
                f"Team already has an agent named '{body.name}'.",
                field="name",
            )
        agent.name = body.name

    if body.preferred_model is not None:
        model = await catalog_repo.get_model(session, body.preferred_model)
        if model is None or not model.is_active:
            raise http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "model_not_found",
                f"Model '{body.preferred_model}' is not in the catalog or is inactive.",
                field="preferred_model",
            )
        agent.preferred_model = body.preferred_model
        chain = await catalog_repo.build_fallback_chain(
            session, body.preferred_model, cross_provider=agent.allow_cross_provider
        )
        if agent.policy is None:
            policy = Policy(agent_id=agent.id, fallback_chain=chain)
            session.add(policy)
            set_committed_value(agent, "policy", policy)
        else:
            agent.policy.fallback_chain = chain

    if body.allow_substitution is not None:
        agent.allow_substitution = body.allow_substitution
    if body.allow_cross_provider is not None:
        agent.allow_cross_provider = body.allow_cross_provider
    if body.rpm_limit is not None:
        agent.rpm_limit = body.rpm_limit or None
    if body.tpm_limit is not None:
        agent.tpm_limit = body.tpm_limit or None
    if body.status is not None:
        agent.status = body.status

    month = keys.monthly_period()
    if body.monthly_budget_usd is not None or body.runaway_hourly_fraction is not None:
        existing_monthly, _ = await agent_repo.get_agent_budgets(session, agent.id)
        micros = (
            usd_to_micros(body.monthly_budget_usd)
            if body.monthly_budget_usd is not None
            else (existing_monthly.limit_micros if existing_monthly else 0)
        )
        await budget_repo.upsert_budget(
            session,
            scope="agent",
            scope_id=agent.id,
            period="monthly",
            limit_micros=micros,
            runaway_hourly_fraction=body.runaway_hourly_fraction,
        )
        await budget_repo.warm_limit_cache(
            agent.team_id, agent.id, month, team_limit=None, agent_limit=micros
        )
    if body.session_budget_usd is not None:
        await budget_repo.upsert_budget(
            session,
            scope="session",
            scope_id=agent.id,
            period="per_session",
            limit_micros=usd_to_micros(body.session_budget_usd),
        )

    await session.flush()
    # Both caches must drop: auth carries the fallback chain and substitution
    # flag, policy carries the limits. Leaving either stale means a budget
    # change silently takes up to a cache TTL to take effect.
    security.auth_cache.invalidate_agent(agent.id)
    budget.policy_cache.invalidate(agent.id)
    await events.emit_config_invalidate(agent.id)

    agent_out = await _to_out(session, agent)
    await events.emit(
        events.Event(
            type="agent.updated",
            severity="info",
            scope="agent",
            scope_id=str(agent.id),
            message=f"Agent '{agent.name}' updated",
            payload=agent_out.model_dump(mode="json"),
        ),
        session=session,
    )
    await session.commit()
    return agent_out


# --------------------------------------------------------------------- delete


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> Response:
    """Soft-delete: revokes the key, keeps the spend history."""
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    name, team_id = agent.name, agent.team_id
    await agent_repo.soft_delete(session, agent)
    await session.flush()

    await budget_repo.drop_limit_cache(team_id, agent_id, keys.monthly_period())
    await events.emit_config_invalidate(agent_id)
    await events.emit(
        events.Event(
            type="agent.deleted",
            severity="warning",
            scope="agent",
            scope_id=str(agent_id),
            message=f"Agent '{name}' deleted; its API key is revoked",
            payload={"agent_id": agent_id, "team_id": team_id, "name": name},
        ),
        session=session,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- key rotation


@router.post("/{agent_id}/rotate-key", response_model=schemas.KeyRotatedResponse)
async def rotate_key(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> schemas.KeyRotatedResponse:
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    raw_key = await agent_repo.rotate_key(session, agent)
    await session.flush()

    await events.emit(
        events.Event(
            type="agent.key_rotated",
            severity="warning",
            scope="agent",
            scope_id=str(agent_id),
            message=f"API key rotated for '{agent.name}'; the previous key is revoked",
            payload={"agent_id": agent_id, "key_prefix": agent.key_prefix},
        ),
        session=session,
    )
    await session.commit()
    return schemas.KeyRotatedResponse(
        agent_id=agent_id, api_key=raw_key, key_prefix=agent.key_prefix
    )


# ------------------------------------------------------------- team transfer


@router.post("/{agent_id}/move", response_model=schemas.AgentOut)
async def move_agent(
    agent_id: int,
    body: schemas.AgentMoveRequest,
    session: AsyncSession = Depends(get_session),
) -> schemas.AgentOut:
    """Reassign an agent to another team.

    Not merely a foreign key: every live counter is namespaced by team, so the
    agent's spend, its runaway pause and its velocity window are carried across
    with it. Team totals stay where they are — the old team did incur that
    spend, and rewriting it away would misstate what a department consumed.
    """
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )
    if body.team_id == agent.team_id:
        return await _to_out(session, agent)

    target = await agent_repo.get_team(session, body.team_id)
    if target is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND,
            "team_not_found",
            f"No team with id {body.team_id}.",
            field="team_id",
        )
    if await agent_repo.name_is_taken(session, body.team_id, agent.name):
        raise http_error(
            status.HTTP_409_CONFLICT,
            "agent_name_taken",
            f"'{target.name}' already has an agent named '{agent.name}'. "
            "Rename one of them first.",
            field="name",
        )

    from_team = agent.team_id
    monthly, _ = await agent_repo.get_agent_budgets(session, agent.id)
    moved = await budget.move_agent_counters(
        agent_id=agent.id,
        from_team=from_team,
        to_team=body.team_id,
        monthly_limit_micros=monthly.limit_micros if monthly else 0,
    )

    agent.team_id = body.team_id
    set_committed_value(agent, "team", target)
    await session.flush()

    security.auth_cache.invalidate_agent(agent.id)
    budget.policy_cache.invalidate(agent.id)
    await events.emit(
        events.Event(
            type="agent.moved",
            severity="warning",
            scope="agent",
            scope_id=str(agent.id),
            actor="dashboard",
            message=(
                f"Agent '{agent.name}' moved to '{target.name}'. "
                f"{format_usd(moved['moved_micros'])} of this period's spend moved "
                "with it; the previous team's total is unchanged."
            ),
            payload={
                "agent_id": agent.id,
                "from_team": from_team,
                "to_team": body.team_id,
                "moved_usd": micros_to_float(moved["moved_micros"]),
                "blocked_moved": moved["blocked_moved"],
                "velocity_buckets_moved": moved["velocity_buckets"],
            },
        ),
        session=session,
    )
    await session.commit()

    log.info("agent.moved", agent_id=agent.id, from_team=from_team, to_team=body.team_id, **moved)
    return await _to_out(session, agent)


# --------------------------------------------------------- detail: analytics


@router.get("/{agent_id}/history")
async def agent_history(
    agent_id: int, days: int = 7, session: AsyncSession = Depends(get_session)
) -> dict:
    """Bucketed spend plus token/latency totals for the agent detail page."""
    if days not in (7, 30, 90):
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_window",
            "days must be 7, 30 or 90.",
            field="days",
        )
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    history = await ledger_repo.spend_history(session, agent_id, days=days)
    totals = await ledger_repo.agent_totals(session, agent_id, days=days)

    # Fill the quiet buckets. A sparse series plots only the hours that had
    # traffic, so a single busy hour is drawn as a bar spanning the whole
    # window — the x-axis stops meaning time. Dense buckets make a gap look
    # like a gap.
    granularity = "hour" if days <= 7 else "day"
    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    now = datetime.now(UTC)
    start = (
        now.replace(minute=0, second=0, microsecond=0)
        if granularity == "hour"
        else now.replace(hour=0, minute=0, second=0, microsecond=0)
    ) - step * (days * 24 - 1 if granularity == "hour" else days - 1)

    def as_utc(value: str) -> datetime:
        # Buckets come back naive (already converted to UTC in SQL); treating a
        # naive value as local time here would undo that.
        parsed = datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )

    observed = {as_utc(row["bucket"]): row for row in history}

    series = []
    cursor = start
    while cursor <= now:
        row = observed.get(cursor)
        series.append(
            {
                "bucket": cursor.isoformat(),
                "usd": micros_to_float(row["micros"]) if row else 0.0,
                "calls": row["calls"] if row else 0,
                "prompt_tokens": row["prompt_tokens"] if row else 0,
                "completion_tokens": row["completion_tokens"] if row else 0,
            }
        )
        cursor += step

    return {
        "agent_id": agent_id,
        "days": days,
        "granularity": granularity,
        "series": series,
        "totals": {
            **{k: v for k, v in totals.items() if k != "micros"},
            "usd": micros_to_float(totals["micros"]),
            "by_model": [
                {
                    "model": row["model"],
                    "calls": row["calls"],
                    "usd": micros_to_float(row["micros"]),
                }
                for row in totals["by_model"]
            ],
        },
    }


# ----------------------------------------------------------- detail: sessions


@router.get("/{agent_id}/sessions")
async def agent_sessions(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    rows = await budget.list_sessions(agent.team_id, agent.id)
    return {
        "agent_id": agent_id,
        "sessions": [
            {
                "session_id": row["session_id"],
                "status": row["status"],
                "opened_at": row["opened_at"],
                "closed_at": row["closed_at"],
                "close_reason": row["close_reason"],
                "spend_usd": micros_to_float(row["spend_micros"]),
                "limit_usd": micros_to_float(row["limit_micros"]),
                "pct": round(row["pct"], 4),
            }
            for row in rows
        ],
    }


@router.delete("/{agent_id}/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def terminate_session(
    agent_id: int, session_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Close one running session without touching the agent's monthly budget.

    The narrowest intervention available: a single conversation is stopped, and
    the agent immediately opens another and carries on with everything else.
    """
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    await budget.close_session(agent.team_id, session_id, reason="terminated_by_operator")
    await events.emit(
        events.Event(
            type="session.terminated",
            severity="warning",
            scope="agent",
            scope_id=str(agent_id),
            actor="dashboard",
            message=f"Session {session_id} of '{agent.name}' terminated by an operator",
            payload={"agent_id": agent_id, "session_id": session_id},
        ),
        session=session,
    )
    await session.commit()
    return {"session_id": session_id, "status": "closed"}


# ---------------------------------------------------------------- chain edit


def _chain_response(agent: Agent, report: policy.ChainReport) -> schemas.ChainResponse:
    return schemas.ChainResponse(
        agent_id=agent.id,
        chain=report.chain,
        steps=[schemas.ChainStep(**step) for step in report.steps],
        crosses_providers=report.crosses_providers,
        allow_cross_provider=agent.allow_cross_provider,
        allow_substitution=agent.allow_substitution,
        is_custom=agent.policy.is_custom if agent.policy else False,
        warnings=report.warnings,
    )


@router.get("/{agent_id}/chain", response_model=schemas.ChainResponse)
async def get_chain(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> schemas.ChainResponse:
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )
    chain = agent.policy.fallback_chain if agent.policy else [agent.preferred_model]
    try:
        report = policy.validate_chain(
            chain,
            preferred_model=agent.preferred_model,
            allow_cross_provider=agent.allow_cross_provider,
        )
    except policy.ChainInvalid as exc:
        # A stored chain can go stale — a model deactivated, cross-provider
        # permission withdrawn. Report it rather than failing the page, and show
        # what would actually be served.
        usable = policy.usable_chain(
            chain,
            allow_cross_provider=agent.allow_cross_provider,
            allow_substitution=agent.allow_substitution,
        )
        report = policy.ChainReport(chain=chain, warnings=[f"Stale chain: {exc.message}"])
        report.steps = []
        report.crosses_providers = pricing.crosses_providers(chain)
        report.warnings.append(
            f"Only {len(usable)} of {len(chain)} step(s) can currently be served."
        )
    return _chain_response(agent, report)


@router.put("/{agent_id}/chain", response_model=schemas.ChainResponse)
async def set_chain(
    agent_id: int,
    body: schemas.ChainUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> schemas.ChainResponse:
    """Replace the fallback ladder.

    ``chain[0]`` becomes the preferred model: the head of the ladder and the
    model the agent asks for are necessarily the same thing, and storing them
    separately only creates a way for them to disagree.
    """
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    try:
        report = policy.validate_chain(
            body.chain,
            preferred_model=body.chain[0],
            allow_cross_provider=agent.allow_cross_provider,
        )
    except policy.ChainInvalid as exc:
        raise http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_chain",
            exc.message,
            position=exc.position,
        ) from None

    agent.preferred_model = body.chain[0]
    if agent.policy is None:
        session.add(Policy(agent_id=agent.id, fallback_chain=body.chain, is_custom=True))
    else:
        agent.policy.fallback_chain = body.chain
        agent.policy.is_custom = True

    await session.flush()
    security.auth_cache.invalidate_agent(agent.id)
    budget.policy_cache.invalidate(agent.id)
    await events.emit(
        events.Event(
            type="agent.updated",
            severity="info",
            scope="agent",
            scope_id=str(agent.id),
            message=(
                f"Fallback chain for '{agent.name}' set to "
                f"{' → '.join(body.chain)}"
            ),
            payload={"agent_id": agent.id, "chain": body.chain},
        ),
        session=session,
    )
    await session.commit()
    await session.refresh(agent, ["policy"])
    return _chain_response(agent, report)


@router.post("/{agent_id}/chain/auto", response_model=schemas.ChainResponse)
async def regenerate_chain(
    agent_id: int, session: AsyncSession = Depends(get_session)
) -> schemas.ChainResponse:
    """Rebuild the chain from the catalog, discarding hand edits."""
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    chain = await catalog_repo.build_fallback_chain(
        session, agent.preferred_model, cross_provider=agent.allow_cross_provider
    )
    if agent.policy is None:
        session.add(Policy(agent_id=agent.id, fallback_chain=chain, is_custom=False))
    else:
        agent.policy.fallback_chain = chain
        agent.policy.is_custom = False

    await session.flush()
    security.auth_cache.invalidate_agent(agent.id)
    await session.commit()
    await session.refresh(agent, ["policy"])

    report = policy.validate_chain(
        chain,
        preferred_model=agent.preferred_model,
        allow_cross_provider=agent.allow_cross_provider,
    )
    return _chain_response(agent, report)


# -------------------------------------------------------------------- unblock


@router.post("/{agent_id}/unblock", response_model=schemas.AgentOut)
async def unblock_agent(
    agent_id: int,
    body: schemas.UnblockRequest,
    session: AsyncSession = Depends(get_session),
) -> schemas.AgentOut:
    """Human review release for an agent paused by the runaway detector.

    The reason is required and recorded: an automated pause that anyone can
    silently undo is not a control, it is a speed bump.
    """
    agent = await agent_repo.get_agent(session, agent_id)
    if agent is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "agent_not_found", f"No agent {agent_id}."
        )

    await gateway.client.delete(keys.blocked(agent.team_id, agent.id))
    agent.status = "active"
    await session.flush()
    security.auth_cache.invalidate_agent(agent.id)

    await events.emit(
        events.Event(
            type="agent.unblocked",
            severity="warning",
            scope="agent",
            scope_id=str(agent_id),
            actor=body.actor or "dashboard",
            message=f"Agent '{agent.name}' released after human review",
            payload={"agent_id": agent_id, "reason": body.reason},
        ),
        session=session,
    )
    await session.commit()
    return await _to_out(session, agent)
