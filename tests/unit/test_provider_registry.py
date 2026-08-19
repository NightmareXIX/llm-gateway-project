"""The slot table: config → :class:`ModelSpec`s → adapters.

The registry is where a configuration mistake should become a boot failure. Every
test here that asserts a ``ConfigError`` is asserting that a particular class of
problem cannot reach a request.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.config import (
    ConfigError,
    ProviderEntry,
    ProvidersConfig,
    Settings,
    Slot,
    SlotCandidate,
    get_providers_config,
    get_settings,
)
from app.providers.gemini import GeminiAdapter
from app.providers.groq import GroqAdapter
from app.providers.openrouter import OpenRouterAdapter
from app.providers.registry import (
    ProviderRegistry,
    UnknownSlot,
    build_model_specs,
    build_registry,
)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))


def _config(**overrides: object) -> ProvidersConfig:
    """A two-provider table where the second is disabled."""
    base: dict[str, object] = {
        "version": 1,
        "providers": {
            "groq": ProviderEntry(
                enabled=True, base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY"
            ),
            "gemini": ProviderEntry(
                enabled=False, base_url="https://example.invalid", api_key_env="GEMINI_API_KEY"
            ),
        },
        "slots": {
            "general": Slot(
                description="two candidates, one routable",
                candidates=(
                    SlotCandidate(
                        provider="gemini",
                        model="gemini-flash",
                        context_tokens=1_000_000,
                        max_output_tokens=8192,
                        capabilities=("text", "vision", "pdf"),
                        max_file_bytes=20_000_000,
                        reserved_fraction=0.5,
                    ),
                    SlotCandidate(
                        provider="groq",
                        model="llama-3.3-70b-versatile",
                        context_tokens=131072,
                        max_output_tokens=32768,
                        capabilities=("text",),
                    ),
                ),
            ),
            "gemini_only": Slot(
                description="nothing routable at all",
                candidates=(
                    SlotCandidate(
                        provider="gemini",
                        model="gemini-flash",
                        context_tokens=1_000_000,
                        max_output_tokens=8192,
                    ),
                ),
            ),
        },
    }
    base.update(overrides)
    return ProvidersConfig.model_validate(base)


# --------------------------------------------------------------------------- #
# build_model_specs — pure
# --------------------------------------------------------------------------- #
def test_capabilities_become_the_supports_flags() -> None:
    specs = build_model_specs(_config())
    groq_spec = specs["general"][0]

    assert groq_spec.supports_vision is False
    assert groq_spec.supports_pdf is False
    assert groq_spec.supports_mime("application/pdf") is False
    assert groq_spec.supports_mime("image/png") is False


def test_candidates_on_disabled_providers_are_dropped() -> None:
    """Advertising a slot that cannot be served is how `/v1/models` starts lying."""
    specs = build_model_specs(_config())

    assert [spec.provider for spec in specs["general"]] == ["groq"]
    assert "gemini_only" not in specs


def test_priority_is_gap_free_after_a_disabled_candidate_is_removed() -> None:
    """Derived from the surviving position, not the configured one — a priority
    of 1 with no 0 above it would make Phase 3's ordering read as a bug."""
    specs = build_model_specs(_config())

    assert [spec.priority for spec in specs["general"]] == [0]


def test_the_wire_shape_trait_comes_from_the_provider_not_the_yaml() -> None:
    """`supports_system_field` cannot vary between two models of one provider, so
    two YAML entries must not be free to disagree about it."""
    specs = build_model_specs(_config())

    assert specs["general"][0].supports_system_field is False


def test_context_and_output_limits_are_carried_through() -> None:
    specs = build_model_specs(_config())
    spec = specs["general"][0]

    assert spec.context_window == 131072
    assert spec.max_output_tokens == 32768
    assert spec.reserved_fraction == 0.0
    assert spec.key == ("groq", "llama-3.3-70b-versatile")


def test_the_checked_in_config_produces_the_slots_phase_one_expects() -> None:
    """Guards the real `config/providers.yaml`, not a synthetic one."""
    specs = build_model_specs(get_providers_config())

    # `build_model_specs` is the pure, slot-level view — it carries `perception`
    # too, because the perception lane (Phase 4) resolves it by name. Filtering
    # it out of what a client can see is `ProviderRegistry.slots()`'s job, not
    # this function's.
    assert set(specs) == {"general", "fast", "perception"}
    assert specs["general"][0].provider == "groq"
    assert specs["general"][0].slot == "general"


def test_a_provider_with_no_adapter_fails_loudly() -> None:
    config = _config(
        providers={
            "anthropic": ProviderEntry(
                enabled=True, base_url="https://example.invalid", api_key_env="X"
            )
        },
        slots={
            "general": Slot(
                description="unknown provider",
                candidates=(
                    SlotCandidate(
                        provider="anthropic",
                        model="claude",
                        context_tokens=100,
                        max_output_tokens=10,
                    ),
                ),
            )
        },
    )

    with pytest.raises(ConfigError, match="no adapter"):
        build_model_specs(config)


def test_a_model_disagreeing_on_reserved_fraction_across_slots_fails_loudly() -> None:
    """D26/trap 19: the same (provider, model) pair in two slots must agree on
    how much of its budget the perception lane fences off, or D8's two halves
    stop summing to the published limit and nothing downstream would notice."""
    config = _config(
        slots={
            "general": Slot(
                description="reserves half for perception",
                candidates=(
                    SlotCandidate(
                        provider="gemini",
                        model="gemini-flash",
                        context_tokens=1_000_000,
                        max_output_tokens=8192,
                        reserved_fraction=0.5,
                    ),
                ),
            ),
            "perception": Slot(
                description="reserves nothing — a drift from `general`'s 0.5",
                internal=True,
                candidates=(
                    SlotCandidate(
                        provider="gemini",
                        model="gemini-flash",
                        context_tokens=1_000_000,
                        max_output_tokens=8192,
                        reserved_fraction=0.0,
                    ),
                ),
            ),
        },
        providers={
            "gemini": ProviderEntry(
                enabled=True, base_url="https://example.invalid", api_key_env="GEMINI_API_KEY"
            ),
        },
    )

    with pytest.raises(ConfigError, match="reserved_fraction"):
        build_model_specs(config)


def test_a_model_agreeing_on_reserved_fraction_across_slots_is_fine() -> None:
    config = _config(
        slots={
            "general": Slot(
                description="reserves half for perception",
                candidates=(
                    SlotCandidate(
                        provider="gemini",
                        model="gemini-flash",
                        context_tokens=1_000_000,
                        max_output_tokens=8192,
                        reserved_fraction=0.5,
                    ),
                ),
            ),
            "perception": Slot(
                description="agrees with `general`",
                internal=True,
                candidates=(
                    SlotCandidate(
                        provider="gemini",
                        model="gemini-flash",
                        context_tokens=1_000_000,
                        max_output_tokens=8192,
                        reserved_fraction=0.5,
                    ),
                ),
            ),
        },
        providers={
            "gemini": ProviderEntry(
                enabled=True, base_url="https://example.invalid", api_key_env="GEMINI_API_KEY"
            ),
        },
    )

    specs = build_model_specs(config)
    assert specs["general"][0].reserved_fraction == 0.5
    assert specs["perception"][0].reserved_fraction == 0.5


# --------------------------------------------------------------------------- #
# build_registry — the live half
# --------------------------------------------------------------------------- #
def test_the_registry_resolves_slots_to_adapters_and_keys(client: httpx.AsyncClient) -> None:
    registry = build_registry(client=client, config=_config(), settings=get_settings())

    assert registry.slots() == ("general",)
    assert isinstance(registry.adapter_for("groq"), GroqAdapter)
    assert registry.system_key("groq") == get_settings().GROQ_API_KEY.get_secret_value()
    assert registry.primary("general").model == "llama-3.3-70b-versatile"


def test_disabled_providers_get_no_adapter(client: httpx.AsyncClient) -> None:
    """No adapter means no way to accidentally route to a provider whose key was
    never configured."""
    registry = build_registry(client=client, config=_config(), settings=get_settings())

    with pytest.raises(UnknownSlot):
        registry.adapter_for("gemini")


def test_an_unknown_slot_names_what_is_available(client: httpx.AsyncClient) -> None:
    registry = build_registry(client=client, config=_config(), settings=get_settings())

    with pytest.raises(UnknownSlot, match="general"):
        registry.candidates("nope")


def test_adapters_share_the_injected_client(client: httpx.AsyncClient) -> None:
    """One connection pool per process. An adapter that built its own would
    multiply TLS handshakes and leak in every test."""
    registry = build_registry(client=client, config=_config(), settings=get_settings())
    adapter = registry.adapter_for("groq")

    assert isinstance(adapter, GroqAdapter)
    assert adapter._client is client


def test_a_providers_options_reach_its_adapter(client: httpx.AsyncClient) -> None:
    """OpenRouter's attribution headers arrive this way. The registry passes them
    through without knowing what they mean, which is what keeps it free of
    per-provider construction branches."""
    config = _config(
        providers={
            "groq": ProviderEntry(
                enabled=True,
                base_url="https://api.groq.com/openai/v1",
                api_key_env="GROQ_API_KEY",
                options={"X-Title": "LLM Gateway"},
            ),
        },
        slots={
            "general": Slot(
                description="one routable candidate",
                candidates=(
                    SlotCandidate(
                        provider="groq",
                        model="llama-3.3-70b-versatile",
                        context_tokens=131072,
                        max_output_tokens=32768,
                    ),
                ),
            )
        },
    )
    registry = build_registry(client=client, config=config, settings=get_settings())
    adapter = registry.adapter_for("groq")

    assert isinstance(adapter, GroqAdapter)
    assert adapter.options == {"X-Title": "LLM Gateway"}


def test_an_adapter_for_a_provider_with_no_options_gets_an_empty_mapping(
    client: httpx.AsyncClient,
) -> None:
    registry = build_registry(client=client, config=_config(), settings=get_settings())
    adapter = registry.adapter_for("groq")

    assert isinstance(adapter, GroqAdapter)
    assert adapter.options == {}


def test_the_checked_in_config_builds_a_registry_for_all_three_providers(
    client: httpx.AsyncClient,
) -> None:
    """Phase 2 Step 6, end to end through the registry.

    Every slot now has three candidates, so failover spans genuinely different
    providers rather than two Groq models. The candidate *order* is unchanged —
    Groq still leads both slots — which is what keeps the enabling a routing
    addition rather than a routing change.
    """
    registry = build_registry(client=client, settings=get_settings())

    assert registry.slots() == ("general", "fast")
    assert registry.primary("general").provider == "groq"

    assert isinstance(registry.adapter_for("gemini"), GeminiAdapter)
    assert isinstance(registry.adapter_for("openrouter"), OpenRouterAdapter)

    providers = [spec.provider for spec in registry.candidates("general")]
    assert providers == ["groq", "gemini", "openrouter"]

    # The lookup still refuses a provider that is not in the table at all, so the
    # branch that turns a config typo into a loud failure stays covered.
    with pytest.raises(UnknownSlot):
        registry.adapter_for("anthropic")


def test_an_internal_slot_is_routable_but_invisible_to_slots(
    client: httpx.AsyncClient,
) -> None:
    """D26: `perception` resolves by name (the extraction lane needs that) but
    is filtered out of `slots()`, which is what `GET /v1/models` and
    `_validate_slot`'s happy path both walk."""
    config = _config(
        slots={
            "general": Slot(
                description="client-facing",
                candidates=(
                    SlotCandidate(
                        provider="groq",
                        model="llama-3.3-70b-versatile",
                        context_tokens=131072,
                        max_output_tokens=32768,
                    ),
                ),
            ),
            "perception": Slot(
                description="internal only",
                internal=True,
                candidates=(
                    SlotCandidate(
                        provider="groq",
                        model="llama-3.3-70b-versatile",
                        context_tokens=131072,
                        max_output_tokens=32768,
                    ),
                ),
            ),
        },
    )
    registry = build_registry(client=client, config=config, settings=get_settings())

    assert registry.slots() == ("general",)
    assert registry.is_internal("perception") is True
    assert registry.is_internal("general") is False
    # Still resolvable directly — the perception lane calls this by name.
    assert len(registry.candidates("perception")) == 1


def test_the_checked_in_config_does_not_expose_perception(client: httpx.AsyncClient) -> None:
    registry = build_registry(client=client, settings=get_settings())

    assert "perception" not in registry.slots()
    assert registry.is_internal("perception") is True
    assert len(registry.candidates("perception")) == 2


def test_an_enabled_provider_with_an_empty_key_kills_startup(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    """The whole reason the registry is built eagerly in the lifespan: a missing
    key should name the variable at boot, not surface as a 502 mid-demo."""
    blank = settings.model_copy(update={"GROQ_API_KEY": SecretStr("   ")})

    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        build_registry(client=client, config=_config(), settings=blank)


def test_an_enabled_provider_whose_key_field_does_not_exist_kills_startup(
    client: httpx.AsyncClient,
) -> None:
    """`api_key_env` names a Settings field. A typo must not silently resolve to
    an empty credential."""
    config = _config(
        providers={
            "groq": ProviderEntry(
                enabled=True,
                base_url="https://api.groq.com/openai/v1",
                api_key_env="GROQ_API_KEYY",
            )
        },
        slots={
            "general": Slot(
                description="one routable candidate",
                candidates=(
                    SlotCandidate(
                        provider="groq",
                        model="llama-3.3-70b-versatile",
                        context_tokens=131072,
                        max_output_tokens=32768,
                    ),
                ),
            )
        },
    )

    with pytest.raises(ConfigError, match="GROQ_API_KEYY"):
        build_registry(client=client, config=config, settings=get_settings())


def test_a_table_with_nothing_routable_kills_startup(client: httpx.AsyncClient) -> None:
    config = _config(
        providers={
            "gemini": ProviderEntry(
                enabled=False, base_url="https://example.invalid", api_key_env="GEMINI_API_KEY"
            )
        },
        slots={
            "general": Slot(
                description="all disabled",
                candidates=(
                    SlotCandidate(
                        provider="gemini",
                        model="gemini-flash",
                        context_tokens=100,
                        max_output_tokens=10,
                    ),
                ),
            )
        },
    )

    with pytest.raises(ConfigError, match="no routable slots"):
        build_registry(client=client, config=config, settings=get_settings())


def test_describe_reports_the_table_for_the_startup_log(client: httpx.AsyncClient) -> None:
    registry = build_registry(client=client, config=_config(), settings=get_settings())

    assert registry.describe() == {"general": ["groq/llama-3.3-70b-versatile"]}


def test_adapter_for_spec_pairs_a_spec_with_its_adapter(client: httpx.AsyncClient) -> None:
    registry = build_registry(client=client, config=_config(), settings=get_settings())
    spec = registry.primary("general")

    assert registry.adapter_for_spec(spec) is registry.adapter_for("groq")


def test_the_registry_holds_no_mutable_view_of_its_inputs(client: httpx.AsyncClient) -> None:
    """Every failure the registry can have is a config problem, and those belong
    at boot rather than partway through traffic."""
    specs = build_model_specs(_config())
    registry = ProviderRegistry(specs=dict(specs), adapters={}, keys={})

    specs.clear()

    assert registry.slots() == ("general",)
