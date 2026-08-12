--[[
release.lua — return a hold in full, charging nothing.

Used when a call never produced billable usage: the provider errored, the
request timed out, the client disconnected, or the reaper found a hold whose
owning request died.

Without this, every failed request would permanently consume its worst-case
reservation. An agent hitting a broken provider would exhaust its month
without generating a single token — the budget would be spent on nothing.

KEYS  1 team spend  2 agent spend  3 session spend  4 hold  5 pending zset
ARGV  1 reason

RETURNS {status, released, team_spend, agent_spend, session_spend}
        status ∈ RELEASED | NOHOLD
--]]

local team_k    = KEYS[1]
local agent_k   = KEYS[2]
local session_k = KEYS[3]
local hold_k    = KEYS[4]
local pending_k = KEYS[5]

local estimate = redis.call('HGET', hold_k, 'estimate')
if not estimate then
  return {'NOHOLD', '0', '0', '0', '0'}
end
estimate = tonumber(estimate)

local function give_back(key)
  local updated = redis.call('INCRBY', key, -estimate)
  if updated < 0 then
    redis.call('SET', key, 0)
    return 0
  end
  return updated
end

local team_after    = give_back(team_k)
local agent_after   = give_back(agent_k)
local session_after = give_back(session_k)

redis.call('DEL', hold_k)
redis.call('ZREM', pending_k, hold_k)

return {
  'RELEASED', tostring(estimate),
  tostring(team_after), tostring(agent_after), tostring(session_after)
}
