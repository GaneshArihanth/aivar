"""Model pricing and cost estimation.

The pre-flight estimate is deliberately the **worst case**: the full prompt at
the input rate, plus the entire ``max_tokens`` ceiling at the output rate. That
is the most this call could possibly cost, and it is what gets held.

Estimating the *likely* cost instead would be more flattering and would break
the guarantee. If you hold the average and 200 concurrent requests all come in
just under the limit, the ones that run long collectively overshoot — and the
overshoot is discovered only at settle time, which is exactly the post-hoc
discovery this system replaces. Holding the ceiling means the budget cannot be
exceeded even if every in-flight request runs to its maximum length.

The difference between the hold and reality is refunded at settle, so agents
are charged what they actually used; the ceiling only constrains what may be
*in flight* at once.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import cost_micros
from app.db.repositories import catalog as catalog_repo

log = structlog.get_logger(__name__)

# Used when a request omits max_tokens. Providers default to "until the model
# stops", which has no finite ceiling, so we impose one for reservation
# purposes and note it on the ledger row.
DEFAULT_MAX_TOKENS = 1024


@dataclass(frozen=True)
class ModelPrice:
    model_id: str
    provider: str
    display_name: str
    input_micros_per_1k: int
    output_micros_per_1k: int
    tier_rank: int
    provider_kind: str = "openai"
    base_url: str | None = None
    api_key_env: str | None = None
    is_active: bool = True

    def estimate_micros(self, prompt_tokens: int, max_tokens: int) -> int:
        """Worst-case cost of a call: full prompt + the whole output ceiling."""
        return cost_micros(prompt_tokens, self.input_micros_per_1k) + cost_micros(
            max_tokens, self.output_micros_per_1k
        )

    def actual_micros(self, prompt_tokens: int, completion_tokens: int) -> int:
        return cost_micros(prompt_tokens, self.input_micros_per_1k) + cost_micros(
            completion_tokens, self.output_micros_per_1k
        )

    def blended_micros_per_1k(self) -> int:
        """Rough single number for ordering models by cost."""
        return (self.input_micros_per_1k + self.output_micros_per_1k) // 2


class PricingCache:
    """In-process mirror of ``model_catalog``.

    Pricing changes rarely and is read on every single request, so it is held
    in memory and refreshed explicitly rather than joined per call.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ModelPrice] = {}

    async def load(self, session: AsyncSession) -> None:
        rows = await catalog_repo.list_models(session, active_only=False)
        self._by_id = {
            row.model_id: ModelPrice(
                model_id=row.model_id,
                provider=row.provider,
                display_name=row.display_name,
                input_micros_per_1k=row.input_micros_per_1k,
                output_micros_per_1k=row.output_micros_per_1k,
                tier_rank=row.tier_rank,
                provider_kind=row.provider_kind,
                base_url=row.base_url,
                api_key_env=row.api_key_env,
                is_active=row.is_active,
            )
            for row in rows
        }
        log.info("pricing.loaded", models=len(self._by_id))

    def replace(self, models: list[ModelPrice]) -> None:
        """Swap the whole catalog in one go, without touching the database.

        Exists for tests that exercise chain and pricing logic in isolation;
        the running app always populates this from PostgreSQL via ``load``.
        """
        self._by_id = {m.model_id: m for m in models}

    def get(self, model_id: str) -> ModelPrice | None:
        return self._by_id.get(model_id)

    def require(self, model_id: str) -> ModelPrice:
        price = self._by_id.get(model_id)
        if price is None:
            raise KeyError(f"Model {model_id!r} is not in the pricing catalog")
        return price

    def cheaper_alternatives(
        self, model_id: str, *, cross_provider: bool = False
    ) -> list[ModelPrice]:
        """Models that cost less than ``model_id``, most capable first.

        Ordered by blended cost rather than tier rank once providers are mixed:
        rank is only meaningful within a vendor's own line-up, so comparing
        Anthropic's rank to OpenAI's would order the ladder by nothing in
        particular. Within one provider the two orderings agree.
        """
        head = self._by_id.get(model_id)
        if head is None:
            return []

        candidates = [
            m
            for m in self._by_id.values()
            if m.model_id != head.model_id
            and m.is_active
            and (cross_provider or m.provider == head.provider)
            and m.blended_micros_per_1k() < head.blended_micros_per_1k()
        ]
        return sorted(candidates, key=lambda m: m.blended_micros_per_1k(), reverse=True)

    def crosses_providers(self, chain: list[str]) -> bool:
        providers = {
            self._by_id[m].provider for m in chain if m in self._by_id
        }
        return len(providers) > 1

    def model_ids(self) -> list[str]:
        return sorted(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)


pricing = PricingCache()
