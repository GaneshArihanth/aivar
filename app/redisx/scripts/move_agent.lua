--[[
move_agent.lua — reassign an agent to a different team.

Every counter key carries a {team:N} hash tag, so an agent's live state lives
under its team's namespace. Changing `agents.team_id` in PostgreSQL alone would
leave all of it stranded: the old counter would keep accruing nothing, a fresh
one would start at zero under the new tag, and the agent would silently receive
a brand-new monthly allowance — moving an agent between teams would become a
way to reset its budget.

What moves and what stays is a deliberate split:

  · the agent's own monthly spend MOVES. It is the agent's consumption, and it
    must keep constraining the agent wherever it sits.
  · the team totals STAY. The old team really did spend that money; rewriting
    history to make it disappear would misstate what a department consumed.
  · the runaway breaker and its velocity window MOVE, so an agent cannot escape
    a pause, or the rate that earned it, by being reassigned.
  · open sessions are left behind to expire. Their keys are team-tagged and
    short-lived; the agent opens new ones immediately.

The ledger keeps each call's original team_id, so `make reconcile` reproduces
exactly this split from PostgreSQL.

CLUSTER CAVEAT: this is the one operation that spans two hash tags, so it is
atomic on standalone Redis but would need a two-phase migration under Redis
Cluster. Every other script in this directory stays within one slot.

KEYS  1 old spend   2 new spend   3 old blocked   4 new blocked
      5 old limit   6 new limit
ARGV  1 old velocity prefix   2 new velocity prefix   3 current bucket
      4 window minutes        5 bucket ttl (s)       6 limit (µ$)

RETURNS {moved_micros, blocked_moved(0|1), velocity_buckets_moved}
--]]

local old_spend_k   = KEYS[1]
local new_spend_k   = KEYS[2]
local old_blocked_k = KEYS[3]
local new_blocked_k = KEYS[4]
local old_limit_k   = KEYS[5]
local new_limit_k   = KEYS[6]

local old_prefix = ARGV[1]
local new_prefix = ARGV[2]
local bucket     = tonumber(ARGV[3])
local window     = tonumber(ARGV[4])
local bucket_ttl = tonumber(ARGV[5])
local limit      = ARGV[6]

-- 1. Carry the agent's consumption across.
local spend = tonumber(redis.call('GET', old_spend_k) or '0')
if spend > 0 then
  redis.call('SET', new_spend_k, spend)
end
redis.call('DEL', old_spend_k)

-- 2. Carry the circuit breaker. A paused agent stays paused.
local blocked_moved = 0
if redis.call('EXISTS', old_blocked_k) == 1 then
  local fields = redis.call('HGETALL', old_blocked_k)
  if #fields > 0 then
    redis.call('HSET', new_blocked_k, unpack(fields))
    blocked_moved = 1
  end
  redis.call('DEL', old_blocked_k)
end

-- 3. Carry the velocity window, so the rate that tripped (or is about to trip)
--    the detector is not laundered by the move.
local buckets_moved = 0
for i = 0, window - 1 do
  local from_key = old_prefix .. (bucket - i)
  local value = redis.call('GET', from_key)
  if value then
    redis.call('SET', new_prefix .. (bucket - i), value, 'EX', bucket_ttl)
    redis.call('DEL', from_key)
    buckets_moved = buckets_moved + 1
  end
end

-- 4. Warm the limit under the new tag so the first request after the move is
--    enforced immediately rather than taking the LIMIT_MISSING reload path.
redis.call('SET', new_limit_k, limit)
redis.call('DEL', old_limit_k)

return {tostring(spend), tostring(blocked_moved), tostring(buckets_moved)}
