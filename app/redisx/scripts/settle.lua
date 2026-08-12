--[[
settle.lua — replace the hold with what the call actually cost.

The reservation held the worst case. The provider has now reported real usage,
which is almost always less, so the difference is refunded to all three scopes
atomically.

Idempotent by construction: the hold is deleted as part of the same atomic
execution that applies the adjustment, so a retried settle for the same
request finds no hold and does nothing. Combined with the UNIQUE constraint on
call_ledger.request_id, a duplicate settle cannot double-charge.

KEYS  1 team spend  2 agent spend  3 session spend  4 hold  5 pending zset
ARGV  1 actual (µ$)  2 now (epoch s)

RETURNS {status, estimate, actual, delta, team_spend, agent_spend, session_spend}
        status ∈ SETTLED | NOHOLD
--]]

local team_k    = KEYS[1]
local agent_k   = KEYS[2]
local session_k = KEYS[3]
local hold_k    = KEYS[4]
local pending_k = KEYS[5]

local actual = tonumber(ARGV[1])

local estimate = redis.call('HGET', hold_k, 'estimate')
if not estimate then
  -- Already settled, or the reaper reclaimed it after this request stalled.
  -- Either way the budget was returned once; doing it again would be a leak in
  -- the agent's favour.
  return {'NOHOLD', '0', tostring(actual), '0', '0', '0', '0'}
end
estimate = tonumber(estimate)

local delta = actual - estimate

-- Clamp at zero rather than allowing a negative counter. A negative total is
-- always a bug (double settle, manual surgery), and letting it persist would
-- silently grant an agent free budget for the rest of the period.
local function adjust(key, amount)
  if amount == 0 then
    return tonumber(redis.call('GET', key) or '0')
  end
  local updated = redis.call('INCRBY', key, amount)
  if updated < 0 then
    redis.call('SET', key, 0)
    return 0
  end
  return updated
end

local team_after    = adjust(team_k, delta)
local agent_after   = adjust(agent_k, delta)
local session_after = adjust(session_k, delta)

redis.call('DEL', hold_k)
redis.call('ZREM', pending_k, hold_k)

return {
  'SETTLED',
  tostring(estimate), tostring(actual), tostring(delta),
  tostring(team_after), tostring(agent_after), tostring(session_after)
}
