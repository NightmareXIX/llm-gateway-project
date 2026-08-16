-- reserve.lua — atomic check-and-increment across every declared quota window.
--
-- The whole reason this is Lua and not a pipeline (Contract C, trap 2): a check
-- and an increment issued as two round trips is a race. Fifty concurrent callers
-- all GET a counter at 9 against a limit of 10, all see room, and all INCRBY —
-- and the overshoot is invisible until the provider rate-limits earlier than
-- predicted. Redis runs a script to completion before serving another command,
-- so the check and the increment here are one indivisible operation.
--
-- Two passes, and the order matters (trap 3). Every window is checked before any
-- window is incremented: a script that incremented as it went and bailed on
-- window three would leave windows one and two permanently overstated, with no
-- reservation recorded to give them back.
--
--   KEYS[1]                  reservation hash
--   KEYS[2..n+1]             counter keys, in the same order as the ARGV blocks
--   ARGV[1]                  n, the window count
--   ARGV[2..5n+1]            n blocks of five: name, limit, cost, ttl, refresh
--   ARGV[5n+2]               reservation TTL, seconds
--   ARGV[5n+3]               request_id, recorded for debugging
--
--   -> {1}                        reserved
--   -> {0, window, ttl_remaining} blocked, naming the window and when it frees
--
-- The blocks carry five values rather than the four the plan sketched because
-- `refresh` is the one piece of window semantics the script would otherwise have
-- to infer from the window's *name*. Policy — headroom, D8's lane split, which
-- windows converge on a real reset instant — is applied in Python before the
-- script sees a number. This file only does arithmetic on what it was handed.

local reservation_key = KEYS[1]
local window_count = tonumber(ARGV[1])

-- Pass 1: can every window afford this? No writes here.
for i = 1, window_count do
    local base = 1 + (i - 1) * 5
    local limit = tonumber(ARGV[base + 2])
    local cost = tonumber(ARGV[base + 3])
    local counter_key = KEYS[i + 1]

    local used = tonumber(redis.call('GET', counter_key)) or 0
    if used + cost > limit then
        -- The counter's own TTL, not a recomputed window width: a fixed window
        -- is aligned to its first increment, so only Redis knows how much of it
        -- is left. A negative TTL means the key vanished between the GET and
        -- here; the caller substitutes its own estimate.
        local ttl_remaining = redis.call('TTL', counter_key)
        if ttl_remaining < 0 then
            ttl_remaining = 0
        end
        return { 0, ARGV[base + 1], ttl_remaining }
    end
end

-- Pass 2: nothing can block now, so spend it.
for i = 1, window_count do
    local base = 1 + (i - 1) * 5
    local name = ARGV[base + 1]
    local cost = tonumber(ARGV[base + 3])
    local ttl = tonumber(ARGV[base + 4])
    local refresh = ARGV[base + 5] == '1'
    local counter_key = KEYS[i + 1]

    local value = redis.call('INCRBY', counter_key, cost)

    -- Trap 1, and it is the expensive one. A per-minute counter whose TTL is
    -- refreshed on every increment never expires under sustained traffic: `rpm`
    -- climbs forever and the gateway serves a permanent false 429 that looks
    -- exactly like a provider outage. So `rpm`/`tpm` set their TTL only when
    -- this call created the counter — which is what an INCRBY returning exactly
    -- the cost means. `rpd`/`tpd` pass refresh=1 because their TTL converges on
    -- a real instant and re-setting it is idempotent.
    --
    -- The TTL < 0 arm is the backstop: a key that somehow exists without one
    -- would otherwise be the permanent false 429 this comment is about.
    if refresh or value == cost or redis.call('TTL', counter_key) < 0 then
        redis.call('EXPIRE', counter_key, ttl)
    end

    -- HINCRBY, not HSET. Contract C keys the reservation on the request id, so a
    -- same-provider retry within one request reserves against the same hash;
    -- accumulating means the eventual release gives back both attempts instead
    -- of silently dropping the first one's budget on the floor.
    redis.call('HINCRBY', reservation_key, name, cost)
end

redis.call('HSETNX', reservation_key, 'request_id', ARGV[window_count * 5 + 3])
redis.call('EXPIRE', reservation_key, tonumber(ARGV[window_count * 5 + 2]))

return { 1 }
