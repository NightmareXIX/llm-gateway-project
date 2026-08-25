"""Every Redis key the gateway will ever write, and nothing else.

Contract C (§2.3) is frozen, and the hard rule that enforces it is short: **never
write an f-string Redis key outside this module.** A key format that exists in
two places drifts, and the drift is silent — the writer and the reader simply
stop meeting, so a quota counter reads zero forever and a breaker never opens.

Two consequences of taking that rule seriously:

**Every builder is here now, including the nine nothing calls yet.** Phase 2 uses
:func:`breaker` and :func:`stream_attempts`; quota, caching, idempotency and the
perception lane arrive in Phases 3 and 4. Writing only what today needs is how
the first stray f-string gets written — the rule is unenforceable in the moment
someone needs a key and finds no builder for it.

**TTLs live beside the builders.** The key and how long it survives are one
decision, and separating them is how a 60-second counter ends up outliving its
window. The exceptions are the daily counters, whose TTL is a *provider* fact
rather than ours — see :data:`UNTIL_PROVIDER_RESET`.

Keys are built, never parsed. That matters more than it looks: model ids
legitimately contain colons (``openai/gpt-oss-20b:free``), so the segments of a
key are not recoverable from the key. Nothing needs to recover them, and the
alternative — rejecting or escaping colons — would break OpenRouter on the day
its adapter lands.
"""

from __future__ import annotations

from typing import Final, Literal
from uuid import UUID

# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
type Scope = str
"""``"system"`` or a ``user_id``.

The mechanism that keeps shared-pool and BYOK usage from cross-contaminating
(overview §9.4): a request served by a user's own key counts against their scope,
one served by the shared pool counts against ``system``, and neither can spend
the other's budget. Quota scopes on ``user_id`` and never ``api_key_id`` — a user
with three integrations is one user with one budget (ADR-007).
"""

SYSTEM_SCOPE: Final[Scope] = "system"
"""The shared pool's scope. The default until Phase 6 gives users their own keys."""

QuotaWindow = Literal["rpm", "rpd", "tpm", "tpd"]
"""The four windows a provider may publish. Hitting any one of them is a 429,
independently of the others — which is why they are four counters and not one."""

GatewayWindow = Literal["rpm", "rpd", "rph"]
"""The windows *we* limit *our own* users on. A deliberately separate type from
:data:`QuotaWindow`: the gateway counts requests per user and never tokens, and
conflating the two would let a ``tpm`` reach a key that has no meaning for it.

``rpm``/``rpd`` are D20's per-tier chat throughput limit. ``rph`` is D43's
anti-abuse floor on the BYOK validation endpoint — a third legal value in an
existing segment, which is what Phase 3 Step 10's amendment to the
``rl:{user_id}:{window}:{window_start}`` format (ADR-022) was for. Not a
second Contract C change: the key shape is untouched, only the set of strings
that may fill this one segment grew."""


# --------------------------------------------------------------------------- #
# TTLs
# --------------------------------------------------------------------------- #
UNTIL_PROVIDER_RESET: Final = None
"""The TTL of a key whose window is the *provider's* to define, not ours.

``rpd``, ``tpd`` and the perception lane reset when the provider says they do —
a rolling 24h window for one, midnight Pacific for another (``fixed_daily_pt``
in ``config/limits.yaml`` exists for exactly this). Phase 3's
``quota/windows.py`` computes the remaining seconds per provider; there is no
correct constant to put here, and a plausible-looking one (86400) would be wrong
for Gemini by up to eight hours.
"""

QUOTA_WINDOW_TTL_S: Final = 60
"""``rpm``/``tpm``. A per-minute counter that outlives its minute is a false 429."""

RESERVATION_TTL_S: Final = 120
"""An in-flight quota reservation. Long enough for a slow completion, short
enough that a process killed mid-request cannot leak budget forever."""

BREAKER_TTL_S: Final = 3600
"""Breaker state. Longer than the longest cooldown (300s) by a wide margin, so
an open breaker cannot expire into a closed one and undo its own decision."""

EXACT_CACHE_TTL_S: Final = 3600

EXTRACTION_TTL_S: Final = 86_400
"""Extracted document text. Postgres is the source of truth; this is the copy
that saves a round trip."""

EXTRACTION_LOCK_TTL_S: Final = 60
"""Stampede guard on concurrent identical uploads. Held with ``NX``, so it must
expire on its own — the holder may be a process that has since died."""

IDEMPOTENCY_TTL_S: Final = 86_400
"""D6's replay window."""

RATE_LIMIT_TTL_MULTIPLIER: Final = 2
"""Our own rate-limit counters live for twice their window, so the *previous*
window is still readable while the current one fills — which is what a sliding
window needs to interpolate across the boundary."""

JWKS_TTL_S: Final = 43_200
"""Supabase's signing keys. Twelve hours; a ``kid`` miss refreshes early anyway."""

STREAM_ATTEMPTS_TTL_S: Final = 300
"""D1's restart counter. Written for observability and deliberately not read for
control flow (D12) — the in-process loop counter is authoritative."""


# --------------------------------------------------------------------------- #
# Segment validation
# --------------------------------------------------------------------------- #
def _segment(value: str | UUID, *, name: str) -> str:
    """Normalize one component of a key, or refuse to build the key at all.

    Empty and whitespace-bearing segments are the two failures worth catching.
    An empty one silently collapses two distinct keys into one — every user's
    rate limit sharing a counter, say — and whitespace makes a key that is
    impossible to type into ``redis-cli`` while debugging the incident it caused.
    Both are programming errors, so both raise rather than degrade.

    Colons are *not* rejected. ``openai/gpt-oss-20b:free`` is a real model id and
    a legitimate segment; see this module's docstring.
    """
    text = str(value)
    if not text:
        raise ValueError(f"Redis key segment {name!r} is empty")
    if any(character.isspace() for character in text):
        raise ValueError(f"Redis key segment {name!r} contains whitespace: {text!r}")
    return text


def _model_scoped(scope: Scope, provider: str, model: str) -> str:
    """The ``q:{scope}:{provider}:{model}`` prefix every quota key shares."""
    return (
        f"q:{_segment(scope, name='scope')}"
        f":{_segment(provider, name='provider')}"
        f":{_segment(model, name='model')}"
    )


# --------------------------------------------------------------------------- #
# Quota — Phase 3
# --------------------------------------------------------------------------- #
def quota(scope: Scope, provider: str, model: str, window: QuotaWindow) -> str:
    """``q:{scope}:{provider}:{model}:{rpm|rpd|tpm|tpd}`` — one usage counter.

    One builder rather than four, because the four differ only in a literal that
    the type system can check. ``rpm``/``tpm`` expire on
    :data:`QUOTA_WINDOW_TTL_S`; ``rpd``/``tpd`` on :data:`UNTIL_PROVIDER_RESET`.
    """
    return f"{_model_scoped(scope, provider, model)}:{window}"


def quota_reservation(scope: Scope, provider: str, model: str, request_id: str | UUID) -> str:
    """``q:{scope}:{provider}:{model}:res:{req_id}`` — one in-flight reservation.

    Written by the reserve script, read by commit/release. It is what makes a
    failed attempt give its budget back instead of burning it.
    """
    return f"{_model_scoped(scope, provider, model)}:res:{_segment(request_id, name='request_id')}"


def quota_perception_lane(scope: Scope, provider: str, model: str) -> str:
    """``q:{scope}:{provider}:{model}:lane:perception`` — D8's sub-counter.

    Gemini's daily budget is split 50/50 between answering and perceiving. This
    counts the perception half, so plain chat cannot starve file understanding.
    """
    return f"{_model_scoped(scope, provider, model)}:lane:perception"


def user_allocation(user_id: str | UUID, provider: str, model: str) -> str:
    """``q:{user_id}:{provider}:{model}:alloc:rpd`` — D39's personal daily cap.

    **An addition to Contract C, made with sign-off rather than silently** —
    the second one, after Phase 3 Step 10's ``rl:`` window segment (ADR-022);
    ADR-036 records why. Nothing had ever written this key before Phase 6
    Step 7, so there was nothing to migrate.

    Always keyed on the real ``user_id``, never on :data:`SYSTEM_SCOPE` — this
    counter exists specifically for the shared-pool path, where the spending
    scope is ``"system"`` but the cap being checked belongs to one user. Reuses
    the ``:rpd`` window *label* the model's own daily counter uses (so the
    existing reserve/commit/release machinery needs no new window kind to
    enforce it), pointed at a sub-key of its own the way
    :func:`quota_perception_lane` points D8's split at a sub-key rather than
    inventing a fifth :data:`QuotaWindow`.
    """
    return f"{_model_scoped(str(user_id), provider, model)}:alloc:rpd"


# --------------------------------------------------------------------------- #
# Circuit breaker — Phase 2, Step 3
# --------------------------------------------------------------------------- #
def breaker(provider: str, model: str) -> str:
    """``cb:{provider}:{model}`` — the breaker's state hash.

    Fields: ``state``, ``failures``, ``opened_at``, ``cooldown_s``,
    ``probe_holder``. Not scoped by user: a provider being down is a fact about
    the provider, and every user's request should skip it.
    """
    return f"cb:{_segment(provider, name='provider')}:{_segment(model, name='model')}"


# --------------------------------------------------------------------------- #
# Caching — Phase 3
# --------------------------------------------------------------------------- #
def exact_cache(request_hash: str) -> str:
    """``cache:exact:{sha256}`` — D5's exact-match response cache."""
    return f"cache:exact:{_segment(request_hash, name='request_hash')}"


# --------------------------------------------------------------------------- #
# Perception — Phase 4
# --------------------------------------------------------------------------- #
def extraction(file_hash: str) -> str:
    """``extract:{file_hash}`` — extracted text, keyed by content not by upload."""
    return f"extract:{_segment(file_hash, name='file_hash')}"


def extraction_lock(file_hash: str) -> str:
    """``lock:extract:{file_hash}`` — held ``NX`` while one extraction runs.

    Two users uploading the same PDF at the same moment should spend one
    provider's quota, not two.
    """
    return f"lock:extract:{_segment(file_hash, name='file_hash')}"


# --------------------------------------------------------------------------- #
# API surface — Phase 3
# --------------------------------------------------------------------------- #
def idempotency(user_id: str | UUID, idem_key: str) -> str:
    """``idem:{user_id}:{idem_key}`` → ``request_id`` (D6).

    Scoped by user so one client's key cannot collide with — or read — another's.
    """
    return f"idem:{_segment(user_id, name='user_id')}:{_segment(idem_key, name='idempotency_key')}"


def rate_limit(user_id: str | UUID, window: GatewayWindow, window_start: int) -> str:
    """``rl:{user_id}:{rpm|rpd}:{window_start}`` — our own limits, not a provider's.

    ``window_start`` is the epoch second the bucket opened, which is what makes
    the previous bucket addressable while the current one fills — the two-bucket
    interpolation D20 uses, and the one key in Contract C that carries a window
    boundary at all.

    **The ``{window}`` segment is a Phase 3 Step 10 amendment to Contract C**,
    made with sign-off rather than silently. §2.3 froze this key as
    ``rl:{user_id}:{window_start}``, which can only address one window — and
    D20 enforces two. A daily bucket always starts on a multiple of 86,400,
    which is also a valid minute boundary, so under the original format the
    ``rpm`` and ``rpd`` buckets are the same key at every midnight UTC and one
    request increments it twice. ADR-022 records the change; nothing had ever
    written the old format, so there was nothing to migrate.
    """
    if window_start < 0:
        raise ValueError(f"window_start must be a non-negative epoch second, got {window_start}")
    return f"rl:{_segment(user_id, name='user_id')}:{window}:{window_start}"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def jwks_supabase() -> str:
    """``jwks:supabase`` — the shared JWKS cache.

    A function rather than a constant, so every key in this module is reached the
    same way and no call site has to know which ones happen to take arguments.
    Phase 1's :class:`~app.auth.jwt.JwksCache` is in-process; this is the key it
    moves to when the cache becomes shared.
    """
    return "jwks:supabase"


# --------------------------------------------------------------------------- #
# Streaming — Phase 2, Step 9
# --------------------------------------------------------------------------- #
def stream_attempts(message_id: str | UUID) -> str:
    """``stream:{message_id}:attempts`` — D1's restart counter.

    Written, never read (D12). One request is served by one process, so the loop
    counter is authoritative and a distributed counter could not decide anything
    faster or more correctly. This key exists so the restarts are *visible*, and
    as the seam a future multi-instance retry-resume would need.
    """
    return f"stream:{_segment(message_id, name='message_id')}:attempts"
