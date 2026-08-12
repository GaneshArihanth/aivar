--[[
reserve.lua — the enforcement decision.

Everything that decides whether a call may proceed happens inside this script,
in one atomic Redis execution. That is the entire point of the design.

The alternative — read the counters in Python, compare, then write — has a
window between the read and the write in which every other concurrent request
sees the same stale total. With twelve agents and any real concurrency, that
window is how a budget gets blown through: 200 requests can each observe "79%
consumed" and each conclude it has room. Redis executes a script to completion
without interleaving, so the check and the increment cannot be separated.

The same reasoning forces the three scopes into *one* script rather than three
calls. Incrementing the session counter and then discovering the agent is
exhausted would burn session budget on a request that never ran. Either all
three scopes are charged or none are.

KEYS
  1  team spend           2  agent spend         3  session spend
  4  team limit           5  agent limit         6  session meta (hash)
  7  agent blocked flag   8  hold (hash)         9  pending-holds zset
 10  team 80% warn flag  11  agent 80% warn flag  12  agent sessions index
 13  global freeze flag  14  team freeze flag     15  agent boost (µ$)
 16  rpm bucket          17  tpm bucket

ARGV
  1  estimate (µ$)        2  request_id          3  now (epoch s)
  4  hold expiry (epoch)  5  session limit (µ$)  6  warn threshold
  7  hard threshold       8  substitution thr.   9  allow substitution (0|1)
 10  session ttl (s)     11  final attempt (0|1) 12  warn flag ttl (s)
 13  model id            14  hold ttl (s)        15  agent id
 16  session id          17  rpm limit (0=off)   18  tpm limit (0=off)
 19  prompt+max tokens   20  rate bucket ttl (s)

RETURNS a flat array of strings:
  {status, detail, team_spend, team_limit, agent_spend, agent_limit,
   session_spend, session_limit, warned_scopes_csv}

  status ∈ OK | PRESSURE | EXHAUSTED | SESSION_EXHAUSTED | SESSION_CLOSED
            | BLOCKED | LIMIT_MISSING | FROZEN | RATE_LIMITED
--]]

local team_spend_k    = KEYS[1]
local agent_spend_k   = KEYS[2]
local session_spend_k = KEYS[3]
local team_limit_k    = KEYS[4]
local agent_limit_k   = KEYS[5]
local session_meta_k  = KEYS[6]
local blocked_k       = KEYS[7]
local hold_k          = KEYS[8]
local pending_k       = KEYS[9]
local warn_team_k     = KEYS[10]
local warn_agent_k    = KEYS[11]
local sessions_index_k = KEYS[12]
local freeze_global_k  = KEYS[13]
local freeze_team_k    = KEYS[14]
local boost_k          = KEYS[15]
local rpm_k            = KEYS[16]
local tpm_k            = KEYS[17]

local estimate      = tonumber(ARGV[1])
local request_id    = ARGV[2]
local now           = tonumber(ARGV[3])
local hold_expiry   = tonumber(ARGV[4])
local session_limit = tonumber(ARGV[5])
local warn_t        = tonumber(ARGV[6])
local hard_t        = tonumber(ARGV[7])
local sub_t         = tonumber(ARGV[8])
local allow_sub     = tonumber(ARGV[9]) == 1
local session_ttl   = tonumber(ARGV[10])
local final         = tonumber(ARGV[11]) == 1
local warn_ttl      = tonumber(ARGV[12])
local model         = ARGV[13]
local hold_ttl      = tonumber(ARGV[14])
local agent_id      = ARGV[15]
local session_id    = ARGV[16]
local rpm_limit     = tonumber(ARGV[17])
local tpm_limit     = tonumber(ARGV[18])
local tokens        = tonumber(ARGV[19])
local rate_ttl      = tonumber(ARGV[20])

local function reply(status, detail, ts, tl, as, al, ss, sl, warned)
  return {
    status, detail,
    tostring(ts or 0), tostring(tl or 0),
    tostring(as or 0), tostring(al or 0),
    tostring(ss or 0), tostring(sl or 0),
    warned or ""
  }
end

-- 1. Circuit breaker. A runaway agent is refused before anything else is
--    considered: it is paused pending human review, not merely over budget.
if redis.call('EXISTS', blocked_k) == 1 then
  return reply('BLOCKED', 'agent')
end

-- 2. Kill switches. Checked here, inside the atomic script, for the same
--    reason the budget is: a freeze evaluated in Python would not stop the
--    requests already in flight between that check and the increment, which is
--    precisely the traffic an operator is trying to stop during an incident.
if redis.call('EXISTS', freeze_global_k) == 1 then
  return reply('FROZEN', 'global')
end
if redis.call('EXISTS', freeze_team_k) == 1 then
  return reply('FROZEN', 'team')
end

-- 3. Limits. A missing limit is never treated as "unlimited" — the caller is
--    told to reload it from PostgreSQL and retry. Silently allowing a request
--    because a cache key expired would be the failure mode this system exists
--    to prevent.
local team_limit = redis.call('GET', team_limit_k)
if not team_limit then return reply('LIMIT_MISSING', 'team') end
team_limit = tonumber(team_limit)

local agent_limit = redis.call('GET', agent_limit_k)
if not agent_limit then return reply('LIMIT_MISSING', 'agent') end
agent_limit = tonumber(agent_limit)

-- A one-time boost raises the ceiling for this period only. Added here rather
-- than written into the limit itself, so the baseline is never lost and the
-- grant can expire on its own.
local boost = tonumber(redis.call('GET', boost_k) or '0')
if boost > 0 then
  agent_limit = agent_limit + boost
end

-- 3. Session state.
local session_status = redis.call('HGET', session_meta_k, 'status')
if session_status == 'closed' then
  local closed_spend = tonumber(redis.call('GET', session_spend_k) or '0')
  return reply('SESSION_CLOSED', 'session',
               0, team_limit, 0, agent_limit, closed_spend, session_limit)
end
local stored_session_limit = redis.call('HGET', session_meta_k, 'limit')
if stored_session_limit then session_limit = tonumber(stored_session_limit) end

-- A session limit of zero means "no per-session cap configured", not "this
-- agent may spend nothing". Read the other way round, an agent whose
-- per-session budget row is missing would have every request refused with
-- "session budget exhausted: $0.00 of $0.00" — bricked, and misleadingly so.
-- The agent and team caps still apply.
local session_capped = session_limit > 0

-- 4. Current totals. Each counter already includes outstanding holds, so a
--    request in flight is counted against the budget while it runs.
local team_now    = tonumber(redis.call('GET', team_spend_k) or '0')
local agent_now   = tonumber(redis.call('GET', agent_spend_k) or '0')
local session_now = tonumber(redis.call('GET', session_spend_k) or '0')

local team_next    = team_now + estimate
local agent_next   = agent_now + estimate
local session_next = session_now + estimate

-- 5. Hard limits, broadest scope first: a team-level breach is the one an
--    operator must act on, and no cheaper model or fresh session escapes it.
if team_next > team_limit * hard_t then
  return reply('EXHAUSTED', 'team',
               team_now, team_limit, agent_now, agent_limit,
               session_now, session_limit)
end
if agent_next > agent_limit * hard_t then
  return reply('EXHAUSTED', 'agent',
               team_now, team_limit, agent_now, agent_limit,
               session_now, session_limit)
end
if session_capped and session_next > session_limit * hard_t then
  -- Close this session only, and only once there is nothing cheaper left to
  -- try. Closing on the first over-budget estimate would end the session even
  -- when a cheaper model would have fitted inside the remaining allowance.
  --
  -- The agent's monthly budget is deliberately untouched: nothing has been
  -- incremented on the path to here, and the agent may open a new session and
  -- continue working.
  if final then
    redis.call('HSET', session_meta_k,
               'status', 'closed',
               'closed_at', now,
               'close_reason', 'session_budget_exhausted')
    redis.call('EXPIRE', session_meta_k, session_ttl)
  end
  return reply('SESSION_EXHAUSTED', 'session',
               team_now, team_limit, agent_now, agent_limit,
               session_now, session_limit)
end

-- 6. Pressure. Under the hard limit but close to it: tell the caller so it can
--    retry against a cheaper model. Skipped on the final attempt, when there is
--    nothing cheaper left to fall back to and the request should simply run.
if (not final) and allow_sub then
  local scope = nil
  if agent_next >= agent_limit * sub_t then scope = 'agent'
  elseif team_next >= team_limit * sub_t then scope = 'team'
  elseif session_capped and session_next >= session_limit * sub_t then
    scope = 'session'
  end
  if scope then
    return reply('PRESSURE', scope,
                 team_now, team_limit, agent_now, agent_limit,
                 session_now, session_limit)
  end
end

-- 7. Rate limits. Distinct from budget: a cheap model can still hammer a
--    provider into throttling everyone. Checked last among the refusals so a
--    request that is over budget is told about the budget, which is the more
--    fundamental problem, and counted only on the commit below — a request
--    refused for budget must not consume rate allowance it never used.
--
--    These are FIXED windows keyed by minute, not sliding ones. The trade-off
--    is deliberate: two integer counters and no per-request bookkeeping, at the
--    cost of allowing up to 2x the limit across a boundary (the tail of one
--    minute plus the head of the next). That is acceptable for a pacing
--    control protecting an upstream provider. It would not be acceptable for
--    the budget, which is why the budget is not implemented this way.
if rpm_limit > 0 then
  local rpm_now = tonumber(redis.call('GET', rpm_k) or '0')
  if rpm_now + 1 > rpm_limit then
    return reply('RATE_LIMITED', 'rpm',
                 team_now, team_limit, agent_now, agent_limit,
                 session_now, session_limit, tostring(rpm_now))
  end
end
if tpm_limit > 0 then
  local tpm_now = tonumber(redis.call('GET', tpm_k) or '0')
  if tpm_now + tokens > tpm_limit then
    return reply('RATE_LIMITED', 'tpm',
                 team_now, team_limit, agent_now, agent_limit,
                 session_now, session_limit, tostring(tpm_now))
  end
end

-- 8. Commit. From here nothing can fail, so all three scopes move together.
redis.call('INCRBY', team_spend_k, estimate)
redis.call('INCRBY', agent_spend_k, estimate)
redis.call('INCRBY', session_spend_k, estimate)
redis.call('EXPIRE', session_spend_k, session_ttl)

if rpm_limit > 0 then
  redis.call('INCRBY', rpm_k, 1)
  redis.call('EXPIRE', rpm_k, rate_ttl)
end
if tpm_limit > 0 then
  redis.call('INCRBY', tpm_k, tokens)
  redis.call('EXPIRE', tpm_k, rate_ttl)
end

if not session_status then
  redis.call('HSET', session_meta_k,
             'status', 'open',
             'limit', session_limit,
             'opened_at', now,
             'agent_id', agent_id,
             'session_id', session_id)
  -- Index the session against its agent so the fleet's live sessions can be
  -- listed and terminated. Written here, inside the same atomic commit that
  -- creates the session, so the index can never disagree with what exists.
  redis.call('ZADD', sessions_index_k, now, session_id)
  redis.call('EXPIRE', sessions_index_k, session_ttl)
end
redis.call('EXPIRE', session_meta_k, session_ttl)

-- The hold records what to give back at settle time, and lets the reaper
-- reclaim the budget if this request dies before settling.
redis.call('HSET', hold_k,
           'estimate', estimate,
           'request_id', request_id,
           'team_key', team_spend_k,
           'agent_key', agent_spend_k,
           'session_key', session_spend_k,
           'model', model,
           'created_at', now,
           'expires_at', hold_expiry)
redis.call('EXPIRE', hold_k, hold_ttl)
redis.call('ZADD', pending_k, hold_expiry, hold_k)

-- 9. Threshold warnings. SETNX makes this fire exactly once per scope per
--    period: on the call that crosses 80%, and never again — so the alert
--    marks the crossing instead of becoming a per-request log flood.
local warned = {}
if team_next >= team_limit * warn_t and team_now < team_limit * warn_t then
  if redis.call('SETNX', warn_team_k, now) == 1 then
    redis.call('EXPIRE', warn_team_k, warn_ttl)
    table.insert(warned, 'team')
  end
end
if agent_next >= agent_limit * warn_t and agent_now < agent_limit * warn_t then
  if redis.call('SETNX', warn_agent_k, now) == 1 then
    redis.call('EXPIRE', warn_agent_k, warn_ttl)
    table.insert(warned, 'agent')
  end
end

return reply('OK', '',
             team_next, team_limit, agent_next, agent_limit,
             session_next, session_limit,
             table.concat(warned, ','))
