"""Slot selection — the ``auto`` sentinel and what happens to a name nobody knows."""

from __future__ import annotations

import httpx
import pytest

from app.providers.registry import ProviderRegistry, UnknownSlot, build_registry
from app.routing import selection


@pytest.fixture
def registry() -> ProviderRegistry:
    """The real slot table over a client that can never reach the network."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    return build_registry(client=client)


def test_auto_resolves_to_the_first_routable_slot(registry: ProviderRegistry) -> None:
    """Config order is the policy, so the answer must track providers.yaml."""
    spec = selection.resolve_slot(registry, selection.AUTO)

    assert spec.slot == registry.slots()[0]
    assert spec.priority == 0


def test_a_named_slot_resolves_to_its_highest_priority_candidate(
    registry: ProviderRegistry,
) -> None:
    spec = selection.resolve_slot(registry, "fast")

    assert spec.slot == "fast"
    assert spec is registry.primary("fast")


def test_an_unknown_slot_raises(registry: ProviderRegistry) -> None:
    """A ``LookupError``, not an ``AppError``: whether it is a 400 or a 500 depends
    on where the name came from, and only the caller knows that."""
    with pytest.raises(UnknownSlot):
        selection.resolve_slot(registry, "llm9")


def test_the_phase_2_candidate_chain_is_a_declared_seam(registry: ProviderRegistry) -> None:
    """A typed signature raising, not a stub that silently returns one candidate."""
    with pytest.raises(NotImplementedError):
        selection.candidates(registry, selection.AUTO)
