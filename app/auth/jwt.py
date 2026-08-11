"""Supabase session tokens: JWKS fetching, caching, and verification.

Supabase owns registration, passwords, email confirmation and OAuth (D7). What
arrives here is a signed JWT, and this module's whole job is to decide whether to
believe it — then hand back claims that the rest of the gateway can trust without
re-checking anything.

**Asymmetric only.** Supabase signs with an ES256 or RS256 key whose public half
is published at ``/auth/v1/.well-known/jwks.json``. Tokens are rejected before a
key is even looked up unless their ``alg`` is one of those two. Accepting HS256
alongside a JWKS-sourced key is the classic algorithm-confusion attack: the
attacker signs a token of their own using the *public* key bytes as an HMAC
secret, and a library that trusts the header's ``alg`` verifies it happily.

**Failures say as little as possible.** Signature mismatch, wrong audience, wrong
issuer, unknown key id and a forbidden algorithm all return the same
``invalid_token``. The two exceptions are deliberate, because the client's
correct response differs: ``token_expired`` means refresh the session, and
``email_not_verified`` means show "check your inbox", not a login screen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JOSEError

from app.config import Settings
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.errors import ServiceUnavailable, Unauthenticated
from app.core.logging import get_logger

logger = get_logger("app.auth.jwt")

ALLOWED_ALGORITHMS: frozenset[str] = frozenset({"ES256", "RS256"})
"""The only signature algorithms this gateway will verify. See the module docstring."""

JWKS_TTL_S = 12 * 60 * 60
"""How long a fetched key set is trusted without re-fetching.

Mirrors the 12h TTL of the ``jwks:supabase`` Redis key in Contract C, which
replaces this in-process cache in Phase 3 once there is more than one instance."""

JWKS_MIN_REFRESH_INTERVAL_S = 60.0
"""Floor between two outbound JWKS fetches.

Without it, a caller sending tokens with a made-up ``kid`` turns each inbound
request into an outbound one — a free amplification vector pointed at Supabase,
using our egress."""

CLOCK_SKEW_LEEWAY_S = 30
"""Tolerance on ``exp``/``iat``. Two correctly-configured servers still disagree
by a second or two, and a user should not be logged out by NTP drift."""

CODE_INVALID_TOKEN = "invalid_token"
CODE_TOKEN_EXPIRED = "token_expired"
CODE_EMAIL_NOT_VERIFIED = "email_not_verified"


@dataclass(frozen=True, slots=True)
class Claims:
    """The parts of a verified token the gateway actually uses."""

    sub: UUID
    """Supabase's user id. Becomes ``users.id`` and every quota scope."""

    email: str
    email_verified: bool
    exp: int
    raw: dict[str, Any]
    """The full verified claim set, for logging and for whatever Phase 6 needs."""


def _invalid_token(reason: str, **context: Any) -> Unauthenticated:
    """Log precisely, respond vaguely.

    ``reason`` goes to the log so an operator can tell a clock-skew problem from
    a forged token; the client is told only that the token did not check out.
    """
    logger.warning("auth.token_rejected", reason=reason, **context)
    return Unauthenticated(
        "The provided session token is not valid.",
        code=CODE_INVALID_TOKEN,
    )


# --------------------------------------------------------------------------- #
# JWKS
# --------------------------------------------------------------------------- #
class JwksCache:
    """Supabase's public signing keys, fetched once and reused.

    Shared process-wide and created in the lifespan, so every request reuses one
    cache and one HTTP client. The client is injected rather than constructed
    here: a per-call ``AsyncClient`` would open a fresh TLS connection to
    Supabase on every cache miss.
    """

    def __init__(
        self,
        *,
        jwks_url: str,
        client: httpx.AsyncClient,
        clock: Clock = SYSTEM_CLOCK,
        ttl_s: float = JWKS_TTL_S,
        min_refresh_interval_s: float = JWKS_MIN_REFRESH_INTERVAL_S,
    ) -> None:
        self._jwks_url = jwks_url
        self._client = client
        self._clock = clock
        self._ttl_s = ttl_s
        self._min_refresh_interval_s = min_refresh_interval_s

        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> dict[str, Any]:
        """The JWK for ``kid``, refreshing at most once if it is not cached.

        A miss is the normal signal that Supabase rotated its signing key, so it
        triggers a refresh rather than an error — that is the whole point of
        publishing a ``kid``. Raises :class:`Unauthenticated` if the key is still
        unknown afterwards.
        """
        cached = self._cached(kid)
        if cached is not None:
            return cached

        await self._refresh()

        key = self._keys.get(kid)
        if key is None:
            raise _invalid_token("unknown_kid", kid=kid)
        return key

    def _cached(self, kid: str) -> dict[str, Any] | None:
        """A cached key, if we have one and the set has not aged out."""
        if self._fetched_at is None:
            return None
        if (self._clock.now() - self._fetched_at).total_seconds() >= self._ttl_s:
            return None
        return self._keys.get(kid)

    async def _refresh(self) -> None:
        """Re-fetch the key set, subject to the lock and the interval floor."""
        waiting_since = self._clock.now()

        async with self._lock:
            # Another caller may have refreshed while we queued on the lock.
            if self._fetched_at is not None and self._fetched_at >= waiting_since:
                return

            now = self._clock.now()
            if (
                self._last_attempt_at is not None
                and (now - self._last_attempt_at).total_seconds() < self._min_refresh_interval_s
            ):
                logger.debug("auth.jwks_refresh_throttled")
                return

            self._last_attempt_at = now

            try:
                keys = await self._fetch()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("auth.jwks_fetch_failed", error=str(exc), url=self._jwks_url)
                if not self._keys:
                    # Nothing cached and nothing fetched: we cannot verify any
                    # token. That is our outage, not the caller's bad request —
                    # 503, not 401.
                    raise ServiceUnavailable(
                        "Authentication is temporarily unavailable.",
                        code="jwks_unavailable",
                    ) from exc
                # We do have keys, just stale ones. Serving with them beats
                # logging everyone out because Supabase had a bad minute.
                return

            self._keys = keys
            self._fetched_at = self._clock.now()
            logger.info("auth.jwks_refreshed", key_count=len(keys))

    async def _fetch(self) -> dict[str, dict[str, Any]]:
        """GET the JWKS document and index it by ``kid``."""
        response = await self._client.get(self._jwks_url)
        response.raise_for_status()
        document = response.json()

        if not isinstance(document, dict):
            raise ValueError("JWKS document is not a JSON object")

        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise ValueError("JWKS document contains no keys")

        keys: dict[str, dict[str, Any]] = {}
        for key in raw_keys:
            if not isinstance(key, dict):
                continue
            kid = key.get("kid")
            # A key with no `kid` cannot be selected by a token header, and one
            # signed with an algorithm we refuse is not worth caching.
            if isinstance(kid, str) and key.get("alg") in ALLOWED_ALGORITHMS:
                keys[kid] = key

        if not keys:
            raise ValueError("JWKS document contains no usable asymmetric keys")
        return keys


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
async def verify_token(token: str, *, jwks: JwksCache, settings: Settings) -> Claims:
    """Verify a Supabase session token and return its claims.

    Raises :class:`Unauthenticated` for anything wrong with the token, and
    :class:`ServiceUnavailable` if the signing keys cannot be reached at all.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JOSEError as exc:
        raise _invalid_token("malformed_header", error=str(exc)) from exc

    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        # Worth a distinct log reason: a symmetric alg arriving here is either a
        # misconfigured project or someone probing for algorithm confusion.
        raise _invalid_token("algorithm_not_allowed", alg=str(algorithm))

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise _invalid_token("missing_kid")

    key = await jwks.get_key(kid)

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=sorted(ALLOWED_ALGORITHMS),
            audience=settings.SUPABASE_JWT_AUDIENCE,
            issuer=settings.supabase_auth_url,
            options={
                "require_exp": True,
                "require_iat": True,
                "leeway": CLOCK_SKEW_LEEWAY_S,
            },
        )
    except ExpiredSignatureError as exc:
        # The one failure the client can fix by itself.
        logger.info("auth.token_expired")
        raise Unauthenticated(
            "Your session has expired. Sign in again.",
            code=CODE_TOKEN_EXPIRED,
        ) from exc
    except JOSEError as exc:
        raise _invalid_token("decode_failed", error=str(exc)) from exc

    return _claims_from(claims, settings=settings)


def _claims_from(claims: dict[str, Any], *, settings: Settings) -> Claims:
    """Post-signature checks. Everything here assumes the token is authentic."""
    # `audience=` is passed to decode, but python-jose treats a token with no
    # `aud` at all as acceptable. Assert its presence rather than rely on that.
    if not _audience_matches(claims.get("aud"), settings.SUPABASE_JWT_AUDIENCE):
        raise _invalid_token("audience_mismatch")

    # Anonymous sign-ins are off by default in Supabase. Refusing the claim here
    # keeps them off by construction — a dashboard toggle flipped by accident
    # does not silently create unauthenticated users with a user_id.
    if claims.get("is_anonymous") is True:
        raise _invalid_token("anonymous_session")

    raw_sub = claims.get("sub")
    if not isinstance(raw_sub, str):
        raise _invalid_token("missing_sub")
    try:
        sub = UUID(raw_sub)
    except ValueError as exc:
        raise _invalid_token("sub_not_a_uuid") from exc

    raw_email = claims.get("email")
    if not isinstance(raw_email, str) or not raw_email:
        # Every Supabase email/password or OAuth user has one, and `users.email`
        # is NOT NULL. A token without it is not something we can mirror.
        raise _invalid_token("missing_email")

    email_verified = _email_verified(claims)
    if email_verified is None:
        # The claim is absent entirely — a Supabase shape change, not a user
        # problem. Fail closed by default, but let an operator open it back up
        # with an env var instead of a redeploy.
        logger.warning("auth.email_verified_claim_absent")
        if settings.REQUIRE_VERIFIED_EMAIL:
            raise Unauthenticated(
                "Confirm your email address to continue.",
                code=CODE_EMAIL_NOT_VERIFIED,
            )
        email_verified = False
    elif not email_verified and settings.REQUIRE_VERIFIED_EMAIL:
        # Should be unreachable: with "Confirm email" on, Supabase does not issue
        # a session to an unconfirmed user. Defence in depth — if that setting is
        # ever flipped off, this is what keeps unconfirmed addresses out.
        logger.warning("auth.email_not_verified", user_id=str(sub))
        raise Unauthenticated(
            "Confirm your email address to continue. Check your inbox for the link.",
            code=CODE_EMAIL_NOT_VERIFIED,
        )

    exp = claims.get("exp")
    return Claims(
        sub=sub,
        email=raw_email,
        email_verified=email_verified,
        exp=int(exp) if isinstance(exp, int | float) else 0,
        raw=claims,
    )


def _audience_matches(claim: Any, expected: str) -> bool:
    """``aud`` is either a string or a list of them, per RFC 7519."""
    if isinstance(claim, str):
        return claim == expected
    if isinstance(claim, list):
        return expected in claim
    return False


def _email_verified(claims: dict[str, Any]) -> bool | None:
    """Read the verification flag. ``None`` means the claim is absent entirely.

    Supabase puts it in ``user_metadata``, not at the top level. Some GoTrue
    versions also emit a top-level copy, so that is checked as a fallback — but
    only as a fallback, and a missing claim in both places stays ``None`` rather
    than quietly becoming ``False``. The caller decides what absence means.
    """
    metadata = claims.get("user_metadata")
    if isinstance(metadata, dict) and "email_verified" in metadata:
        return bool(metadata["email_verified"])
    if "email_verified" in claims:
        return bool(claims["email_verified"])
    return None
