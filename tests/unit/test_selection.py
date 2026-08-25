"""Slot selection: the ``auto`` sentinel, the failover chain, and its ordering.

The candidate chain is a pure function of (registry, request, latency snapshot),
which is what lets these be table tests over a hand-built slot table rather than
integration tests over a running app.

What they defend:

**The spill (D10, ADR-011).** Without it ``substituted`` is unreachable — the
field compares the requested slot to the served one, and inside a single slot
those can never differ. A test that only checks "the named slot's candidates come
first" would pass on an implementation that silently deletes the honesty
mechanism.

**De-duplication on the pair, not the provider.** Free-tier limits are per-model,
so two Groq models are two genuinely different candidates. Collapsing on provider
would erase a whole failover chain and the symptom would be "failover stopped
working for the second model in every slot".

**Config order out of a cold process (D11, ADR-014).** The ranking must be
invisible until it has evidence. A cold process that reorders is a gateway whose
first request after every deploy goes somewhere different, which is miserable to
reason about and impossible to reproduce.
"""

from __future__ import annotations

from types import MappingProxyType

import httpx
import pytest

from app.providers.registry import ProviderRegistry, UnknownSlot, build_registry
from app.providers.types import ModelSpec
from app.routing import selection
from app.usage.metrics import COMPLETE, MIN_SAMPLES, LatencySample, LatencySnapshot


@pytest.fixture
def registry() -> ProviderRegistry:
    """The real slot table over a client that can never reach the network."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    return build_registry(client=client)


# --------------------------------------------------------------------------- #
# A hand-built table, so ordering tests do not move when providers.yaml does
# --------------------------------------------------------------------------- #
def spec(slot: str, provider: str, model: str, priority: int) -> ModelSpec:
    return ModelSpec(
        slot=slot,
        provider=provider,
        model=model,
        context_window=131072,
        max_output_tokens=8192,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=priority,
    )


def fleet(**slots: tuple[str, ...]) -> ProviderRegistry:
    """A registry from ``slot=("provider/model", ...)`` pairs, in declaration order.

    No adapters and no keys: selection never resolves either, and leaving them
    empty is what proves it.
    """

    def parse(slot: str, priority: int, entry: str) -> ModelSpec:
        provider, model = entry.split("/", 1)
        return spec(slot, provider, model, priority)

    specs = {
        slot: tuple(parse(slot, priority, entry) for priority, entry in enumerate(entries))
        for slot, entries in slots.items()
    }
    return ProviderRegistry(specs=specs, adapters={}, keys={})


def names(chain: tuple[ModelSpec, ...]) -> list[str]:
    return [f"{candidate.provider}/{candidate.model}" for candidate in chain]


def measured(*, samples: int = MIN_SAMPLES, **by_entry: float) -> LatencySnapshot:
    """A snapshot from ``groq_fast=120.0`` style kwargs (``_`` splits the pair)."""
    entries = {
        tuple(entry.split("_", 1)): LatencySample(ewma_ms=ms, samples=samples)
        for entry, ms in by_entry.items()
    }
    return LatencySnapshot(
        mode=COMPLETE,
        entries=MappingProxyType(entries),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# resolve_slot — Phase 1's single answer, unchanged
# --------------------------------------------------------------------------- #
def test_auto_resolves_to_the_first_routable_slot(registry: ProviderRegistry) -> None:
    """Config order is the policy, so the answer must track providers.yaml."""
    resolved = selection.resolve_slot(registry, selection.AUTO)

    assert resolved.slot == registry.slots()[0]
    assert resolved.priority == 0


def test_a_named_slot_resolves_to_its_highest_priority_candidate(
    registry: ProviderRegistry,
) -> None:
    resolved = selection.resolve_slot(registry, "fast")

    assert resolved.slot == "fast"
    assert resolved is registry.primary("fast")


def test_an_unknown_slot_raises(registry: ProviderRegistry) -> None:
    """A ``LookupError``, not an ``AppError``: whether it is a 400 or a 500 depends
    on where the name came from, and only the caller knows that."""
    with pytest.raises(UnknownSlot):
        selection.resolve_slot(registry, "llm9")


# --------------------------------------------------------------------------- #
# auto expansion
# --------------------------------------------------------------------------- #
def test_auto_flattens_every_slot_in_config_order() -> None:
    table = fleet(general=("groq/big", "gemini/flash"), fast=("groq/small", "openrouter/free"))

    assert names(selection.candidates(table, selection.AUTO)) == [
        "groq/big",
        "gemini/flash",
        "groq/small",
        "openrouter/free",
    ]


def test_a_model_shared_between_slots_appears_once() -> None:
    table = fleet(general=("groq/big", "gemini/flash"), fast=("gemini/flash", "groq/small"))

    assert names(selection.candidates(table, selection.AUTO)) == [
        "groq/big",
        "gemini/flash",
        "groq/small",
    ]


def test_two_models_from_one_provider_are_two_candidates() -> None:
    """De-duplication is on ``(provider, model)``. Collapsing on provider would
    erase a whole failover chain, since free-tier limits are per-model."""
    table = fleet(general=("groq/big", "groq/small"))

    assert names(selection.candidates(table, selection.AUTO)) == ["groq/big", "groq/small"]


def test_the_real_config_produces_a_chain(registry: ProviderRegistry) -> None:
    """A guard on the hand-built table above: the shipped config must also work."""
    chain = selection.candidates(registry, selection.AUTO)

    assert len(chain) == len({candidate.key for candidate in chain})
    assert chain[0] == registry.primary(registry.slots()[0])


def test_auto_never_includes_a_private_key_only_slots_candidates() -> None:
    """D41: ``auto``'s promise is "the gateway picks" — silently routing to a
    model only one caller can reach makes their ``auto`` unreproducible and
    their cache entries unshareable. ``_fleet`` skips the slot for every
    caller, key holder included — but requesting it *by name* still leads
    with its own candidate and spills into the rest of the fleet exactly as
    D10 always has (the fleet it spills into simply no longer contains a
    second copy of itself, since ``_fleet`` never carries it at all).
    """
    specs: dict[str, tuple[ModelSpec, ...]] = {
        "general": (spec("general", "groq", "big", 0),),
        "pro": (spec("pro", "gemini", "flash-pro", 0),),
    }
    table = ProviderRegistry(
        specs=specs, adapters={}, keys={}, private_key_only_slots=frozenset({"pro"})
    )

    assert names(selection.candidates(table, selection.AUTO)) == ["groq/big"]
    assert names(selection.candidates(table, "pro")) == ["gemini/flash-pro", "groq/big"]


# --------------------------------------------------------------------------- #
# Named slots, and the spill (D10)
# --------------------------------------------------------------------------- #
def test_a_named_slot_leads_with_its_own_candidates() -> None:
    table = fleet(general=("groq/big", "gemini/flash"), fast=("groq/small", "openrouter/free"))

    assert names(selection.candidates(table, "fast"))[:2] == ["groq/small", "openrouter/free"]


def test_a_named_slot_spills_into_the_rest_of_the_fleet() -> None:
    """D2 says an exhausted slot fails over silently and then discloses. Without
    the spill there is nothing to disclose: ``substituted`` compares the requested
    slot to the served one, and inside one slot those can never differ."""
    table = fleet(general=("groq/big", "gemini/flash"), fast=("groq/small",))

    assert names(selection.candidates(table, "fast")) == [
        "groq/small",
        "groq/big",
        "gemini/flash",
    ]


def test_the_spill_does_not_repeat_the_slots_own_candidates() -> None:
    table = fleet(general=("groq/big", "groq/small"), fast=("groq/small",))
    chain = selection.candidates(table, "fast")

    assert names(chain) == ["groq/small", "groq/big"]
    assert len(chain) == len({candidate.key for candidate in chain})


def test_an_unknown_named_slot_raises() -> None:
    with pytest.raises(UnknownSlot):
        selection.candidates(fleet(general=("groq/big",)), "llm9")


# --------------------------------------------------------------------------- #
# Pinning (D3)
# --------------------------------------------------------------------------- #
def test_a_pin_collapses_the_chain_to_one_candidate() -> None:
    """A conversation that has made a tool call has exactly one model whose
    history is intelligible. A chain of alternatives is the wrong answer."""
    table = fleet(general=("groq/big", "gemini/flash"), fast=("groq/small",))

    chain = selection.candidates(table, selection.AUTO, pinned="gemini/flash")

    assert names(chain) == ["gemini/flash"]


def test_a_pin_outranks_the_requested_slot() -> None:
    table = fleet(general=("groq/big",), fast=("groq/small",))

    assert names(selection.candidates(table, "fast", pinned="groq/big")) == ["groq/big"]


def test_a_pin_naming_a_model_that_left_the_config_raises() -> None:
    """Falling back to the fleet would serve a pinned conversation on a model that
    cannot read its own tool-call history. D3 scopes that out rather than papering
    over it."""
    with pytest.raises(UnknownSlot, match="pinned"):
        selection.candidates(fleet(general=("groq/big",)), selection.AUTO, pinned="groq/gone")


def test_a_pin_is_a_provider_slash_model_not_a_slot_name() -> None:
    """A slot's primary candidate moves with a config edit; a pin must not."""
    with pytest.raises(UnknownSlot):
        selection.candidates(fleet(general=("groq/big",)), selection.AUTO, pinned="general")


# --------------------------------------------------------------------------- #
# Latency ranking (D11)
# --------------------------------------------------------------------------- #
def test_a_cold_snapshot_reproduces_config_order_exactly() -> None:
    table = fleet(general=("groq/big", "gemini/flash"), fast=("openrouter/free",))
    cold = LatencySnapshot(mode=COMPLETE, entries=MappingProxyType({}))

    assert names(selection.candidates(table, selection.AUTO, latency=cold)) == names(
        selection.candidates(table, selection.AUTO)
    )


def test_no_snapshot_reproduces_config_order_exactly() -> None:
    """The `ROUTING_LATENCY_RANKING` kill switch has to be a genuine no-op."""
    table = fleet(general=("groq/big", "gemini/flash"))

    assert names(selection.candidates(table, selection.AUTO, latency=None)) == [
        "groq/big",
        "gemini/flash",
    ]


def test_the_faster_candidate_is_promoted() -> None:
    table = fleet(general=("groq/big", "gemini/flash"))
    snapshot = measured(groq_big=900.0, gemini_flash=120.0)

    assert names(selection.candidates(table, selection.AUTO, latency=snapshot)) == [
        "gemini/flash",
        "groq/big",
    ]


def test_a_candidate_under_the_sample_threshold_keeps_its_position() -> None:
    """Ranking only where there is evidence. A candidate with two samples is not
    "slow", it is unmeasured, and sorting it anywhere is a guess."""
    table = fleet(general=("groq/big", "gemini/flash"))
    snapshot = measured(samples=MIN_SAMPLES - 1, groq_big=900.0, gemini_flash=120.0)

    assert names(selection.candidates(table, selection.AUTO, latency=snapshot)) == [
        "groq/big",
        "gemini/flash",
    ]


def test_unmeasured_candidates_hold_their_positions_while_ranked_ones_swap() -> None:
    """The precise rule: ranked candidates trade places with *each other*, into the
    positions they already occupied. An unmeasured candidate is not sorted to
    either end of the list."""
    table = fleet(general=("groq/slow", "openrouter/unknown", "gemini/fast"))
    snapshot = measured(groq_slow=900.0, gemini_fast=120.0)

    assert names(selection.candidates(table, selection.AUTO, latency=snapshot)) == [
        "gemini/fast",
        "openrouter/unknown",
        "groq/slow",
    ]


def test_one_measured_candidate_cannot_reorder_anything() -> None:
    table = fleet(general=("groq/big", "gemini/flash"))
    snapshot = measured(gemini_flash=1.0)

    assert names(selection.candidates(table, selection.AUTO, latency=snapshot)) == [
        "groq/big",
        "gemini/flash",
    ]


def test_equal_latencies_keep_config_order() -> None:
    """A stable sort, so a tie does not shuffle the chain from request to request."""
    table = fleet(general=("groq/big", "gemini/flash"))
    snapshot = measured(groq_big=200.0, gemini_flash=200.0)

    assert names(selection.candidates(table, selection.AUTO, latency=snapshot)) == [
        "groq/big",
        "gemini/flash",
    ]


def test_ranking_applies_to_a_named_slots_spilled_chain_too() -> None:
    table = fleet(general=("groq/big", "gemini/flash"), fast=("openrouter/free",))
    snapshot = measured(groq_big=900.0, gemini_flash=120.0)

    assert names(selection.candidates(table, "fast", latency=snapshot)) == [
        "openrouter/free",
        "gemini/flash",
        "groq/big",
    ]


def test_ranking_never_promotes_a_candidate_past_a_pin() -> None:
    table = fleet(general=("groq/big", "gemini/flash"))
    snapshot = measured(groq_big=900.0, gemini_flash=1.0)

    chain = selection.candidates(table, selection.AUTO, pinned="groq/big", latency=snapshot)

    assert names(chain) == ["groq/big"]
