"""Configuration loading — the "fail loudly at startup" promise, made assertable.

Step 1 of the Phase 1 plan asks for exactly one thing from this module: a missing
environment variable must kill the process at boot, naming itself, rather than
becoming a ``None`` that surfaces as a 500 three hours into a demo. That is a
claim about a *message*, and an untested error message is a message that says
whatever it happened to say the day it was written.

**Two hazards this module works around.**

``get_settings`` is ``lru_cache``d and :mod:`tests.conftest` repoints
``DATABASE_URL`` at ``gateway_test`` before clearing it once. Clearing that cache
carelessly mid-suite would let it repopulate from a different environment and
point every later database test somewhere else. The ``isolated_env`` fixture
therefore clears on the way *out* as well as the way in, and restores the
environment through ``monkeypatch`` so the rebuild sees what conftest set.

The repo also has a real ``.env``, and ``env_file=".env"`` resolves relative to
the working directory — so deleting ``DATABASE_URL`` from the environment would
prove nothing while that file is still supplying it. ``isolated_env`` chdirs into
a ``tmp_path`` first, which is what makes "missing" actually mean missing.

The YAML half is driven through ``_load_yaml_model`` against files written into
``tmp_path``, never through the cached ``get_providers_config`` accessors, so no
test here can leave a poisoned table behind for another.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.config import (
    KNOWN_CAPABILITIES,
    ConfigError,
    LimitsConfig,
    ProvidersConfig,
    Settings,
    _load_yaml_model,
    get_limits_config,
    get_providers_config,
    get_settings,
    validate_startup_config,
)
from app.db.session import create_db_engine, driver_connect_args

# --------------------------------------------------------------------------- #
# Environment settings
# --------------------------------------------------------------------------- #
REQUIRED_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "SUPABASE_URL",
    "SUPABASE_JWT_AUDIENCE",
    "GROQ_API_KEY",
    # Required from Phase 2 Step 1, while both providers are still `enabled: false`.
    # The point of listing them here is that the parameterized test below then
    # asserts the same "names itself at boot" promise for them as for the rest.
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "ENCRYPTION_KEY",
)

OPTIONAL_VARS = ("ENV", "REQUIRE_VERIFIED_EMAIL", "ROUTING_LATENCY_RANKING")

BASELINE = {
    "ENV": "test",
    "DATABASE_URL": "postgresql+asyncpg://gateway:gateway@127.0.0.1:5432/gateway",
    "REDIS_URL": "redis://localhost:6379/1",
    "SUPABASE_URL": "https://test-project.supabase.co",
    "SUPABASE_JWT_AUDIENCE": "authenticated",
    "GROQ_API_KEY": "gsk_not_a_real_key",
    "GEMINI_API_KEY": "not_a_real_gemini_key",
    "OPENROUTER_API_KEY": "sk-or-v1-not-a-real-key",
    "ENCRYPTION_KEY": "dGVzdC1mZXJuZXQta2V5LW5vdC1hLXJlYWwtb25lLTAwMD0=",
}


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """A known environment, no ``.env`` in reach, and a cache cleared both ways."""
    monkeypatch.chdir(tmp_path)
    for name in (*REQUIRED_VARS, *OPTIONAL_VARS):
        monkeypatch.delenv(name, raising=False)
    for name, value in BASELINE.items():
        monkeypatch.setenv(name, value)

    get_settings.cache_clear()
    yield
    # Cleared again rather than left populated: this one was built from BASELINE
    # and from a working directory that is about to be deleted.
    get_settings.cache_clear()


@pytest.mark.usefixtures("isolated_env")
def test_a_complete_environment_loads() -> None:
    settings = get_settings()

    assert settings.ENV == "test"
    assert settings.SUPABASE_JWT_AUDIENCE == "authenticated"
    assert settings.GROQ_API_KEY.get_secret_value() == "gsk_not_a_real_key"


@pytest.mark.parametrize("missing", REQUIRED_VARS)
@pytest.mark.usefixtures("isolated_env")
def test_a_missing_variable_fails_at_boot_and_names_itself(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """The Step 1 requirement. Not a ``None`` to be discovered later."""
    monkeypatch.delenv(missing)

    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert missing in message
    assert "Missing required environment variable(s)" in message
    assert ".env.example" in message


@pytest.mark.usefixtures("isolated_env")
def test_every_missing_variable_is_reported_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not one per restart. Fixing config should take one pass, not six."""
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("REDIS_URL")
    monkeypatch.delenv("SUPABASE_URL")

    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "REDIS_URL" in message
    assert "SUPABASE_URL" in message


@pytest.mark.usefixtures("isolated_env")
def test_a_present_but_invalid_value_is_reported_as_invalid_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not setting a variable and setting it wrong are different afternoons."""
    monkeypatch.setenv("ENV", "staging")

    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert "Invalid environment variable(s)" in message
    assert "ENV" in message
    assert "Missing required environment variable(s)" not in message


@pytest.mark.parametrize(
    "env,expected",
    [("dev", False), ("test", False), ("prod", True)],
)
def test_only_prod_is_production(env: str, expected: bool) -> None:
    assert _settings(ENV=env).is_production is expected


@pytest.mark.parametrize("supabase_url", ["https://p.supabase.co", "https://p.supabase.co/"])
def test_the_supabase_urls_derive_without_a_doubled_slash(supabase_url: str) -> None:
    """A trailing slash in the environment is a typo, not a different project."""
    settings = _settings(SUPABASE_URL=supabase_url)

    assert settings.supabase_auth_url == "https://p.supabase.co/auth/v1"
    assert settings.supabase_jwks_url == "https://p.supabase.co/auth/v1/.well-known/jwks.json"


def test_secrets_do_not_survive_being_printed() -> None:
    """§9.8: never logged, never in an error message.

    ``SecretStr`` is what enforces it, and the failure mode if someone widens
    these fields to ``str`` is a provider key in the startup log — silent, and
    only noticed by whoever finds it there.
    """
    secrets = {
        "GROQ_API_KEY": "gsk_the_real_thing",
        "GEMINI_API_KEY": "gemini_the_real_thing",
        "OPENROUTER_API_KEY": "openrouter_the_real_thing",
        "ENCRYPTION_KEY": "fernet_the_real_thing",
    }
    settings = _settings(**secrets)

    for value in secrets.values():
        assert value not in repr(settings)
        assert value not in str(settings)
    # Still reachable when actually needed.
    assert settings.GROQ_API_KEY.get_secret_value() == "gsk_the_real_thing"
    assert settings.GEMINI_API_KEY.get_secret_value() == "gemini_the_real_thing"
    assert settings.OPENROUTER_API_KEY.get_secret_value() == "openrouter_the_real_thing"


def test_settings_are_frozen() -> None:
    """Configuration is read at boot and never edited by a request handler."""
    settings = _settings()

    with pytest.raises(ValidationError):
        settings.ENV = "prod"  # type: ignore[misc]


def test_verified_email_defaults_closed() -> None:
    """The switch exists for a Supabase schema change, not as a convenience."""
    assert _settings().REQUIRE_VERIFIED_EMAIL is True


def test_latency_ranking_defaults_on() -> None:
    """D11 is the behaviour, not the opt-in. The flag exists so a misbehaving
    ranking can be switched off in one deploy — off is the fallback position, and
    it reproduces the config order a cold process already serves."""
    assert _settings().ROUTING_LATENCY_RANKING is True
    assert _settings(ROUTING_LATENCY_RANKING=False).ROUTING_LATENCY_RANKING is False


# --------------------------------------------------------------------------- #
# The transaction-pooler switch (Step 11)
# --------------------------------------------------------------------------- #
def test_prepared_statements_are_left_alone_by_default() -> None:
    """Off for a direct connection and for the session pooler — which is every
    environment except the one that needs it."""
    assert _settings().DB_DISABLE_PREPARED_STATEMENTS is False


def test_the_default_passes_the_driver_nothing_at_all() -> None:
    """Absent, not ``statement_cache_size=None`` or some other neutral sentinel.
    A driver argument that is present-but-harmless is one refactor away from being
    present-and-wrong."""
    assert driver_connect_args(_settings()) == {}


def test_the_switch_turns_off_asyncpgs_statement_cache() -> None:
    """Behind Supabase's transaction pooler this is the difference between a
    working service and ``DuplicatePreparedStatementError`` on every concurrent
    request, so it is worth asserting the value that reaches the driver rather
    than only that the setting parses."""
    assert driver_connect_args(_settings(DB_DISABLE_PREPARED_STATEMENTS=True)) == {
        "statement_cache_size": 0
    }


def test_the_engine_builds_either_way() -> None:
    """Cheap, and it catches the thing a dict-level test cannot: a keyword the
    engine constructor does not accept. Building an engine opens no connection."""
    for disabled in (False, True):
        engine = create_db_engine(_settings(DB_DISABLE_PREPARED_STATEMENTS=disabled))
        assert engine.url.get_backend_name() == "postgresql"


def _settings(**overrides: Any) -> Settings:
    """Build settings from explicit values, touching neither the env nor the cache."""
    return Settings(**{**BASELINE, **overrides})


# --------------------------------------------------------------------------- #
# config/providers.yaml
# --------------------------------------------------------------------------- #
def _providers_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 1,
        "providers": {
            "groq": {
                "enabled": True,
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "GROQ_API_KEY",
            },
            "gemini": {
                "enabled": False,
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key_env": "GEMINI_API_KEY",
            },
        },
        "slots": {
            "general": {
                "description": "Default answering slot.",
                "candidates": [
                    {
                        "provider": "groq",
                        "model": "llama-3.3-70b-versatile",
                        "context_tokens": 131072,
                        "max_output_tokens": 32768,
                        "capabilities": ["text"],
                    }
                ],
            }
        },
    }
    document.update(overrides)
    return document


def _write(tmp_path: Path, document: object, name: str = "providers.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_a_well_formed_provider_table_loads(tmp_path: Path) -> None:
    config = _load_yaml_model(ProvidersConfig, _write(tmp_path, _providers_document()))

    candidate = config.slots["general"].candidates[0]
    assert candidate.provider == "groq"
    assert candidate.capabilities == ("text",)
    # Defaults that are not in the document above.
    assert candidate.supports_streaming is True
    assert candidate.max_file_bytes is None
    assert candidate.reserved_fraction == 0.0


def test_a_provider_that_declares_no_options_gets_an_empty_mapping(tmp_path: Path) -> None:
    """Not ``None``. Every adapter is handed this at construction, and an
    adapter with nothing to configure should not have to check."""
    config = _load_yaml_model(ProvidersConfig, _write(tmp_path, _providers_document()))

    assert config.providers["groq"].options == {}


def test_options_are_carried_through_verbatim(tmp_path: Path) -> None:
    """Header names are case- and hyphen-sensitive on the wire, so this is a
    passthrough rather than anything that normalizes keys."""
    document = _providers_document()
    document["providers"]["groq"]["options"] = {"HTTP-Referer": "https://example.test"}

    config = _load_yaml_model(ProvidersConfig, _write(tmp_path, document))

    assert config.providers["groq"].options == {"HTTP-Referer": "https://example.test"}


def test_a_misspelled_capability_is_a_boot_failure_not_a_silent_downgrade(tmp_path: Path) -> None:
    """``visoin`` in an open set reads as "no vision support" and quietly routes
    every image down the extraction lane — a regression indistinguishable from
    correct behaviour. The closed set turns it into a refusal to start."""
    document = _providers_document()
    document["slots"]["general"]["candidates"][0]["capabilities"] = ["text", "visoin"]

    with pytest.raises(ConfigError) as excinfo:
        _load_yaml_model(ProvidersConfig, _write(tmp_path, document))

    message = str(excinfo.value)
    assert "visoin" in message
    for known in KNOWN_CAPABILITIES:
        assert known in message


def test_a_slot_routing_to_an_unknown_provider_is_rejected_by_name(tmp_path: Path) -> None:
    document = _providers_document()
    document["slots"]["general"]["candidates"][0]["provider"] = "anthropic"

    with pytest.raises(ConfigError) as excinfo:
        _load_yaml_model(ProvidersConfig, _write(tmp_path, document))

    message = str(excinfo.value)
    assert "general" in message
    assert "anthropic" in message


def test_a_slot_with_no_candidates_cannot_exist(tmp_path: Path) -> None:
    """A slot with nowhere to route is a slot that 500s the first time it is asked for."""
    document = _providers_document()
    document["slots"]["general"]["candidates"] = []

    with pytest.raises(ConfigError):
        _load_yaml_model(ProvidersConfig, _write(tmp_path, document))


def test_a_stray_key_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    """``extra="forbid"``. A typo'd field name that loads silently is a setting
    the operator believes is in effect and is not."""
    document = _providers_document()
    document["slots"]["general"]["candidates"][0]["max_ouput_tokens"] = 512

    with pytest.raises(ConfigError) as excinfo:
        _load_yaml_model(ProvidersConfig, _write(tmp_path, document))

    assert "max_ouput_tokens" in str(excinfo.value)


@pytest.mark.parametrize("field", ["context_tokens", "max_output_tokens"])
def test_a_nonsense_window_is_rejected(tmp_path: Path, field: str) -> None:
    document = _providers_document()
    document["slots"]["general"]["candidates"][0][field] = 0

    with pytest.raises(ConfigError):
        _load_yaml_model(ProvidersConfig, _write(tmp_path, document))


def test_enabled_slots_drops_a_slot_with_nowhere_live_to_route(tmp_path: Path) -> None:
    document = _providers_document()
    document["slots"]["vision"] = {
        "description": "Disabled for now.",
        "candidates": [
            {
                "provider": "gemini",
                "model": "gemini-flash",
                "context_tokens": 1048576,
                "max_output_tokens": 8192,
                "capabilities": ["text", "vision", "pdf"],
            }
        ],
    }
    config = _load_yaml_model(ProvidersConfig, _write(tmp_path, document))

    assert set(config.slots) == {"general", "vision"}
    assert set(config.enabled_slots()) == {"general"}


def test_a_slot_survives_if_any_one_candidate_is_live(tmp_path: Path) -> None:
    """The disabled candidate stays in the list — it is a failover target the
    moment its provider is switched on, and dropping it here would hide that."""
    document = _providers_document()
    document["slots"]["general"]["candidates"].append(
        {
            "provider": "gemini",
            "model": "gemini-flash",
            "context_tokens": 1048576,
            "max_output_tokens": 8192,
            "capabilities": ["text"],
        }
    )
    config = _load_yaml_model(ProvidersConfig, _write(tmp_path, document))

    assert set(config.enabled_slots()) == {"general"}
    assert len(config.enabled_slots()["general"].candidates) == 2


# --------------------------------------------------------------------------- #
# config/limits.yaml
# --------------------------------------------------------------------------- #
LIMITS_DOCUMENT: dict[str, Any] = {
    "version": 1,
    "limits": {
        "groq": {
            "llama-3.3-70b-versatile": {
                "rpm": 30,
                "rpd": 1000,
                "tpm": 12000,
                "tpd": 100000,
                "reset": {
                    "rpm": "rolling_60s",
                    "rpd": "fixed_daily_utc",
                    "tpm": "rolling_60s",
                    "tpd": "fixed_daily_utc",
                },
            }
        },
        "gemini": {},
    },
}


def test_limits_carry_their_reset_semantics(tmp_path: Path) -> None:
    """The reset kind is the reason this file exists at all — a fixed daily reset
    and a rolling 24h window are not the same thing (§4.3)."""
    config = _load_yaml_model(LimitsConfig, _write(tmp_path, LIMITS_DOCUMENT, "limits.yaml"))

    limits = config.for_model("groq", "llama-3.3-70b-versatile")
    assert limits is not None
    assert limits.rpm == 30
    assert limits.reset.rpm == "rolling_60s"
    assert limits.reset.rpd == "fixed_daily_utc"


@pytest.mark.parametrize(
    "provider,model",
    [
        ("groq", "llama-3.1-8b-instant"),  # known provider, unlisted model
        ("gemini", "gemini-flash"),  # declared provider, empty table
        ("anthropic", "claude-opus"),  # provider we do not carry at all
    ],
)
def test_an_unlisted_model_has_no_limits_rather_than_wrong_ones(
    tmp_path: Path, provider: str, model: str
) -> None:
    config = _load_yaml_model(LimitsConfig, _write(tmp_path, LIMITS_DOCUMENT, "limits.yaml"))

    assert config.for_model(provider, model) is None


def test_an_unknown_reset_kind_is_rejected(tmp_path: Path) -> None:
    document = {
        "version": 1,
        "limits": {"groq": {"m": {"rpm": 30, "reset": {"rpm": "every_other_tuesday"}}}},
    }

    with pytest.raises(ConfigError):
        _load_yaml_model(LimitsConfig, _write(tmp_path, document, "limits.yaml"))


# --------------------------------------------------------------------------- #
# YAML loading itself
# --------------------------------------------------------------------------- #
def test_an_unreadable_file_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "not-here.yaml"

    with pytest.raises(ConfigError) as excinfo:
        _load_yaml_model(ProvidersConfig, missing)

    assert str(missing) in str(excinfo.value)


def test_unparseable_yaml_is_a_config_error_not_a_yaml_error(tmp_path: Path) -> None:
    """Every failure in this module leaves as one exception type. A caller that
    has to catch ``YAMLError`` too will eventually forget to."""
    path = tmp_path / "providers.yaml"
    path.write_text("providers: [unclosed\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        _load_yaml_model(ProvidersConfig, path)

    assert "not valid YAML" in str(excinfo.value)


@pytest.mark.parametrize("content", ["- one\n- two\n", "just a string\n", ""])
def test_yaml_that_is_not_a_mapping_is_refused_by_shape(tmp_path: Path, content: str) -> None:
    """An empty file parses to ``None``; a list parses to a list. Both would be an
    ``AttributeError`` deep inside pydantic without this check."""
    path = tmp_path / "providers.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        _load_yaml_model(ProvidersConfig, path)

    assert "top-level mapping" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# The checked-in configuration
# --------------------------------------------------------------------------- #
def test_the_committed_configuration_actually_loads() -> None:
    """The first of the tests here that read ``config/*.yaml``.

    Every validator above is only worth having if the files in the repo satisfy
    them, and this is the check that would have caught a bad edit to either file
    before the deploy did.
    """
    validate_startup_config()


def test_every_openrouter_model_keeps_its_free_suffix() -> None:
    """Phase 2 trap 9. The ``:free`` suffix is part of the model *name*, not a
    flag — dropping it does not fail, it silently routes to the paid variant of
    the same model and bills a card. Asserted here rather than trusted to the
    YAML, because the failure is invisible until an invoice arrives."""
    config = get_providers_config()

    routed = [
        candidate.model
        for slot in config.slots.values()
        for candidate in slot.candidates
        if candidate.provider == "openrouter"
    ]

    assert routed, "expected the committed table to route to openrouter somewhere"
    for model in routed:
        assert model.endswith(":free"), model

    for provider, models in get_limits_config().limits.items():
        if provider == "openrouter":
            for model in models:
                assert model.endswith(":free"), model


def test_the_committed_table_declares_three_providers_with_one_enabled() -> None:
    """The shape Phase 2 Step 1 leaves behind, and the thing Step 6 changes.

    Gemini and OpenRouter are fully declared — models, capabilities, limits,
    options — and switched off, because ``registry._traits`` refuses to build a
    registry for an enabled provider it has no adapter for. Step 6 writes those
    adapters and flips these two booleans; if this test fails with them already
    true, check that ``_PROVIDER_TRAITS`` grew the matching entries.
    """
    config = get_providers_config()

    assert set(config.providers) == {"groq", "gemini", "openrouter"}
    assert config.providers["groq"].enabled is True
    assert config.providers["gemini"].enabled is False
    assert config.providers["openrouter"].enabled is False

    # Declared but unroutable: the slot table still resolves to Groq alone.
    assert set(config.enabled_slots()) == {"general", "fast"}


def test_openrouter_carries_its_attribution_headers() -> None:
    """OpenRouter reads these to attribute traffic to an app. They are not
    secrets and they change per deployment, which is exactly why they are config
    rather than constants inside the adapter."""
    options = get_providers_config().providers["openrouter"].options

    assert set(options) == {"HTTP-Referer", "X-Title"}
    assert options["HTTP-Referer"].startswith("https://")


def test_every_routable_candidate_has_a_limits_entry() -> None:
    """Phase 3's tracker looks these up by ``(provider, model)``. A candidate
    with no entry is not "unlimited" — it is a model the tracker cannot budget
    for, and the mismatch is a typo in one of two files that nothing else would
    catch until a quota check silently did nothing."""
    providers = get_providers_config()
    limits = get_limits_config()

    for slot in providers.slots.values():
        for candidate in slot.candidates:
            assert limits.for_model(candidate.provider, candidate.model) is not None, (
                f"{candidate.provider}/{candidate.model} is routable but has no limits entry"
            )


def test_geminis_daily_window_resets_on_pacific_time() -> None:
    """The reason ``fixed_daily_pt`` exists at all. Google's RPD resets at
    midnight Pacific; modelling it as a rolling 24h window makes the ``resets_at``
    that Phase 3's ``/v1/models`` reports wrong by up to eight hours."""
    limits = get_limits_config().for_model("gemini", "gemini-3.6-flash")

    assert limits is not None
    assert limits.reset.rpd == "fixed_daily_pt"
