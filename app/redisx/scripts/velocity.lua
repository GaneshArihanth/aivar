--[[
velocity.lua — sliding-hour spend rate and the runaway circuit breaker.

Detects the scenario in the brief: an agent stuck in a recursive loop burning a
month of budget overnight. A monthly total cannot catch it — by the time the
total looks alarming the money is gone. What distinguishes a loop from ordinary
work is *rate*, so this tracks spend per minute and compares the last hour
against a fraction of the monthly budget.

The window is 60 one-minute counters rather than a sorted set of individual
calls: it stays O(60) to read regardless of traffic, and it cannot grow without
bound during exactly the runaway event it is meant to detect — a design that
allocates memory per call would be at its weakest when a loop is emitting
50,000 of them.

Bucket keys are constructed inside the script from a prefix. They all carry the
same {team:N} hash tag as KEYS[1], so they resolve to one slot and the script
stays valid under Redis Cluster.

KEYS  1 blocked flag
ARGV  1 bucket key prefix   2 current bucket   3 window minutes
      4 amount (µ$)         5 threshold (µ$)   6 bucket ttl (s)
      7 now (epoch s)       8 bucket ttl guard

RETURNS {hour_spend, tripped(0|1), threshold, already_blocked(0|1)}
--]]

local blocked_k  = KEYS[1]

local prefix     = ARGV[1]
local bucket     = tonumber(ARGV[2])
local window     = tonumber(ARGV[3])
local amount     = tonumber(ARGV[4])
local threshold  = tonumber(ARGV[5])
local bucket_ttl = tonumber(ARGV[6])
local now        = tonumber(ARGV[7])

if amount > 0 then
  local current_k = prefix .. bucket
  redis.call('INCRBY', current_k, amount)
  redis.call('EXPIRE', current_k, bucket_ttl)
end

local hour_spend = 0
for i = 0, window - 1 do
  local value = redis.call('GET', prefix .. (bucket - i))
  if value then hour_spend = hour_spend + tonumber(value) end
end

local already_blocked = redis.call('EXISTS', blocked_k)
if already_blocked == 1 then
  return {tostring(hour_spend), '0', tostring(threshold), '1'}
end

-- Threshold of zero means the detector is disabled for this agent.
if threshold > 0 and hour_spend > threshold then
  -- No TTL: the pause persists until a human releases it through the unblock
  -- endpoint. An automatic expiry would let a looping agent resume on its own,
  -- which is the opposite of "pause for human review".
  redis.call('HSET', blocked_k,
             'reason', 'runaway_velocity',
             'hour_spend', hour_spend,
             'threshold', threshold,
             'tripped_at', now)
  return {tostring(hour_spend), '1', tostring(threshold), '0'}
end

return {tostring(hour_spend), '0', tostring(threshold), '0'}
