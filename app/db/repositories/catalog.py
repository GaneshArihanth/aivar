"""Model catalog queries and fallback-chain derivation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ModelCatalog


async def list_models(
    session: AsyncSession, *, active_only: bool = True
) -> list[ModelCatalog]:
    stmt = select(ModelCatalog).order_by(
        ModelCatalog.provider, ModelCatalog.tier_rank.desc()
    )
    if active_only:
        stmt = stmt.where(ModelCatalog.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def get_model(session: AsyncSession, model_id: str) -> ModelCatalog | None:
    return (
        await session.execute(
            select(ModelCatalog).where(ModelCatalog.model_id == model_id)
        )
    ).scalar_one_or_none()


async def build_fallback_chain(
    session: AsyncSession, preferred: str, *, cross_provider: bool = False
) -> list[str]:
    """Preferred model first, then progressively cheaper models.

    Same-provider by default: crossing providers mid-flight changes the
    response format, the tokenizer and the data-processing agreement covering
    the request — consequences that should follow an explicit decision rather
    than a budget threshold. Agents with ``allow_cross_provider`` opt into the
    wider ladder.
    """
    head = await get_model(session, preferred)
    if head is None:
        return [preferred]

    stmt = select(ModelCatalog).where(
        ModelCatalog.model_id != preferred,
        ModelCatalog.is_active.is_(True),
    )
    if not cross_provider:
        stmt = stmt.where(ModelCatalog.provider == head.provider)

    rows = list((await session.execute(stmt)).scalars())

    # Blended cost, not tier rank: rank only ranks a vendor against itself.
    def blended(model: ModelCatalog) -> int:
        return (model.input_micros_per_1k + model.output_micros_per_1k) // 2

    head_cost = blended(head)
    cheaper = sorted(
        (row for row in rows if blended(row) < head_cost), key=blended, reverse=True
    )
    return [preferred, *(m.model_id for m in cheaper)]
