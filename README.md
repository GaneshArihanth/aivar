# Agent Budget Controller

An API proxy that enforces LLM spend limits **per team, per agent and per
session — before a request is dispatched**, not after the bill arrives.

Point your agents at this instead of the provider. Every call is metered,
budgeted and — when the money runs out — refused, rerouted to a cheaper model,
or paused for human review. Nothing reaches a provider that the budget cannot
pay for.

```
agent ──▶ Agent Budget Controller ──▶ OpenAI / Anthropic / Gemini / Ollama / vLLM
             │
             └── reject · downgrade · pause · freeze
```

---

## Contents

1. [The problem](#the-problem)
2. [The central design problem](#the-central-design-problem)
3. [Quick start](#quick-start)
4. [Using it from an agent](#using-it-from-an-agent)
5. [The dashboard](#the-dashboard)
6. [Enforcement model](#enforcement-model)
7. [Features in detail](#features-in-detail)
8. [API reference](#api-reference)
9. [Data model](#data-model)
10. [Redis keyspace](#redis-keyspace)
11. [The Lua scripts](#the-lua-scripts)
12. [Code layout](#code-layout)
13. [Configuration](#configuration)
14. [Testing](#testing)
15. [Operations](#operations)
16. [Design decisions and trade-offs](#design-decisions-and-trade-offs)
17. [Known limitations](#known-limitations)
18. [Troubleshooting](#troubleshooting)

---

## The problem

An engineering team runs twelve agents across four products against a shared
LLM budget. One agent enters a recursive loop and makes 50,000 API calls
overnight. Nobody finds out until the monthly invoice arrives.

The failure was not poor monitoring. It was that **enforcement lived in
billing, which is post-hoc, instead of in the request path, which is not.**
Dashboards, alerts and cost reports all describe money that is already spent.

This project puts the control where the request is.

---

## The central design problem

**The cost of an LLM call is unknown until it completes, but the allow/deny
decision has to happen before it starts.**

The obvious middleware does:

```
read counter → compare to limit → forward → add actual cost
```

That is exactly the design that permits the incident above. Between the read
and the write there is a window in which every concurrent request sees the same
stale total. Two hundred requests can each observe "79% consumed" and each
conclude it has room. The budget is blown by arithmetic, not by malice.

So the system is modelled as an **authorization-hold ledger** — the pattern a
card terminal uses when it puts a hold on your card at a hotel:

| Phase | What happens |
|---|---|
| **Reserve** | Compute the *worst case* cost — prompt tokens at the input rate, plus the entire `max_tokens` ceiling at the output rate. Atomically check and hold that amount against session, agent **and** team together. All three, or none. |
| **Forward** | Proxy the call to the provider. |
| **Settle** | Read the real usage from the response and atomically adjust by `(actual − estimate)` — almost always a refund. Write an immutable ledger row. |
| **Reap** | A background janitor releases holds orphaned by crashes and timeouts, so a dead request cannot hold budget hostage. |

Two properties follow, and both are load-bearing:

**The whole decision lives in one Redis Lua script.** Redis executes a script
to completion without interleaving, so the check and the increment cannot be
separated. Python never compares a budget to a limit — any comparison it made
would be against a value another worker may already have changed.

**The three scopes move together.** Incrementing the session counter and *then*
discovering the agent is exhausted would burn session budget on a request that
never ran. `reserve.lua` commits all three or refuses.

The result: over-spend is structurally impossible rather than statistically
unlikely. The test that proves it fires 500 concurrent reservations at a budget
with room for exactly 100, and asserts **exactly 100** are admitted — not
"roughly 100", not "no more than 105".

---

## Quick start

Requires macOS with Homebrew. Python 3.14 is used here; 3.11+ works.

```bash
make setup                      # venv, dependencies, Redis + PostgreSQL via brew, .env
make infra-up                   # start Redis (project config) and PostgreSQL
make db-create migrate seed     # schema + 4 teams, 12 agents, 11 models
make demo                       # mock provider + proxy, then walk every scenario
```

Then open **http://127.0.0.1:8000/dashboard**.

`make seed` prints twelve API keys **once**. They are stored only as HMACs and
cannot be recovered — rotate a key from the dashboard if you lose one.

Deploying to AWS from GitHub with Terraform: **[DEPLOY.md](DEPLOY.md)**.

### Zero-infrastructure mode

No Redis, no PostgreSQL, same code paths — `fakeredis` and SQLite are swapped in
behind the same interfaces:

```bash
DEV_MODE=embedded make dev
```

### Everyday commands

```bash
./scripts/devctl.sh start|stop|restart|status|logs   # mock (:9000) + proxy (:8000)
make test                 # the full suite
make test-criteria        # only the six success criteria
make reconcile            # compare Redis counters against the ledger
python -m loadgen.main --scenario mixed --calls 20   # generate traffic
```

---

## Using it from an agent

Point any OpenAI-compatible client at the proxy:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "X-Agent-Key: sk-agent-…" \
  -H "X-Session-Id: session-42" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"max_tokens":200}'
```

**Authentication** — `X-Agent-Key: sk-agent-…` or `Authorization: Bearer sk-agent-…`.

**Sessions** — `X-Session-Id` (or `session_id` in the body) names the session the
per-session budget applies to. Omit it and each call gets its own session, which
makes the session cap a de-facto per-call cap — a safe default rather than an
unbounded one.

### Every response discloses what the budget layer did

```
X-Budget-Request-Id:          9f2c…                 join key to the ledger row
X-Session-Id:                 session-42
X-Budget-Model-Requested:     gpt-4o
X-Budget-Model-Served:        gpt-4o-mini           ← substitution happened
X-Budget-Substitution-Reason: agent_budget_pressure_92pct
X-Budget-Cost-USD:            0.001838              this call, full precision
X-Budget-Agent-Remaining-USD: 4.03
X-Budget-Team-Remaining-USD:  312.50
X-Budget-Session-Remaining-USD: 1.94                omitted when uncapped
```

### What the enforcement layer returns

| Condition | Status | `error.type` |
|---|---|---|
| Within budget | `200` | — |
| Under pressure, substitution allowed | `200` | — (served model disclosed in headers) |
| Agent or team monthly budget exhausted | `402` | `budget_exhausted` |
| Session budget exhausted (session closed, agent unaffected) | `402` | `session_budget_exhausted` |
| Session already closed | `402` | `session_closed` |
| Agent paused by the runaway detector | `423` | `agent_paused_runaway` |
| Rate limit reached (RPM or TPM) | `429` | `rate_limited` |
| Dispatch frozen, globally or per team | `503` | `dispatch_frozen` |
| Model not in the pricing catalog | `422` | `model_not_found` |
| Model cannot be dispatched as configured | `502` | `model_not_dispatchable` |
| Malformed body / unusable `max_tokens` | `400` | `invalid_request_body` / `invalid_max_tokens` |
| Unknown or revoked key | `401` | `invalid_api_key` |
| Provider failed (hold released in full) | `502` / `504` | `upstream_error` / `upstream_timeout` |
| Redis unreachable, fail mode `closed` | `503` | — |

Every error uses one envelope, whichever layer produced it:

```json
{"error": {"type": "budget_exhausted", "scope": "agent",
  "scope_id": "7", "scope_name": "fraud-screener",
  "limit_usd": "50.00", "consumed_usd": "50.00",
  "period": "2026-08", "resets_at": "2026-09-01T00:00:00+00:00",
  "message": "Agent monthly budget exhausted. Request rejected before dispatch."}}
```

---

## The dashboard

Four pages, served as static files with **no build step** — native ES modules,
hand-rolled SVG charts, no bundler and no CDN. Live data arrives over SSE with a
3-second poll as a safety net.

### Dashboard (`#/`)

- **Fleet tiles** — total spend vs committed budget, agent count, agents over
  80%, and **Paused / blocked**. The last is clickable: it opens a review queue
  listing every paused agent with why it tripped and a resume action per row.
- **Live spend** — teams, each with a budget bar and its agents. Per-agent bar,
  spend, percentage, and a `⋯` menu (details, edit budget, fallback chain,
  toggle substitution, rotate key, pause/resume, delete).
- **Event feed** — live, keyed and incremental. Click or press Enter on any
  event to open the detail overlay.

### Agents (`#/agents`)

The full management table: filter by name/team/model, sort by spend, %, last
hour, name or team, filter to "needs attention". Inline substitution switch and
per-row pause / chain / budget / more.

### Agent detail (`#/agents/:id`)

- **Spend history** — 7 / 30 / 90 day chart, hourly for a week and daily beyond.
  The series is dense, so a quiet hour looks quiet.
- **Token economics** — input vs output split, average and slowest latency,
  substitution count, and cost broken down by the model actually served.
- **Live sessions** — every open session with its spend, and a terminate action.
- **Controls** — edit monthly and session budgets, boost, rate limits, fallback
  chain, rotate key, pause, delete.

### Teams (`#/teams`)

Team cards with budget bars and agents as draggable chips. Create teams, edit
caps (warning first if the new cap is below current spend), freeze a team,
delete empty ones, and **drag an agent onto another team to move it** — or use
the per-chip move menu, since a drag-only control cannot be operated by
keyboard.

### Models (`#/models`)

The pricing catalog. Register a custom or self-hosted model, set input/output
cost per 1k tokens, choose the wire format, test connectivity, edit, delete.
Shows which environment variable each model needs and whether it currently
resolves — never the value.

### Event detail overlay

Clicking any event opens a drawer with the whole story of that decision:

> **SESSION LIMIT EXCEEDED** — $0.0018 of $0.0020
> *This call reserved $0.0002, which did not fit in what was left.*
>
> Requested `gpt-4o` · Served `gpt-4o-mini` (substituted) · 143 in / 892 out
> tokens · 214 ms · Reserved $0.0091 · Charged $0.0006 · Refunded $0.0085

Joined to the exact ledger row via the `request_id` carried in the event
payload, with a deep link to the agent. **No prompt or completion text** — none
is stored.

---

## Enforcement model

### The three scopes

| Scope | Period | Meaning |
|---|---|---|
| **Team** | monthly | A department's ceiling. Every agent on the team draws from it. |
| **Agent** | monthly | One agent's allowance. |
| **Session** | per session | A cap on a single conversation. Configured per agent, applied to each session independently. |

All three are checked in one atomic operation, broadest first — a team breach is
the one an operator must act on, and no cheaper model or fresh session escapes
it.

### Thresholds

| Threshold | Default | Behaviour |
|---|---|---|
| `warn_threshold` | 0.80 | Emit a warning event — **exactly once** per scope per period, on the crossing. Guarded by `SETNX`, so it marks the event rather than becoming a per-request flood. |
| `substitution_threshold` | 0.90 | Under pressure: step down the fallback chain to a cheaper model rather than refusing work the budget can still pay for. |
| `hard_threshold` | 1.00 | Refuse. The request never leaves the process. |

All three are per-budget-row, so an individual agent or team can differ.

### Request lifecycle

```
authenticate  →  estimate  →  RESERVE ⇄ (substitute)  →  forward  →  SETTLE
  HMAC lookup     tokens ×      reserve.lua              provider     settle.lua
  (cached)        rate          all-or-nothing                        refund diff
                                                                         │
                          reject ──┐                              velocity check
                                   ▼                                     │
                         ledger row + event  ◀───────────────────────────┘
```

### What "refused" costs

Nothing. A rejected request never reaches a provider — the test for the 100%
hard block asserts on the **mock provider's own request counter**, not just the
402, because a response claiming refusal while the call was still dispatched
would be worse than no control at all.

---

## Features in detail

### Model catalog and providers

Eleven models across four providers are seeded with their real endpoints:

| Provider | Models | Endpoint |
|---|---|---|
| OpenAI | gpt-4o, gpt-4.1, gpt-4.1-mini, gpt-4o-mini, gpt-4.1-nano | `api.openai.com/v1` |
| Anthropic | claude-opus-4, claude-sonnet-4, claude-haiku-4-5 | `api.anthropic.com/v1` |
| Google | gemini-2.0-flash, gemini-2.0-flash-lite | Gemini's OpenAI-compat endpoint |
| Local | llama3.1:8b | `localhost:11434/v1`, priced at zero |

**Wire formats.** `provider_kind` decides how a request is translated:

- `openai` — the Chat Completions schema. Covers OpenAI, Azure OpenAI, Ollama,
  vLLM, LM Studio, Groq, Together, Fireworks and Gemini's compatibility
  endpoint. Most self-hosted deployments need no new code, just a base URL.
- `anthropic` — the native Messages API. Three differences matter and are all
  handled: the system prompt is a top-level field rather than a message,
  `max_tokens` is required, and usage is `input_tokens`/`output_tokens`. The
  reply is normalised back into the OpenAI schema so callers never see it.
- `bedrock` / `vertex` — registerable for pricing and policy, but **not
  dispatched directly**: both need request signing (SigV4, Google auth) rather
  than a bearer token. Route them through any OpenAI-compatible gateway. The UI
  says so rather than offering a setting that silently fails.

**Mock vs live.** `UPSTREAM_MODE=mock` (the default) sends everything to the
mock provider whatever the catalog says, so registering a real endpoint costs
nothing. `UPSTREAM_MODE=live` uses each model's own base URL and credential.
It is deliberately **not** "go live whenever a key happens to be present" — a
tool built to prevent surprise spend must not start spending real money because
an unrelated `OPENAI_API_KEY` was exported in a shell.

**Credentials** are referenced by environment-variable *name*. The value is
never accepted by the API, never stored, and never returned; the UI shows only
whether the named variable resolves.

**Test button** always probes the model's *own* endpoint, even in mock mode —
a test that answers about the mock while you are asking about your Ollama box
would report success for a machine that is switched off.

### Fallback chains (model substitution)

A chain is the ordered ladder the proxy walks when the preferred model no
longer fits:

```
gpt-4o → claude-sonnet-4 → gpt-4o-mini → gemini-2.0-flash-lite
```

- Derived automatically from the catalog (same provider, descending cost), or
  built by hand in the drag-to-reorder editor.
- **Cross-provider is per-agent opt-in** (`allow_cross_provider`, default off).
  Crossing providers changes the response schema, the tokenizer and the
  agreement covering the data in the request — consequences that should follow
  a decision, not a budget threshold.
- Walked on **pressure or exhaustion**. Hard-blocking while an affordable model
  exists would refuse work the budget can still pay for.
- The session is closed only once nothing cheaper is left to try — closing on
  the first over-budget estimate would end a session a cheaper model could have
  continued.
- Validation refuses: duplicates, unknown models, inactive models, a head that
  is not the preferred model, and cross-provider steps without permission. It
  *warns* (rather than refusing) when a step costs more than the one above it —
  dead weight for budget pressure, but it becomes the head if the model above
  is later deactivated.
- Stale entries are filtered per request, so a deactivated model never wastes a
  reservation attempt.

### Runaway detector

The scenario from the brief. A monthly total cannot catch a loop — by the time
the total looks alarming the money is gone. What separates a loop from ordinary
work is **rate**.

- Spend is bucketed per minute; the last 60 buckets are summed on every settle.
- More than **20% of the monthly budget inside one hour** trips the breaker.
- Tripping pauses the agent (`423`) and writes a critical event.
- **No TTL.** The pause is cleared only through the audited unblock endpoint. An
  automatic cool-off would let a looping agent resume looping — the pause exists
  so that a person looks at it.
- The threshold is **per agent** (`runaway_hourly_fraction`; `0` disables it). A
  single global fraction misbehaves at both extremes: a nightly batch job that
  legitimately does a month's work in one window would be paused every night,
  and for a very small budget 20% is a few cents so any burst looks like a loop.
- 60 one-minute counters, not a sorted set of individual calls: O(60) to read
  regardless of traffic, and it cannot grow without bound during exactly the
  runaway it exists to detect.

### Sessions

Tracked in Redis with a TTL and indexed per agent, so they can be listed and
terminated. Terminating one closes that conversation and nothing else — the
agent's monthly budget is untouched and it opens a new session immediately.

A session cap of zero (or a missing per-session budget row) means **no session
cap**, not "spend nothing". Read the other way round, an agent whose row was
missing would have every request refused with "session budget exhausted: $0.00
of $0.00" — bricked, and misleadingly so.

### Teams and transfers

Moving an agent between teams is **not** a `team_id` update. Every counter key
carries a `{team:N}` hash tag, so a naive move would strand the old counter and
start a fresh one at zero — making reassignment a way to reset a budget.

`move_agent.lua` carries the live state across, with a deliberate split:

| | |
|---|---|
| Agent's monthly spend | **moves** — it is the agent's consumption and must keep constraining it |
| Team totals | **stay** — the old team did incur that spend; erasing it would misstate what a department consumed |
| Runaway pause + velocity window | **move** — otherwise reassignment would quietly release an agent nobody reviewed |
| Open sessions | left to expire — team-tagged and short-lived |

The ledger keeps each call's original `team_id`, so `make reconcile` reproduces
exactly this split from PostgreSQL.

### Live controls

All three are evaluated **inside `reserve.lua`**. A freeze checked in Python
would leave a window between the check and the increment, and under concurrency
that window is where requests slip through.

- **Freeze** — global (header button) or per team. Returns `503` with
  `Retry-After`. It stops every *reservation* from the instant it is set; it
  cannot recall a request already handed to a provider, because nothing can. No
  expiry: an incident switch that silently un-flips itself is worse than none.
- **Budget boost** — grants extra budget for the current period *without
  changing the monthly limit*, so the baseline is still there tomorrow. Requires
  a typed reason, is recorded in `budget_grants`, expires (and never outlives
  the period), and **accumulates** rather than replaces so pressing the button
  twice during an incident does not undo the first press.
- **Rate limits** — RPM and TPM sliders per agent. Pacing, not spend: a cheap
  model can still hammer a provider into throttling everyone. A rate refusal
  consumes no budget and leaves no hold. Counted only on commit, so a request
  refused for budget does not eat rate allowance it never used.

---

## API reference

40 endpoints. `/admin/*` is unauthenticated — see the security note under
[Known limitations](#known-limitations).

### Proxy

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/chat/completions` | Budget-enforced chat completions |

### Status and observability

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/budget/status` | Live spend for every team and agent |
| `GET` | `/v1/budget/events` | Recent budget events |
| `GET` | `/v1/budget/calls?limit=` | Recent ledger rows |
| `GET` | `/v1/budget/calls/{request_id}` | One ledger row — the event overlay's join |
| `GET` | `/events/stream` | SSE feed (Redis pub/sub fan-out) |
| `GET` | `/admin/reconcile` | Redis counters vs the PostgreSQL ledger |
| `GET` | `/health` | Redis + PostgreSQL health |

### Agents

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/agents` | Create an agent, its budgets and policy; **returns the raw key once** |
| `GET` | `/admin/agents` | List all live agents |
| `GET` | `/admin/agents/{id}` | One agent's full configuration |
| `PATCH` | `/admin/agents/{id}` | Name, budgets, model, substitution, cross-provider, status, RPM/TPM, runaway fraction |
| `DELETE` | `/admin/agents/{id}` | Soft delete — revokes the key, keeps the history |
| `POST` | `/admin/agents/{id}/rotate-key` | New key, old one revoked; **shown once** |
| `POST` | `/admin/agents/{id}/unblock` | Audited release after a runaway pause (reason required) |
| `POST` | `/admin/agents/{id}/move` | Reassign to another team, carrying live state |
| `GET` | `/admin/agents/{id}/history?days=7\|30\|90` | Dense spend series + token/latency totals |
| `GET` | `/admin/agents/{id}/sessions` | Live and recent sessions |
| `DELETE` | `/admin/agents/{id}/sessions/{sid}` | Terminate one session |
| `GET` | `/admin/agents/{id}/chain` | Current fallback chain, with warnings |
| `PUT` | `/admin/agents/{id}/chain` | Replace the chain (head becomes the preferred model) |
| `POST` | `/admin/agents/{id}/chain/auto` | Rebuild from the catalog, discarding hand edits |
| `POST` | `/admin/agents/{id}/boost` | Grant extra budget for this period |
| `GET` | `/admin/agents/{id}/boost` | Active boost and recent grants |
| `DELETE` | `/admin/agents/{id}/boost` | Revoke the boost |

### Teams

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/teams` | Teams with budgets and agent counts |
| `POST` | `/admin/teams` | Create a team with a monthly cap |
| `PATCH` | `/admin/teams/{id}` | Rename or change the cap |
| `DELETE` | `/admin/teams/{id}` | Delete an empty team |
| `POST` | `/admin/teams/{id}/freeze` | Freeze one team |
| `DELETE` | `/admin/teams/{id}/freeze` | Resume it |

### Models

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/models` | The catalog, with pricing and credential status |
| `POST` | `/admin/models` | Register a custom or self-hosted model |
| `PATCH` | `/admin/models/{id}` | Edit pricing, endpoint, wire format, active flag |
| `DELETE` | `/admin/models/{id}` | Remove (refused while in use; pruned from chains) |
| `POST` | `/admin/models/{id}/test` | Probe the model's own endpoint |
| `GET` | `/admin/models/provider-kinds` | Supported wire formats and dispatchability |

### Global controls

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/freeze` | Freeze state, global and per team |
| `POST` | `/admin/freeze` | Freeze all dispatch (reason required) |
| `DELETE` | `/admin/freeze` | Resume |

---

## Data model

PostgreSQL is the **log of record**: configuration plus one immutable
`call_ledger` row per settled call. Every Redis counter can be rebuilt from it,
so losing Redis is a recoverable incident rather than a budget reset.

Nine tables, six migrations.

### `teams`
`id · name · created_at · frozen_at · frozen_reason`

### `agents`
`id · team_id · name · key_hash · key_prefix · key_created_at · preferred_model ·
allow_substitution · allow_cross_provider · rpm_limit · tpm_limit · status ·
created_at · deleted_at`

- `key_hash` is HMAC-SHA256 and unique-indexed — authentication is an O(1)
  lookup by value.
- Partial unique index on `(team_id, name) WHERE deleted_at IS NULL`, so
  soft-deleting an agent frees its name for reuse.
- `status ∈ active | paused | blocked | deleted`. `blocked` is owned by the
  runaway detector and cannot be set by hand.

### `budgets`
`id · scope · scope_id · limit_micros · period · warn_threshold ·
hard_threshold · substitution_threshold · runaway_hourly_fraction · updated_at`

- `scope ∈ team | agent | session`, `period ∈ monthly | daily | per_session`.
- A `session`-scoped row is a *policy on an agent* ("$2 per session"), not a row
  per session instance, so `scope_id` holds the agent id.

### `model_catalog`
`model_id · provider · display_name · input_micros_per_1k ·
output_micros_per_1k · tier_rank · is_active · provider_kind · base_url ·
api_key_env · is_custom · context_window · notes · created_at`

### `policies`
`agent_id · fallback_chain (JSONB) · is_custom`

`is_custom` marks a hand-edited chain, so it is not silently regenerated when
the preferred model changes.

### `sessions`
`id · agent_id · team_id · limit_micros · opened_at · closed_at · close_reason`

### `call_ledger`
`id · request_id · agent_id · team_id · session_id · period · requested_model ·
served_model · substituted · prompt_tokens · completion_tokens ·
estimated_micros · actual_micros · decision · latency_ms · created_at`

- `request_id` is unique — a retried settle cannot double-charge.
- `decision ∈ allowed | substituted | rejected_budget | rejected_session |
  rejected_runaway | upstream_error`.
- Indexed on `(agent_id, created_at)`, `(team_id, created_at)` and `period`.

### `budget_grants`
`id · agent_id · micros · period · reason · actor · expires_at · created_at`

One-time boosts, kept as their own rows rather than folded into the limit — a
boost is a temporary, attributable exception, and merging it would erase both
that it happened and the number to return to.

### `budget_events`
`id · created_at · severity · scope · scope_id · type · actor · payload (JSONB) ·
message`

---

## Redis keyspace

Every key carries a `{team:N}` hash tag. On standalone Redis that is cosmetic,
but it means the scope keys touched by one `reserve.lua` call always hash to the
same slot — so the design stays correct under Redis Cluster, where a multi-key
script spanning slots is rejected outright.

All monetary values are integer micro-dollars.

| Key | Type | Purpose |
|---|---|---|
| `bc:{team:N}:spend:team:<period>` | int | Team spend, including outstanding holds |
| `bc:{team:N}:spend:agent:<id>:<period>` | int | Agent spend |
| `bc:{team:N}:spend:session:<sid>` | int | Session spend (TTL) |
| `bc:{team:N}:limit:team:<period>` | int | Cached team limit |
| `bc:{team:N}:limit:agent:<id>:<period>` | int | Cached agent limit |
| `bc:{team:N}:session:<sid>` | hash | Session status, limit, opened/closed |
| `bc:{team:N}:sessions:agent:<id>` | zset | Session index, scored by open time |
| `bc:{team:N}:hold:<request_id>` | hash | One outstanding reservation |
| `bc:holds:pending` | zset | All holds by expiry — the reaper's range query |
| `bc:{team:N}:warned:<scope>:<id>:<period>:80` | flag | Makes the 80% warning fire once |
| `bc:{team:N}:vel:agent:<id>:<minute>` | int | Velocity bucket (TTL 90 min) |
| `bc:{team:N}:blocked:agent:<id>` | hash | Runaway breaker — no TTL |
| `bc:{team:N}:boost:agent:<id>:<period>` | int | One-time grant |
| `bc:{team:N}:rpm:agent:<id>:<minute>` | int | Requests this minute |
| `bc:{team:N}:tpm:agent:<id>:<minute>` | int | Tokens this minute |
| `bc:freeze:global` | hash | Global kill switch (untagged — owned by no team) |
| `bc:{team:N}:freeze:team` | hash | Per-team kill switch |
| `bc:events` | pub/sub | Fan-out to every worker's SSE clients |

---

## The Lua scripts

Enforcement lives entirely here. Python assembles arguments and translates
return values; it never decides.

### `reserve.lua` — the enforcement decision

17 keys, 20 arguments, one atomic execution. In order:

1. **Runaway breaker** — a paused agent is refused before anything else.
2. **Kill switches** — global, then team.
3. **Limits** — a missing limit is never read as "unlimited"; it returns
   `LIMIT_MISSING` so the caller reloads from PostgreSQL and retries.
4. **Boost** — folded into the agent's effective ceiling for this period.
5. **Session state** — closed sessions refused; a zero limit means uncapped.
6. **Hard limits** — team, then agent, then session. Broadest first.
7. **Pressure** — under the limit but close: tell the caller so it can step down
   the chain. Skipped on the final attempt.
8. **Rate limits** — RPM then TPM, checked last among refusals so a request over
   budget is told about the budget.
9. **Commit** — all three scopes, the session index, the hold, and the rate
   counters move together.
10. **Warnings** — `SETNX`-guarded, so 80% fires exactly once.

Returns `OK | PRESSURE | EXHAUSTED | SESSION_EXHAUSTED | SESSION_CLOSED |
BLOCKED | LIMIT_MISSING | FROZEN | RATE_LIMITED`.

### `settle.lua`
Replaces the hold with the real cost, refunding the difference across all three
scopes. Idempotent by construction: the hold is deleted in the same execution
that applies the adjustment, so a retry finds nothing to do. Counters are
clamped at zero — a negative total is always a bug and would grant free budget.

### `release.lua`
Returns a hold in full, charging nothing. Used for provider errors, timeouts,
client disconnects and reaped holds. Without it, an agent hitting a broken
provider would exhaust its month without generating a single token.

### `velocity.lua`
Increments the current minute bucket, sums the sliding hour, and trips the
runaway breaker when the threshold is crossed.

### `move_agent.lua`
Carries an agent's spend, runaway pause and velocity window to a new team's
namespace while leaving team totals alone. The one operation that spans two hash
tags — atomic on standalone Redis, would need a two-phase migration under
Cluster.

---

## Code layout

```
app/
  main.py              app factory, lifespan, routers, error envelope, static
  config.py            pydantic-settings
  logging_setup.py     structlog + API-key redaction

  redisx/
    client.py          connection (blocking pool) + Lua registration
    keys.py            THE single source of truth for key naming
    scripts/*.lua      reserve · settle · release · velocity · move_agent

  core/
    budget.py          reserve/settle/release orchestration, policy cache
    money.py           integer micro-dollar arithmetic
    pricing.py         in-memory catalog mirror, cost estimation
    policy.py          fallback chain validation and runtime filtering
    providers.py       request/response adapters per wire format
    upstream.py        per-model HTTP routing, pooled clients, probe
    runaway.py         velocity window and circuit breaker
    security.py        key generation, HMAC hashing, auth cache
    tokenizer.py       heuristic / tiktoken
    events.py          PostgreSQL audit + Redis pub/sub

  api/
    proxy.py           POST /v1/chat/completions
    admin_agents.py    agent CRUD, chain, history, sessions, move
    admin_config.py    teams
    admin_models.py    catalog
    admin_controls.py  freeze, boost
    status.py          status, calls, events, reconcile
    stream.py          SSE
    schemas.py         Pydantic models
    errors.py          one error envelope

  db/
    models.py          SQLAlchemy ORM
    session.py         async engine/session
    repositories/      agents · budgets · catalog · ledger

  workers/
    reaper.py          releases orphaned holds
    reconciler.py      Redis ⇄ ledger drift and rebuild

  static/              dashboard: index.html, css/, js/ (18 modules)

mock_llm/main.py       OpenAI-shaped fake provider, latency + failure injection
loadgen/main.py        scenario runner
tests/                 unit · integration · criteria
scripts/               setup · devctl · demo · seed · reconcile · verify_phase1
infra/redis.conf       project-local Redis config
alembic/versions/      6 migrations
```

~16,250 lines including tests.

---

## Configuration

Everything is environment-driven; see `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `DEV_MODE` | `services` | `embedded` swaps in fakeredis + SQLite |
| `DATABASE_URL` | `postgresql+asyncpg://localhost/budget_controller` | Sync drivers are auto-upgraded to asyncpg |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `REDIS_MAX_CONNECTIONS` | `128` | Blocking pool — bursts queue instead of erroring |
| `REDIS_POOL_TIMEOUT_SECONDS` | `10.0` | |
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:9000/v1` | Include the version prefix |
| `UPSTREAM_MODE` | `mock` | `live` uses each model's own endpoint |
| `UPSTREAM_TIMEOUT_SECONDS` | `30.0` | |
| `API_KEY_PEPPER` | random per process | **Set this** — otherwise every key breaks on restart |
| `ADMIN_TOKEN` | unset | Bearer token guarding `/admin/*` |
| `ENFORCEMENT_FAIL_MODE` | `closed` | Reject when Redis is unreachable |
| `DEFAULT_WARN_THRESHOLD` | `0.80` | |
| `DEFAULT_SUBSTITUTION_THRESHOLD` | `0.90` | |
| `DEFAULT_HARD_THRESHOLD` | `1.00` | |
| `HOLD_TTL_SECONDS` | `120` | How long an unsettled reservation survives |
| `REAPER_INTERVAL_SECONDS` | `30` | |
| `RUNAWAY_HOURLY_FRACTION` | `0.20` | Global default; per-agent overrides it |
| `RUNAWAY_WINDOW_MINUTES` | `60` | |
| `SESSION_TTL_SECONDS` | `43200` | 12 hours |
| `TOKENIZER` | `heuristic` | `tiktoken` for exact OpenAI counts |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | |

---

## Testing

**108 tests**, all green.

```bash
make test           # everything
make test-criteria  # only the six stated success criteria
```

The criteria tests run against the **live stack** — proxy on :8000, mock on
:9000 — not an in-process ASGI app. They are claims about deployed behaviour,
and an in-process harness would skip uvicorn's concurrency, real Redis, real
PostgreSQL and real HTTP.

### The six success criteria

| # | Criterion | How it is proven |
|---|---|---|
| 1 | Tracks across 3 concurrent agents making rapid calls | Redis and the ledger agree **exactly**, zero outstanding holds |
| 2 | Warning at the 80% threshold | Exactly one event at the crossing, still exactly one after 20 more calls |
| 3 | Hard block at 100% | The **mock provider's request counter does not move** — a blocked request never leaves the proxy |
| 4 | Session breach closes the session | Agent counter unchanged; a new session works immediately |
| 5 | Model substitution to a cheaper model | Asserted from the provider's side, not just the response header |
| 6 | Runaway detector on velocity | Trips while budget remains; stays tripped until an audited release |

### Coverage by file

| Tests | File | What it covers |
|---|---|---|
| 14 | `unit/test_money_and_security.py` | µ$ exactness over 100k charges, key generation/hashing, log redaction, tokenizer |
| 13 | `unit/test_chain_policy.py` | Chain validation and runtime filtering |
| 10 | `unit/test_providers.py` | OpenAI/Anthropic request and response translation |
| 7 | `integration/test_reservation_core.py` | **500 concurrent reserves admit exactly 100**, all-or-nothing, idempotent settle |
| 7 | `integration/test_resilience.py` | Orphaned-hold reaping, `LIMIT_MISSING`, fail-closed/open, negative clamp |
| 11 | `criteria/test_live_controls.py` | Freeze, boost, RPM/TPM |
| 8 | `criteria/test_model_catalog.py` | Catalog CRUD, credential handling, probe honesty |
| 7 | `criteria/test_bad_input.py` | Malformed bodies, unknown models, upstream failure |
| 6 | `criteria/test_substitution.py` | 4-step cross-provider ladder, rung by rung |
| 6 | `criteria/test_agent_detail.py` | UTC bucket alignment, dense series, sessions |
| 5 | `criteria/test_enforcement.py` | Criteria 2, 3, 4 |
| 5 | `criteria/test_team_transfer.py` | The accounting split, escape-by-move |
| 3 | `criteria/test_runaway.py` | Criterion 6 |
| 2 | `criteria/test_concurrency.py` | Criterion 1 |

Plus `./scripts/verify_phase1.sh` — 20 API-level assertions on agent CRUD and
key security.

### Load generator

```bash
python -m loadgen.main --scenario steady|burst|runaway|session|mixed [--calls N]
```

Each scenario provisions agents with the budget shape it needs, and every run
ends with a reconciliation check.

---

## Operations

```bash
make infra-up                        # Redis + PostgreSQL
./scripts/devctl.sh start|stop|restart|status|logs
make migrate                         # alembic upgrade head
make seed                            # teams, agents, catalog (idempotent)
make reconcile                       # report drift
python -m scripts.reconcile --apply  # rebuild counters from the ledger
```

### Reconciliation

Redis is a derived cache; the ledger is authoritative. After a Redis restart,
flush or eviction:

```bash
python -m scripts.reconcile --apply
```

This rebuilds every agent and team counter, and re-warms the cached limits.
Drift reported *while traffic is flowing* is normal — reservations are held
before their ledger row exists — so the report also prints outstanding holds.

### Homebrew's Redis 8.10 bottle is broken

Its default `redis.conf` loads four modules the bottle does not ship, so
`brew services start redis` aborts. This project runs the same binary against
`infra/redis.conf` instead. To fix the global service, comment out the
`loadmodule` lines in `/opt/homebrew/etc/redis.conf`.

---

## Design decisions and trade-offs

**Money is integer micro-dollars.** `$500.00` → `500_000_000`. Floats drift over
tens of thousands of calls, and Lua 5.1 numbers are IEEE doubles — µ$ keeps a
$1M budget five orders of magnitude below 2^53, so arithmetic inside the scripts
is exact. Conversion to a float or a display string happens only at the API
boundary. Fractional costs round **up**: under-charging a fraction of a
micro-dollar on every call is a systematic leak in the direction of overspending.

**Reservations hold the worst case, not the average.** Holding the average would
be more flattering and would break the guarantee: if 200 concurrent requests all
run long, the overshoot is discovered only at settle — the post-hoc discovery
this system replaces. The ceiling only constrains what may be *in flight*; the
difference is refunded, so agents are charged what they actually used.

**Fail closed.** Redis unreachable → reject. The absence of enforcement is the
bug this project exists to fix. `ENFORCEMENT_FAIL_MODE=open` is available and
explicit.

**API keys are HMAC-SHA256 with a server pepper, not bcrypt.** Those are
password hashes: deliberately slow, to make guessing a low-entropy human secret
expensive. An agent key is 256 bits of randomness — there is nothing to brute
force, so the slowness buys no security while costing ~100 ms on *every* proxied
request. Worse, a salted-per-row hash cannot be looked up by value, so
authentication would mean comparing against every agent row. HMAC-SHA256 is
deterministic (a unique-indexed lookup), fast, and keeps the pepper outside the
database, so a stolen dump cannot verify keys offline.

**The raw key exists in exactly one place** — the body of the response that
created it. A non-secret `sk-agent-a1b2c3…` prefix is stored so the UI can
identify a key it cannot see, and a structlog processor scrubs any `sk-agent-…`
literal from every log record regardless of which field it arrives in.

**Substitution never crosses providers without permission**, and the swap is
always disclosed in the response headers and the ledger. A silent model change
is a correctness change the agent's owner deserves to see.

**Rate limits are fixed windows, not sliding.** Two integer counters and no
per-request bookkeeping, at the cost of allowing up to 2× the limit across a
minute boundary. Acceptable for a pacing control protecting an upstream
provider; *not* acceptable for the budget, which is why the budget is not
implemented this way.

**The reaper exists because crashes happen.** A reservation is held from the
moment it is granted until the call settles. If the process dies, nothing
settles it, and the hold sits against the budget for the rest of the month —
enough of those and an agent is throttled by money it never spent.

**No frontend build step.** Native ES modules, hand-rolled SVG charts, no CDN.
A charting library would mean either a runtime dependency a self-hosted control
plane should not need, or a bundler. Static assets are served with
`must-revalidate` so a deployed fix never appears not to have shipped.

---

## Known limitations

- **Streaming responses are not proxied.** The mock implements SSE and the
  settle path is shaped for a usage-bearing final chunk, but the proxy handles
  non-streaming completions only.
- **One request shape.** The OpenAI `/v1/chat/completions` schema. Anthropic is
  supported as an *upstream* via the adapter, but not as an inbound shape.
- **Bedrock and Vertex are not dispatched natively** — they need request signing
  rather than a bearer token. Route them through an OpenAI-compatible gateway.
- **Token estimation is heuristic** (~4 chars/token) unless `TOKENIZER=tiktoken`.
  Under-estimation is safe because settle corrects against reported usage.
- **`/admin/*` is unauthenticated**, by decision, including the freeze switch,
  budget boosts and rate limits. Anyone who can reach the address can read the
  whole fleet and change it. Agent keys are never exposed — only their prefix —
  and `/v1/chat/completions` still requires a valid `X-Agent-Key`.
  `ADMIN_TOKEN` exists but is deliberately unset: the dashboard's JavaScript
  never sends it, so enabling it locks the UI out. Restrict by network instead.
- **Rate limits allow a 2× burst across a minute boundary** (fixed window, as
  above).
- **`move_agent.lua` spans two hash tags**, so it is atomic on standalone Redis
  but would need a two-phase migration under Redis Cluster. Every other script
  stays within one slot.
- **No payload capture.** Prompts and completions are never stored, so the event
  overlay shows costs, tokens and decisions but not text.

---

## Troubleshooting

**`brew services start redis` fails** — expected; see above. Use
`make infra-up`.

**Every request returns 401 after a restart** — `API_KEY_PEPPER` was unset, so a
random one was generated and every stored hash is now unverifiable. Set it in
`.env` and re-seed, or rotate keys.

**Charts are empty but calls are being made** — check the server's time zone
handling; buckets are truncated in UTC deliberately. `GET
/admin/agents/{id}/history?days=7` should show non-zero `calls` in at least one
bucket.

**`make reconcile` reports drift** — if `outstanding_holds` is non-zero, traffic
is in flight and the drift is expected. If it is zero and drift persists, the
two stores genuinely disagree; `--apply` rebuilds from the ledger.

**Requests return 503 with `dispatch_frozen`** — someone froze the system.
`GET /admin/freeze` shows who and why.

**An agent returns 423 forever** — it tripped the runaway detector and is waiting
for a human. Release it from the Paused/blocked tile, or
`POST /admin/agents/{id}/unblock` with a reason.
