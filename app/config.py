"""Typed configuration: environment settings plus the YAML provider/limit tables.

Two rules govern this module.

1. Nothing here is optional-by-accident. A missing environment variable raises
   :class:`ConfigError` naming the variable, at import of the settings object —
   never a ``None`` that surfaces as a 500 later.
2. Nothing else in the app reads ``os.environ`` or parses ``config/*.yaml``.
   Everything goes through the cached accessors at the bottom of this file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"

Environment = Literal["dev", "test", "prod"]


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or internally inconsistent.

    Always fatal. It is raised during startup so the process dies at boot rather
    than serving traffic with a half-configured gateway.
    """


# --------------------------------------------------------------------------- #
# Environment settings
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    """Environment-derived configuration. Every field is required unless defaulted."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    ENV: Environment = "dev"

    DATABASE_URL: str

    DB_DISABLE_PREPARED_STATEMENTS: bool = False
    """Turn off asyncpg's prepared-statement cache. Required behind a *transaction*
    pooler; wrong anywhere else.

    Supabase publishes two pooled endpoints. The **session** pooler (port 5432)
    holds one upstream connection per client connection, and prepared statements
    work normally. The **transaction** pooler (port 6543) hands a different
    upstream connection to each transaction, so a statement prepared on one and
    executed on another either is not found or collides with a same-named
    statement from someone else — surfacing as ``DuplicatePreparedStatementError``
    under any concurrency at all, not as a graceful degradation.

    Declared rather than sniffed out of the URL's port. The failure is total, the
    cause is invisible in the traceback, and the fix should be something an
    operator stated deliberately — not something a regex guessed and got wrong
    the first time the connection string is formatted differently."""

    REDIS_URL: str

    SUPABASE_URL: str
    SUPABASE_JWT_AUDIENCE: str

    REQUIRE_VERIFIED_EMAIL: bool = True
    """Reject sessions whose token does not assert a confirmed email address.

    Defence in depth. With "Confirm email" on — which is the project's standing
    configuration in *every* environment — Supabase should never mint a session
    for an unconfirmed user, so this should never fire. It exists because the
    check is nearly free and the failure it guards against is silent.

    The switch exists for one reason: the claim is read from ``user_metadata``,
    which is Supabase's shape, not a standard. If they move it, a missing claim
    reads as unverified and every user is locked out. Better an env var than an
    emergency redeploy. Default fail-closed; ``create_app`` logs which way it is
    set so the answer to "why is everyone getting 401s?" is in the first line of
    the startup log."""

    GROQ_API_KEY: SecretStr
    GEMINI_API_KEY: SecretStr
    OPENROUTER_API_KEY: SecretStr
    """The three provider credentials, all required, all read by
    ``registry._resolve_system_key`` through the ``api_key_env`` name each
    provider declares in ``providers.yaml``.

    Required even while Gemini and OpenRouter are ``enabled: false`` — the same
    reasoning that made ``REDIS_URL`` required through the whole of Phase 1. A
    config surface that grows a variable the day a feature is switched on is a
    config surface that breaks the deploy that switches it on; this way the
    variable is already set, and turning the provider on is a YAML edit that
    cannot fail on a missing secret."""

    ROUTING_LATENCY_RANKING: bool = True
    """D11: reorder ``auto``'s candidate list by observed latency.

    On by default. Off restores pure ``providers.yaml`` declaration order, which
    is also what a cold process does anyway — the ranking only applies to
    candidates with enough successful samples to have earned a position.

    It exists as a switch because the standing caveat of ranking by speed with no
    quota filter (Phase 3) is that it leans toward whichever provider is closest
    to its rate limit: Groq is the fastest thing in the pool *and* has the
    tightest TPM ceiling. A flag is cheaper than a revert when the thing being
    debugged is the router itself."""

    ENCRYPTION_KEY: SecretStr
    """Fernet key for BYOK provider-credential encryption (``app/core/crypto.py``)."""

    QUOTA_ENFORCEMENT: bool = True
    """Phase 3 D15: reserve budget before every attempt and fail closed when Redis
    cannot confirm one is available.

    On by default — this *is* the phase's behaviour, not an opt-in. Off, the
    tracker is never constructed, no reservation is made, and the gateway goes
    back to Phase 2's reactive failover on a real 429. It exists for the same
    reason ``ROUTING_LATENCY_RANKING`` does: when the router itself is what is
    being debugged, a flag is cheaper than a revert — and because quota fails
    closed, this is also the escape hatch from a self-inflicted total outage if
    Redis is down and enforcement would otherwise refuse every request.
    ``/readyz`` reads this same flag, so an instance with enforcement off does not
    fail readiness on a dependency it is not using."""

    QUOTA_HEADROOM_FRACTION: float = Field(default=0.1, ge=0.0, lt=1.0)
    """Fraction of every published limit held back before the tracker ever sees
    it (D16). A fixed-window counter permits up to 2x the limit across a reset
    boundary; this is what keeps the effective ceiling under the real one by more
    than that jitter costs. A gateway whose entire premise is free-tier
    aggregation should be under-spending its budgets on purpose."""

    CACHE_EXACT_ENABLED: bool = True
    """D5/D19: serve identical, deterministic requests from
    ``cache:exact:{sha256}`` instead of calling a provider.

    On by default. Off, every request is a cache ``BYPASS`` and the gateway
    behaves as if the cache did not exist — useful when the thing under debug is
    whether a *response* is correct, and a stale or wrongly-scoped cache hit
    would be indistinguishable from a real answer."""

    RATE_LIMIT_ENABLED: bool = True
    """D20: enforce the gateway's own per-user sliding-window limit on
    ``POST /v1/chat/completions``, independent of every upstream provider limit.

    On by default. Off removes the gateway's only protection against one
    enthusiastic caller draining a shared free-tier pool for everyone else — this
    switch exists for load-testing the gateway itself, not for production use."""

    @property
    def is_production(self) -> bool:
        return self.ENV == "prod"

    @property
    def supabase_auth_url(self) -> str:
        """Base of the Supabase Auth (GoTrue) API. Also the JWT ``iss`` claim."""
        return f"{self.SUPABASE_URL.rstrip('/')}/auth/v1"

    @property
    def supabase_jwks_url(self) -> str:
        """Where the project's public signing keys live."""
        return f"{self.supabase_auth_url}/.well-known/jwks.json"


def _describe_settings_error(exc: ValidationError) -> str:
    """Turn a pydantic ValidationError into a message that names the variables."""
    missing: list[str] = []
    invalid: list[str] = []

    for error in exc.errors():
        name = ".".join(str(part) for part in error["loc"]) or "<root>"
        if error["type"] == "missing":
            missing.append(name)
        else:
            invalid.append(f"{name}: {error['msg']}")

    lines = ["Invalid gateway configuration."]
    if missing:
        lines.append("Missing required environment variable(s):")
        lines.extend(f"  - {name}" for name in missing)
    if invalid:
        lines.append("Invalid environment variable(s):")
        lines.extend(f"  - {detail}" for detail in invalid)
    lines.append("See .env.example for the full list.")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings. Raises :class:`ConfigError` naming what is wrong."""
    try:
        return Settings()  # values come from the environment / .env
    except ValidationError as exc:
        raise ConfigError(_describe_settings_error(exc)) from exc


# --------------------------------------------------------------------------- #
# config/providers.yaml
# --------------------------------------------------------------------------- #
class ProviderEntry(BaseModel):
    """One upstream provider. Disabled providers are parsed but never routed to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    base_url: str
    api_key_env: str

    options: dict[str, str] = Field(default_factory=dict)
    """Non-secret, per-deployment settings handed to the adapter at construction.

    OpenRouter's ``HTTP-Referer`` and ``X-Title`` attribution headers are the
    reason this exists: they are not credentials, they differ per deployment, and
    they belong to exactly one provider. Carrying them here keeps the registry
    free of per-provider construction branches — every adapter takes ``options``
    and the ones with nothing to configure ignore it.

    Secrets never go in here. This file is checked in; ``api_key_env`` above is
    the only path a credential takes."""


KNOWN_CAPABILITIES = frozenset({"text", "vision", "pdf"})
"""Modalities a candidate may declare.

Closed rather than free-form. A typo in an open set (``visoin``) reads as "this
model has no vision support" and silently routes every image down the extraction
lane — a capability regression that looks exactly like correct behaviour. Closing
the set turns it into a boot failure instead.
"""


class SlotCandidate(BaseModel):
    """One (provider, model) pair a slot may be served by, in failover order.

    This is the config-side half of :class:`app.providers.types.ModelSpec`. Every
    field here is a fact about the *model*; provider-wide wire-shape traits — like
    whether the system prompt is a top-level field — live in the registry's
    provider table, because they cannot vary between two models of one provider.

    Ordering is meaning: a candidate's position in :attr:`Slot.candidates` becomes
    its ``ModelSpec.priority``. A separate priority field would be a second source
    of truth for the same fact, free to drift out of step with the list it orders.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    capabilities: tuple[str, ...] = ()

    supports_streaming: bool = True
    max_file_bytes: int | None = Field(default=None, gt=0)
    """Largest file this model accepts natively. ``None`` means "no native file
    input at all", which is Groq's case and every text-only model's."""

    reserved_fraction: float = Field(default=0.0, ge=0.0, lt=1.0)
    """D8: the share of this model's budget held back for the perception lane.

    Zero everywhere in Phase 1. Gemini goes to 0.5 in Phase 3, when
    ``quota/lanes.py`` is the thing that reads it.
    """

    @model_validator(mode="after")
    def _capabilities_are_known(self) -> SlotCandidate:
        unknown = sorted(set(self.capabilities) - KNOWN_CAPABILITIES)
        if unknown:
            raise ValueError(
                f"{self.provider}/{self.model} declares unknown capabilities {unknown}; "
                f"known values are {sorted(KNOWN_CAPABILITIES)}"
            )
        return self


class Slot(BaseModel):
    """A logical model slot the client asks for, e.g. ``general``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    candidates: tuple[SlotCandidate, ...] = Field(min_length=1)


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    providers: dict[str, ProviderEntry]
    slots: dict[str, Slot]

    @model_validator(mode="after")
    def _candidates_reference_known_providers(self) -> ProvidersConfig:
        for slot_name, slot in self.slots.items():
            for candidate in slot.candidates:
                if candidate.provider not in self.providers:
                    raise ValueError(
                        f"slot {slot_name!r} routes to unknown provider {candidate.provider!r}"
                    )
        return self

    def enabled_slots(self) -> dict[str, Slot]:
        """Slots with at least one candidate on an enabled provider."""
        return {
            name: slot
            for name, slot in self.slots.items()
            if any(self.providers[c.provider].enabled for c in slot.candidates)
        }


# --------------------------------------------------------------------------- #
# config/limits.yaml
# --------------------------------------------------------------------------- #
ResetKind = Literal["rolling_60s", "fixed_daily_utc", "fixed_daily_pt"]


class ResetPolicy(BaseModel):
    """How each window resets. ``None`` means the provider publishes no such window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rpm: ResetKind | None = None
    rpd: ResetKind | None = None
    tpm: ResetKind | None = None
    tpd: ResetKind | None = None


class ModelLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rpm: int | None = Field(default=None, gt=0)
    rpd: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)
    tpd: int | None = Field(default=None, gt=0)
    reset: ResetPolicy = Field(default_factory=ResetPolicy)


class GatewayLimits(BaseModel):
    """D20: the gateway's own per-user limit for one ``users.tier`` value.

    Unrelated to the provider table above it — this protects the gateway's own
    capacity from its own callers, not a provider's key from getting banned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rpm: int = Field(gt=0)
    rpd: int = Field(gt=0)


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    limits: dict[str, dict[str, ModelLimits]]
    gateway: dict[str, GatewayLimits]
    """Keyed by ``users.tier`` (``free``, ``plus``). Required rather than
    defaulted: ``extra="forbid"`` already fails startup if the YAML carries a key
    this model does not know, and the absence of a default makes the reverse true
    too — the YAML block and this field can only ever land in the same commit."""

    def for_model(self, provider: str, model: str) -> ModelLimits | None:
        return self.limits.get(provider, {}).get(model)


# --------------------------------------------------------------------------- #
# YAML loading
# --------------------------------------------------------------------------- #
def _load_yaml_model[ModelT: BaseModel](model: type[ModelT], path: Path) -> ModelT:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc

    try:
        parsed: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(f"{path} must contain a top-level mapping, got {type(parsed).__name__}")

    try:
        return model.model_validate(parsed)
    except ValidationError as exc:
        raise ConfigError(f"{path} failed validation:\n{exc}") from exc


@lru_cache(maxsize=1)
def get_providers_config() -> ProvidersConfig:
    return _load_yaml_model(ProvidersConfig, CONFIG_DIR / "providers.yaml")


@lru_cache(maxsize=1)
def get_limits_config() -> LimitsConfig:
    return _load_yaml_model(LimitsConfig, CONFIG_DIR / "limits.yaml")


def validate_startup_config() -> None:
    """Force every config source to load. Called from the lifespan, before serving.

    Any problem raises :class:`ConfigError` and the process fails to start.
    """
    get_settings()
    get_providers_config()
    get_limits_config()
