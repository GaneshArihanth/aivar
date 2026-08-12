"""Fallback chain validation.

A chain is the ordered list of models the proxy walks when the preferred one no
longer fits the budget. Its shape has to hold two properties, both of which are
easy to get wrong by hand:

1. **Every step must be affordable-or-cheaper than the last.** The chain is
   walked *because* a reservation was refused, so a step that costs more than
   the one that just failed cannot succeed either — it would be dead weight
   that only adds latency before the eventual refusal.
2. **Providers only change with permission.** Crossing a provider boundary
   changes the response schema, the tokenizer and the agreement covering the
   data in the request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.pricing import pricing


class ChainInvalid(Exception):
    def __init__(self, message: str, *, position: int | None = None) -> None:
        self.message = message
        self.position = position
        super().__init__(message)


@dataclass
class ChainReport:
    chain: list[str]
    crosses_providers: bool = False
    warnings: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)


def validate_chain(
    chain: list[str], *, preferred_model: str, allow_cross_provider: bool
) -> ChainReport:
    """Check a proposed chain, or raise :class:`ChainInvalid` explaining why."""
    if not chain:
        raise ChainInvalid("A chain needs at least the preferred model in it.")

    if len(set(chain)) != len(chain):
        seen: set[str] = set()
        for index, model_id in enumerate(chain):
            if model_id in seen:
                raise ChainInvalid(
                    f"'{model_id}' appears twice. Each model can occupy only one "
                    "position in the ladder.",
                    position=index,
                )
            seen.add(model_id)

    if chain[0] != preferred_model:
        raise ChainInvalid(
            f"The chain must start with the agent's preferred model "
            f"('{preferred_model}'), not '{chain[0]}'.",
            position=0,
        )

    report = ChainReport(chain=list(chain))
    previous_cost: int | None = None
    head_provider: str | None = None

    for index, model_id in enumerate(chain):
        price = pricing.get(model_id)
        if price is None:
            raise ChainInvalid(
                f"'{model_id}' is not in the model catalog.", position=index
            )
        if not price.is_active:
            raise ChainInvalid(
                f"'{model_id}' is marked inactive in the catalog, so it can never "
                "serve a request.",
                position=index,
            )

        cost = price.blended_micros_per_1k()
        if head_provider is None:
            head_provider = price.provider
        elif price.provider != head_provider and not allow_cross_provider:
            raise ChainInvalid(
                f"'{model_id}' is a {price.provider} model but the chain starts on "
                f"{head_provider}. Enable cross-provider substitution for this agent "
                "to mix providers.",
                position=index,
            )

        if previous_cost is not None and cost > previous_cost:
            # A warning, not an error. For *budget* substitution this step is
            # dead weight — the ladder is walked because a reservation was
            # refused, so something pricier cannot fit either. But it is not
            # useless in general: if the model above it is later deactivated in
            # the catalog, this entry becomes the head and serves. Refusing
            # outright would also make the operator's ordering our decision
            # rather than theirs.
            report.warnings.append(
                f"'{model_id}' costs more than the step above it, so budget pressure "
                "will never fall through to it — it only serves if the step above is "
                "removed from the catalog."
            )
        elif previous_cost is not None and cost == previous_cost:
            report.warnings.append(
                f"'{model_id}' costs the same as the step above it, so it adds a "
                "retry without adding headroom."
            )

        report.steps.append(
            {
                "model_id": model_id,
                "provider": price.provider,
                "display_name": price.display_name,
                "input_usd_per_1k": price.input_micros_per_1k / 1_000_000,
                "output_usd_per_1k": price.output_micros_per_1k / 1_000_000,
                "blended_micros": cost,
            }
        )
        previous_cost = cost

    report.crosses_providers = pricing.crosses_providers(chain)
    if report.crosses_providers:
        report.warnings.append(
            "This chain crosses providers. Responses will be translated back into "
            "the OpenAI schema, but tokenization and model behaviour differ between "
            "vendors."
        )
    if len(chain) == 1:
        report.warnings.append(
            "With a single entry there is nothing to fall back to — the agent will "
            "be refused outright when its budget runs low."
        )
    return report


def usable_chain(
    chain: list[str], *, allow_cross_provider: bool, allow_substitution: bool
) -> list[str]:
    """Filter a stored chain down to what can actually be served right now.

    Applied per request rather than at save time, because the catalog and the
    agent's flags move independently of the chain: a model can be deactivated,
    or cross-provider permission withdrawn, long after the chain was written.
    Filtering here keeps a stale entry from wasting a reservation attempt.
    """
    if not chain:
        return []
    if not allow_substitution:
        return chain[:1]

    head = pricing.get(chain[0])
    usable: list[str] = []
    for model_id in chain:
        price = pricing.get(model_id)
        if price is None or not price.is_active:
            continue
        if (
            head is not None
            and not allow_cross_provider
            and price.provider != head.provider
        ):
            continue
        usable.append(model_id)
    return usable
