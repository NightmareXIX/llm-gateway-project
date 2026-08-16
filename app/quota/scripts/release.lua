-- release.lua — give back everything a reservation took, request windows included.
--
-- Called for exactly one case: an attempt that never left the process. A render
-- failure after the reservation, or a candidate abandoned before the call. Any
-- attempt that reached the provider commits instead, even a failed one (trap 6).
--
--   KEYS[1]         reservation hash
--   KEYS[2..n+1]    counter keys, in the same order as the ARGV names
--   ARGV[1]         n, the window count
--   ARGV[2..n+1]    window names, matching the reservation hash's fields
--
--   -> {1}  released
--   -> {0}  the reservation was already gone; nothing released
--
-- Amounts come from the hash rather than from the caller, so a release cannot
-- subtract something the reserve never added — which is what makes a double
-- release harmless: the DEL below means the second one finds nothing.

local reservation_key = KEYS[1]

if redis.call('EXISTS', reservation_key) == 0 then
    return { 0 }
end

local window_count = tonumber(ARGV[1])

for i = 1, window_count do
    local name = ARGV[i + 1]
    local counter_key = KEYS[i + 1]
    local reserved = tonumber(redis.call('HGET', reservation_key, name)) or 0

    -- A counter that has expired needs no refund: its window has reset and the
    -- budget is already back. Recreating it to write a decrement would leave a
    -- TTL-less key holding a negative-then-clamped zero forever.
    if reserved > 0 and redis.call('EXISTS', counter_key) == 1 then
        local value = redis.call('INCRBY', counter_key, -reserved)
        if value < 0 then
            redis.call('INCRBY', counter_key, -value)
        end
    end
end

redis.call('DEL', reservation_key)

return { 1 }
