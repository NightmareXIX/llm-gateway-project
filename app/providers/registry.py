"""Slot table → :class:`ModelSpec`s → adapters.

The indirection this module implements is the reason a client never names a
provider. A request asks for the ``general`` slot; the registry answers with an
ordered candidate list, and swapping which model actually serves it is a YAML
edit that no client can observe.

**The YAML is not read here.** ``app/config.py`` owns every file and every
environment variable in the process, and re-parsing ``providers.yaml`` would give
the app two copies of its own configuration free to disagree. This module takes
an already-validated :class:`ProvidersConfig` and turns it into runtime objects.

**Two halves, split on purity.** :func:`build_model_specs` is a pure function
from config to specs — no client, no keys, cacheable, and what the render
pipeline and Phase 3's quota tracker actually need. :class:`ProviderRegistry`
adds the live parts: adapter instances bound to the process's HTTP client, and
system credentials. Only the second half needs a running application.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple, Protocol

import httpx
from pydantic import SecretStr

from app.config import (
    ConfigError,
    ProvidersConfig,
    Settings,
    get_providers_config,
    get_settings,
)
from app.core.logging import get_logger
from app.providers.base import ProviderAdapter
from app.providers.gemini import GeminiAdapter
from app.providers.groq import GroqAdapter
from app.providers.openrouter import OpenRouterAdapter
from app.providers.types import ModelSpec

logger = get_logger("app.providers.registry")


class UnknownSlot(LookupError):
    """A slot name that is not in the table, or has no enabled candidate.

    A ``LookupError`` rather than an ``AppError``: whether an unknown slot is a
    client's 400 or our own 500 depends on where the name came from, and the
    caller is the only one who knows. Step 8's endpoint translates it.
    """


class AdapterFactory(Protocol):
    """How an adapter is constructed.

    Typed as a callable rather than ``type[HttpProviderAdapter]`` because the
    base class deliberately does *not* satisfy :class:`ProviderAdapter` — it
    supplies plumbing, not ``build_payload`` or ``complete``. What the registry
    requires is "something that, given a client, a base URL and that provider's
    options, hands back a conforming adapter", which is exactly this.

    ``options`` is **not** a Contract A change. This protocol is registry-internal
    and ``__init__`` was never part of the frozen :class:`ProviderAdapter`
    protocol — the alternative, a per-provider construction branch in
    :func:`build_registry`, is the thing the registry exists to avoid.
    """

    def __call__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        options: Mapping[str, str],
    ) -> ProviderAdapter: ...


class ProviderTraits(NamedTuple):
    """Facts that belong to a provider's wire format, not to any of its models.

    ``supports_system_field`` cannot vary between two Groq models — it is a
    property of the API shape — so putting it in ``providers.yaml`` per candidate
    would invite two entries for one provider to contradict each other.
    """

    adapter_class: AdapterFactory
    supports_system_field: bool


_PROVIDER_TRAITS: dict[str, ProviderTraits] = {
    "groq": ProviderTraits(adapter_class=GroqAdapter, supports_system_field=False),
    # `supports_system_field` describes the wire format; it is not a switch the
    # adapter reads. Gemini hoists the system prompt unconditionally because it has
    # nowhere else to put one, and Groq and OpenRouter leave it in the array for the
    # same reason — so a `False` branch in either adapter would be dead code.
    "gemini": ProviderTraits(adapter_class=GeminiAdapter, supports_system_field=True),
    "openrouter": ProviderTraits(adapter_class=OpenRouterAdapter, supports_system_field=False),
}


def build_model_specs(config: ProvidersConfig | None = None) -> dict[str, tuple[ModelSpec, ...]]:
    """Pure: the slot table as ordered, routable :class:`ModelSpec`s.

    Candidates on disabled providers are dropped, and a slot left with none is
    dropped with them — a slot that cannot be served should not be advertised by
    ``/v1/models`` (Phase 3) as though it could.

    ``priority`` comes from each candidate's surviving position, so it stays
    gap-free even when a disabled provider is removed from the middle of a list.
    """
    config = config or get_providers_config()
    specs: dict[str, tuple[ModelSpec, ...]] = {}

    for slot_name, slot in config.slots.items():
        routable = [c for c in slot.candidates if config.providers[c.provider].enabled]
        if not routable:
            continue

        specs[slot_name] = tuple(
            ModelSpec(
                slot=slot_name,
                provider=candidate.provider,
                model=candidate.model,
                context_window=candidate.context_tokens,
                max_output_tokens=candidate.max_output_tokens,
                supports_streaming=candidate.supports_streaming,
                supports_vision="vision" in candidate.capabilities,
                supports_pdf="pdf" in candidate.capabilities,
                supports_system_field=_traits(candidate.provider).supports_system_field,
                max_file_bytes=candidate.max_file_bytes,
                priority=priority,
                reserved_fraction=candidate.reserved_fraction,
            )
            for priority, candidate in enumerate(routable)
        )

    _check_reserved_fraction_agrees_across_slots(specs)
    return specs


def _check_reserved_fraction_agrees_across_slots(specs: dict[str, tuple[ModelSpec, ...]]) -> None:
    """D26/trap 19: a model declared in more than one slot must reserve the same
    fraction of its budget everywhere it appears.

    Phase 4's ``perception`` slot repeats the same two Gemini models ``general``
    and ``fast`` already declare. ``reserved_fraction`` is read by
    ``quota/lanes.py`` per *model*, not per slot — if two slots disagreed, D8's
    answer and perception halves would stop summing to the published limit, and
    nothing downstream would notice. A boot failure here is the whole defence.
    """
    declared: dict[tuple[str, str], tuple[str, float]] = {}

    for slot_name, slot_specs in specs.items():
        for spec in slot_specs:
            prior = declared.get(spec.key)
            if prior is None:
                declared[spec.key] = (slot_name, spec.reserved_fraction)
                continue

            prior_slot, prior_fraction = prior
            if prior_fraction != spec.reserved_fraction:
                raise ConfigError(
                    f"{spec.provider}/{spec.model} declares reserved_fraction="
                    f"{spec.reserved_fraction} in slot {slot_name!r} but "
                    f"{prior_fraction} in slot {prior_slot!r}; every slot that shares a "
                    f"model must agree, or D8's answer/perception split stops summing to one."
                )


def _traits(provider: str) -> ProviderTraits:
    traits = _PROVIDER_TRAITS.get(provider)
    if traits is None:
        raise ConfigError(
            f"provider {provider!r} is configured in providers.yaml but has no adapter; "
            f"known providers are {sorted(_PROVIDER_TRAITS)}"
        )
    return traits


class ProviderRegistry:
    """The live slot table: specs, adapters, and system credentials.

    Built once in the lifespan and read from ``app.state``. Immutable after
    construction — every failure it could have is a configuration problem, and
    those belong at boot rather than in the middle of a request.
    """

    def __init__(
        self,
        *,
        specs: dict[str, tuple[ModelSpec, ...]],
        adapters: dict[str, ProviderAdapter],
        keys: dict[str, SecretStr],
        internal_slots: frozenset[str] = frozenset(),
        private_key_only_slots: frozenset[str] = frozenset(),
    ) -> None:
        self._specs = specs
        self._adapters = adapters
        self._keys = keys
        self._internal_slots = internal_slots
        self._private_key_only_slots = private_key_only_slots

    def slots(self) -> tuple[str, ...]:
        """Every routable, client-facing slot name, in config order.

        An internal slot (Phase 4's ``perception``) is routable — ``candidates()``
        resolves it — but is not something a client should ever see in
        ``GET /v1/models`` or ask for by name, so it is filtered out here rather
        than dropped from ``_specs`` entirely.
        """
        return tuple(name for name in self._specs if name not in self._internal_slots)

    def is_internal(self, slot: str) -> bool:
        """Whether ``slot`` exists only for the gateway to call on itself.

        ``api/v1/chat.py::_validate_slot`` uses this to give a client naming
        ``perception`` explicitly the same 400 a typo gets (D26) — ``candidates()``
        itself does not raise, because the perception lane has to be able to
        resolve the slot by name.
        """
        return slot in self._internal_slots

    def requires_private_key(self, slot: str) -> bool:
        """D41 (§9.7): whether ``slot`` is a slot the shared pool cannot serve.

        Unlike :meth:`is_internal`, such a slot *is* client-facing —
        ``slots()`` still lists it. ``api/v1/models.py`` uses this to hide it
        from a caller who does not hold a private key for every provider in
        its candidate chain, ``api/v1/chat.py::_validate_slot`` uses it to
        refuse an explicit request the same way, and ``routing/selection.py``'s
        ``_fleet`` uses it to keep the slot's candidates out of ``auto``
        entirely — for every caller, key holder included, so that ``auto``
        stays reproducible and its cache entries stay shareable.
        """
        return slot in self._private_key_only_slots

    def candidates(self, slot: str) -> tuple[ModelSpec, ...]:
        """The slot's candidates in failover order. Phase 2's router walks this."""
        try:
            return self._specs[slot]
        except KeyError:
            raise UnknownSlot(
                f"unknown or unroutable slot {slot!r}; available: {sorted(self._specs)}"
            ) from None

    def primary(self, slot: str) -> ModelSpec:
        """The first candidate — the only one Phase 1 ever reaches."""
        return self.candidates(slot)[0]

    def adapter_for(self, provider: str) -> ProviderAdapter:
        try:
            return self._adapters[provider]
        except KeyError:
            raise UnknownSlot(f"no adapter registered for provider {provider!r}") from None

    def adapter_for_spec(self, spec: ModelSpec) -> ProviderAdapter:
        """Sugar for the common pairing."""
        return self.adapter_for(spec.provider)

    def system_key(self, provider: str) -> str:
        """The shared-pool credential for a provider.

        Phase 6 replaces this call with ``resolve_provider_key(user_id,
        provider)``, which checks for a user's own key first and falls back to
        exactly this value (§9.3). Nothing else about the call path changes,
        which is the point of routing credentials through one accessor now.
        """
        try:
            return self._keys[provider].get_secret_value()
        except KeyError:
            raise UnknownSlot(f"no system key configured for provider {provider!r}") from None

    # Registered so a test can assert the whole table, without exposing the
    # mutable dicts the registry is built from.
    def describe(self) -> dict[str, list[str]]:
        """``{slot: ["provider/model", ...]}`` — for the startup log line."""
        return {
            slot: [f"{spec.provider}/{spec.model}" for spec in specs]
            for slot, specs in self._specs.items()
        }


def build_registry(
    *,
    client: httpx.AsyncClient,
    config: ProvidersConfig | None = None,
    settings: Settings | None = None,
) -> ProviderRegistry:
    """Assemble the registry, failing loudly on anything misconfigured.

    Every enabled provider must have an adapter and a non-empty credential. Both
    checks happen here, at boot, on purpose: a missing ``GROQ_API_KEY`` should
    kill the process at startup with a message naming the variable, not surface
    as a 502 partway through a demo.
    """
    config = config or get_providers_config()
    settings = settings or get_settings()

    adapters: dict[str, ProviderAdapter] = {}
    keys: dict[str, SecretStr] = {}

    for name, entry in config.providers.items():
        if not entry.enabled:
            continue

        traits = _traits(name)
        adapters[name] = traits.adapter_class(
            client=client, base_url=entry.base_url, options=entry.options
        )
        keys[name] = _resolve_system_key(name, entry.api_key_env, settings)

    specs = build_model_specs(config)
    if not specs:
        raise ConfigError(
            "no routable slots: every slot in providers.yaml points only at disabled providers"
        )

    internal_slots = frozenset(
        name for name, slot in config.slots.items() if slot.internal and name in specs
    )
    private_key_only_slots = frozenset(
        name for name, slot in config.slots.items() if slot.requires_private_key and name in specs
    )

    registry = ProviderRegistry(
        specs=specs,
        adapters=adapters,
        keys=keys,
        internal_slots=internal_slots,
        private_key_only_slots=private_key_only_slots,
    )
    logger.info(
        "providers.registry_built",
        providers=sorted(adapters),
        slots=registry.describe(),
    )
    return registry


def _resolve_system_key(provider: str, env_name: str, settings: Settings) -> SecretStr:
    """Read a provider's credential off :class:`Settings` by the name YAML declares.

    Indirect on purpose. ``app/config.py``'s standing rule is that nothing else
    in the app touches ``os.environ``; ``api_key_env`` names a *settings field*,
    which keeps the YAML honest about where the value comes from while leaving
    the loading, validation and secret-wrapping in one place.
    """
    value = getattr(settings, env_name, None)

    if value is None:
        raise ConfigError(
            f"provider {provider!r} is enabled and declares api_key_env={env_name!r}, "
            f"but Settings has no such field. Add it to app/config.py and .env.example."
        )
    if not isinstance(value, SecretStr):
        raise ConfigError(
            f"Settings.{env_name} must be a SecretStr so it cannot be logged by accident, "
            f"got {type(value).__name__}."
        )
    if not value.get_secret_value().strip():
        raise ConfigError(
            f"provider {provider!r} is enabled but {env_name} is empty. "
            f"Set it, or set enabled: false in config/providers.yaml."
        )

    return value


def _conformance_check(client: httpx.AsyncClient) -> tuple[ProviderAdapter, ...]:
    """Never called. Its return type is the assertion.

    If any adapter drifts out of structural conformance with
    :class:`ProviderAdapter` — a renamed method, a changed signature — this fails
    under mypy, so ``make typecheck`` catches it rather than the contract suite
    catching it later. Every adapter gets a line here.

    Worth noting what this proves that the contract suite does not: conformance is
    *structural*, so nothing at runtime would object to an adapter missing a method
    the router never happens to call on it. This is the only place that objects.
    """
    return (
        GroqAdapter(client=client, base_url="https://example.invalid"),
        GeminiAdapter(client=client, base_url="https://example.invalid"),
        OpenRouterAdapter(client=client, base_url="https://example.invalid"),
    )
