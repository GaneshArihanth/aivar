"""Seed the model catalog and a fleet matching the scenario in the brief:
twelve agents across four products, all drawing on team budgets.

Run with ``make seed``. Idempotent: re-running updates the catalog and adds
only the teams/agents that are missing.

The generated agent keys are printed once, here, because this is the only
moment they exist. They are stored as HMACs and cannot be recovered later —
rotate a key from the dashboard if you lose one.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.money import usd_to_micros
from app.db.models import ModelCatalog, Team
from app.db.repositories import agents as agent_repo
from app.db.repositories import budgets as budget_repo
from app.db.session import dispose_engine, init_engine, session_scope
from app.redisx import keys
from app.redisx.client import gateway

# Illustrative list prices in micro-dollars per 1,000 tokens. tier_rank orders
# the substitution ladder: a fallback chain steps downward through it.
#
# Each row carries its real endpoint and the environment variable holding its
# key. Nothing is dispatched there unless UPSTREAM_MODE=live — in the default
# mock mode these are recorded but unused, so registering a real endpoint costs
# nothing and going live is one setting away.
#
# provider_kind is the wire format, not the vendor: Gemini and any local
# runtime are reached through their OpenAI-compatible endpoints, so they need
# no adapter of their own.
OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

MODELS = [
    # model_id, provider, display, in/1k µ$, out/1k µ$, rank, kind, base_url, key env, ctx
    ("gpt-4o", "openai", "GPT-4o", 2_500, 10_000, 60,
     "openai", OPENAI_URL, "OPENAI_API_KEY", 128_000),
    ("gpt-4.1", "openai", "GPT-4.1", 2_000, 8_000, 50,
     "openai", OPENAI_URL, "OPENAI_API_KEY", 1_047_576),
    ("gpt-4.1-mini", "openai", "GPT-4.1 mini", 400, 1_600, 30,
     "openai", OPENAI_URL, "OPENAI_API_KEY", 1_047_576),
    ("gpt-4o-mini", "openai", "GPT-4o mini", 150, 600, 20,
     "openai", OPENAI_URL, "OPENAI_API_KEY", 128_000),
    ("gpt-4.1-nano", "openai", "GPT-4.1 nano", 100, 400, 10,
     "openai", OPENAI_URL, "OPENAI_API_KEY", 1_047_576),

    ("claude-opus-4", "anthropic", "Claude Opus 4", 15_000, 75_000, 60,
     "anthropic", ANTHROPIC_URL, "ANTHROPIC_API_KEY", 200_000),
    ("claude-sonnet-4", "anthropic", "Claude Sonnet 4", 3_000, 15_000, 40,
     "anthropic", ANTHROPIC_URL, "ANTHROPIC_API_KEY", 200_000),
    ("claude-haiku-4-5", "anthropic", "Claude Haiku 4.5", 800, 4_000, 15,
     "anthropic", ANTHROPIC_URL, "ANTHROPIC_API_KEY", 200_000),

    # The 2.0 generation was retired by Google: its endpoint answers 404 with
    # "no longer available", so seeding it produced a catalog entry that priced
    # and routed correctly and could never actually be called.
    ("gemini-3.5-flash", "google", "Gemini 3.5 Flash", 300, 2_500, 20,
     "openai", GEMINI_URL, "GEMINI_API_KEY", 1_048_576),
    ("gemini-3.5-flash-lite", "google", "Gemini 3.5 Flash-Lite", 100, 400, 5,
     "openai", GEMINI_URL, "GEMINI_API_KEY", 1_048_576),

    # A local runtime, priced at zero because it costs machine time rather than
    # per-token spend — it still consumes the request budget of nothing, so it
    # makes a natural last resort at the bottom of a chain.
    ("llama3.1:8b", "ollama", "Llama 3.1 8B (local)", 0, 0, 1,
     "openai", "http://localhost:11434/v1", None, 131_072),
]

# Four products, each a team with its own monthly budget.
TEAMS = [
    ("Checkout", 500),
    ("Search", 500),
    ("Support Desk", 300),
    ("Data Platform", 750),
]

# Three agents per team = twelve, as in the brief.
AGENTS = [
    # team, name, monthly $, session $, model, allow_substitution
    ("Checkout", "fraud-screener", 50, 2, "gpt-4o", True),
    ("Checkout", "cart-recovery", 40, 2, "gpt-4o-mini", True),
    ("Checkout", "invoice-parser", 30, 1, "gpt-4.1-nano", True),
    ("Search", "query-rewriter", 60, 2, "gpt-4o-mini", True),
    ("Search", "result-ranker", 80, 3, "gpt-4o", True),
    ("Search", "synonym-miner", 25, 1, "gpt-4.1-nano", True),
    ("Support Desk", "ticket-triage", 45, 2, "claude-sonnet-4", True),
    ("Support Desk", "reply-drafter", 60, 2, "claude-haiku-4-5", True),
    # Substitution off: an agent whose output feeds a contract-sensitive flow
    # and must not silently change model.
    ("Support Desk", "escalation-summariser", 35, 2, "claude-opus-4", False),
    ("Data Platform", "schema-mapper", 90, 4, "gpt-4o", True),
    ("Data Platform", "etl-doctor", 120, 5, "claude-sonnet-4", True),
    ("Data Platform", "anomaly-explainer", 70, 3, "gpt-4o-mini", True),
]


async def seed() -> int:
    init_engine()
    await gateway.connect()
    created_keys: list[tuple[str, str, str]] = []
    month = keys.monthly_period()

    async with session_scope() as session:
        # ---------------------------------------------------------- catalog
        for (
            model_id, provider, display, in_rate, out_rate, rank,
            kind, base_url, key_env, context,
        ) in MODELS:
            row = (
                await session.execute(
                    select(ModelCatalog).where(ModelCatalog.model_id == model_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = ModelCatalog(model_id=model_id)
                session.add(row)
            # Seeded rows are refreshed in place; a model an operator added by
            # hand is left alone (is_custom), so re-seeding never overwrites it.
            elif row.is_custom:
                continue

            row.provider = provider
            row.display_name = display
            row.input_micros_per_1k = in_rate
            row.output_micros_per_1k = out_rate
            row.tier_rank = rank
            row.provider_kind = kind
            row.base_url = base_url
            row.api_key_env = key_env
            row.context_window = context
            row.is_active = True
            row.is_custom = False
        await session.flush()

        # ------------------------------------------------------------ teams
        team_ids: dict[str, int] = {}
        for name, monthly_usd in TEAMS:
            team = (
                await session.execute(select(Team).where(Team.name == name))
            ).scalar_one_or_none()
            if team is None:
                team = Team(name=name)
                session.add(team)
                await session.flush()
            team_ids[name] = team.id

            micros = usd_to_micros(monthly_usd)
            await budget_repo.upsert_budget(
                session,
                scope="team",
                scope_id=team.id,
                period="monthly",
                limit_micros=micros,
            )
            await budget_repo.warm_limit_cache(
                team.id, None, month, team_limit=micros, agent_limit=None
            )

        # ----------------------------------------------------------- agents
        for team_name, name, monthly, per_session, model, allow_sub in AGENTS:
            team_id = team_ids[team_name]
            if await agent_repo.name_is_taken(session, team_id, name):
                continue
            created = await agent_repo.create_agent(
                session,
                name=name,
                team_id=team_id,
                monthly_micros=usd_to_micros(monthly),
                session_micros=usd_to_micros(per_session),
                preferred_model=model,
                allow_substitution=allow_sub,
            )
            await budget_repo.warm_limit_cache(
                team_id,
                created.agent.id,
                month,
                team_limit=None,
                agent_limit=created.monthly_micros,
            )
            created_keys.append((team_name, name, created.raw_api_key))

    await gateway.close()
    await dispose_engine()

    print(f"\nSeeded {len(TEAMS)} teams and {len(AGENTS)} agents "
          f"({len(created_keys)} new).")
    if created_keys:
        print("\nAPI keys — shown once, stored only as HMAC-SHA256:\n")
        width = max(len(f"{t}/{n}") for t, n, _ in created_keys)
        for team, name, key in created_keys:
            print(f"  {f'{team}/{name}':<{width}}  {key}")
        print("\nUse one with:  curl -H 'X-Agent-Key: <key>' ...")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(seed()))
