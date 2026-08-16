-- commit.lua — correct a reservation's token estimate to what was really spent.
--
-- Only the *token* windows are adjusted, by the difference between what was
-- reserved and what actually happened. The request windows are deliberately left
-- alone (trap 6): the provider counted the request the moment it arrived, so a
-- 429 you provoked is a request you made, and handing that back would make the
-- tracker wrong in exactly the scenario it exists for.
--
--   KEYS[1]         reservation hash
--   KEYS[2..n+1]    token counter keys, in the same order as the ARGV blocks
--   ARGV[1]         n, the token-window count (may be 0 — the DEL still matters)
--   ARGV[2..4n+1]   n blocks of four: name, actual, ttl, refresh
--
--   -> {1}  applied
--   -> {0}  the reservation was gone; nothing applied
--
-- The {0} sentinel is D17's expired-reservation case and it is a real one:
-- RESERVATION_TTL_S is 120 seconds and a long generation can outlive it. The
-- estimate stays counted, which over-counts — the safe direction. Applying the
-- delta blind would be worse: it would double-count a stream whose reservation a
-- release had already refunded.

local reservation_key = KEYS[1]

if redis.call('EXISTS', reservation_key) == 0 then
    return { 0 }
end

local window_count = tonumber(ARGV[1])

for i = 1, window_count do
    local base = 1 + (i - 1) * 4
    local name = ARGV[base + 1]
    local actual = tonumber(ARGV[base + 2])
    local ttl = tonumber(ARGV[base + 3])
    local refresh = ARGV[base + 4] == '1'
    local counter_key = KEYS[i + 1]

    local reserved = tonumber(redis.call('HGET', reservation_key, name)) or 0
    local delta = actual - reserved

    if delta > 0 then
        -- Upward corrections may create the counter. If the window rolled over
        -- mid-generation those tokens land in the new window rather than the one
        -- they were spent in, which over-counts briefly; under-counting them
        -- would let real spend go unrecorded, and this gateway is supposed to
        -- under-spend its budgets on purpose.
        redis.call('INCRBY', counter_key, delta)
        if refresh or redis.call('TTL', counter_key) < 0 then
            redis.call('EXPIRE', counter_key, ttl)
        end
    elseif delta < 0 and redis.call('EXISTS', counter_key) == 1 then
        -- A refund never resurrects a counter. The window it was reserved
        -- against has already reset, so there is nothing left to give back and
        -- recreating the key would write a negative correction into a window
        -- that never saw the spend.
        local value = redis.call('INCRBY', counter_key, delta)
        if value < 0 then
            -- Clamped here rather than in Python, because a counter that a Redis
            -- restart left behind at zero must not be pushed negative by a
            -- commit that arrives afterwards — and only the script sees the
            -- result of its own INCRBY atomically.
            redis.call('INCRBY', counter_key, -value)
        end
    end
end

redis.call('DEL', reservation_key)

return { 1 }
