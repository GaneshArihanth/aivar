"""Model and pricing catalog administration.

Lets an operator register a custom or self-hosted model — an Ollama box, a vLLM
deployment, an Azure deployment name — and set its per-1k cost, without editing
code. Pricing entered here is what the reservation is sized against, so it is
the number that decides whether a call is affordable.

Credentials are referenced by environment-variable *name*. The value is never
accepted by this API, never stored, and never returned; the UI shows only
whether the named variable currently resolves.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import schemas
from app.api.errors import http_error
from app.config import settings
from app.core import providers, upstream
from app.core.money import MICROS_PER_USD, usd_to_micros
from app.core.pricing import pricing
from app.db.models import Agent, ModelCatalog, Policy
from app.db.repositories import catalog as catalog_repo
from app.db.session import get_session, session_scope

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/models", tags=["admin:models"])


def _to_out(row: ModelCatalog) -> schemas.ModelOut:
    return schemas.ModelOut(
        model_id=row.model_id,
        provider=row.provider,
        display_name=row.display_name,
        input_usd_per_1k=row.input_micros_per_1k / MICROS_PER_USD,
        output_usd_per_1k=row.output_micros_per_1k / MICROS_PER_USD,
        tier_rank=row.tier_rank,
        is_active=row.is_active,
        provider_kind=row.provider_kind,
        base_url=row.base_url,
        api_key_env=row.api_key_env,
        # Whether the named variable resolves right now — never its value.
        credential_present=bool(providers.resolve_credential(row.api_key_env)),
        is_custom=row.is_custom,
        context_window=row.context_window,
        notes=row.notes,
        dispatchable=providers.is_dispatchable(row.provider_kind),
    )


async def _reload_pricing(session: AsyncSession) -> None:
    """Refresh the in-process pricing mirror after a catalog change.

    Without this the proxy keeps estimating against the old numbers until the
    next restart — and an operator who has just corrected a price would be
    watching it not take effect.
    """
    await pricing.load(session)


@router.get("", response_model=list[schemas.ModelOut])
async def list_models(
    include_inactive: bool = True, session: AsyncSession = Depends(get_session)
) -> list[schemas.ModelOut]:
    rows = await catalog_repo.list_models(session, active_only=not include_inactive)
    return [_to_out(row) for row in rows]


@router.get("/provider-kinds")
async def list_provider_kinds() -> dict:
    """Wire formats the catalog understands, and which can be dispatched."""
    return {
        "mode": settings.upstream_mode,
        "mock_base_url": settings.upstream_base_url,
        "kinds": [
            {"kind": kind, **meta} for kind, meta in providers.PROVIDER_KINDS.items()
        ],
    }


@router.post("", response_model=schemas.ModelOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: schemas.ModelCreateRequest, session: AsyncSession = Depends(get_session)
) -> schemas.ModelOut:
    existing = await catalog_repo.get_model(session, body.model_id)
    if existing is not None:
        raise http_error(
            status.HTTP_409_CONFLICT,
            "model_exists",
            f"A model with id '{body.model_id}' is already in the catalog.",
            field="model_id",
        )

    row = ModelCatalog(
        model_id=body.model_id,
        provider=body.provider,
        display_name=body.display_name or body.model_id,
        input_micros_per_1k=usd_to_micros(body.input_usd_per_1k),
        output_micros_per_1k=usd_to_micros(body.output_usd_per_1k),
        tier_rank=body.tier_rank,
        is_active=body.is_active,
        provider_kind=body.provider_kind,
        base_url=str(body.base_url).rstrip("/") if body.base_url else None,
        api_key_env=body.api_key_env or None,
        context_window=body.context_window,
        notes=body.notes,
        is_custom=True,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    await _reload_pricing(session)

    log.info("model.created", model_id=row.model_id, provider_kind=row.provider_kind)
    return _to_out(row)


@router.patch("/{model_id}", response_model=schemas.ModelOut)
async def update_model(
    model_id: str,
    body: schemas.ModelUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> schemas.ModelOut:
    row = await catalog_repo.get_model(session, model_id)
    if row is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "model_not_found", f"No model '{model_id}'."
        )

    if body.display_name is not None:
        row.display_name = body.display_name
    if body.provider is not None:
        row.provider = body.provider
    if body.input_usd_per_1k is not None:
        row.input_micros_per_1k = usd_to_micros(body.input_usd_per_1k)
    if body.output_usd_per_1k is not None:
        row.output_micros_per_1k = usd_to_micros(body.output_usd_per_1k)
    if body.tier_rank is not None:
        row.tier_rank = body.tier_rank
    if body.provider_kind is not None:
        row.provider_kind = body.provider_kind
    if body.base_url is not None:
        row.base_url = str(body.base_url).rstrip("/") or None
    if body.api_key_env is not None:
        row.api_key_env = body.api_key_env or None
    if body.context_window is not None:
        row.context_window = body.context_window
    if body.notes is not None:
        row.notes = body.notes

    if body.is_active is not None and body.is_active != row.is_active:
        if not body.is_active:
            users = await _models_in_use(session, model_id)
            if users:
                raise http_error(
                    status.HTTP_409_CONFLICT,
                    "model_in_use",
                    f"'{model_id}' is the preferred model for {len(users)} agent(s): "
                    f"{', '.join(users[:5])}"
                    f"{'…' if len(users) > 5 else ''}. Point them elsewhere first.",
                    agents=users,
                )
        row.is_active = body.is_active

    await session.flush()
    await session.commit()
    await _reload_pricing(session)
    return _to_out(row)


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    model_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    row = await catalog_repo.get_model(session, model_id)
    if row is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "model_not_found", f"No model '{model_id}'."
        )

    users = await _models_in_use(session, model_id)
    if users:
        raise http_error(
            status.HTTP_409_CONFLICT,
            "model_in_use",
            f"'{model_id}' is the preferred model for {len(users)} agent(s): "
            f"{', '.join(users[:5])}{'…' if len(users) > 5 else ''}. "
            "Point them at another model first.",
            agents=users,
        )

    # Historical ledger rows reference this model id by value, not by foreign
    # key, so past spend keeps its provenance after the row is gone.
    await session.delete(row)
    await session.commit()
    await _reload_pricing(session)

    # Chains that referenced it would otherwise silently point at nothing.
    await _prune_from_chains(model_id)
    log.info("model.deleted", model_id=model_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{model_id}/test")
async def test_model(
    model_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Send one tiny probe request, so a misconfigured endpoint is found now."""
    row = await catalog_repo.get_model(session, model_id)
    if row is None:
        raise http_error(
            status.HTTP_404_NOT_FOUND, "model_not_found", f"No model '{model_id}'."
        )
    price = pricing.get(model_id)
    if price is None:
        await _reload_pricing(session)
        price = pricing.require(model_id)
    result = await upstream.probe(price)
    return {"model_id": model_id, "mode": settings.upstream_mode, **result}


# ------------------------------------------------------------------ helpers


async def _models_in_use(session: AsyncSession, model_id: str) -> list[str]:
    rows = await session.execute(
        select(Agent.name).where(
            Agent.preferred_model == model_id, Agent.deleted_at.is_(None)
        )
    )
    return [name for (name,) in rows.all()]


async def _prune_from_chains(model_id: str) -> None:
    async with session_scope() as session:
        policies = list((await session.execute(select(Policy))).scalars())
        for policy in policies:
            chain = policy.fallback_chain or []
            if model_id in chain:
                policy.fallback_chain = [m for m in chain if m != model_id]

