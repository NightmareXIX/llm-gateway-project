"""Wire models for ``GET /v1/models`` (D21).

**OpenAI's envelope, plus everything the gateway knows.** ``schemas/chat.py``
made this trade in Phase 1 for the same reason: an SDK pointed at this gateway
calls ``client.models.list()`` and expects ``{"object": "list", "data": [...]}``
with OpenAI's per-entry fields present, and ignores whatever else rides along.
Our frontend reads the rest.

**Every timestamp is absolute.** ``resets_at`` is an ISO-8601 UTC instant, never
a duration — a client that wants "in 4 minutes" can compute it from the instant
plus its own clock, and an absolute instant does not rot sitting in a cached
response the way a duration would.

Request/response models only; nothing here imports from the rest of the app.
The computation that fills these in lives in ``app/api/v1/models.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

SlotStatus = Literal["available", "rate_limited", "unavailable", "unknown"]
"""A slot's or a candidate's live status.

``available`` — servable right now. ``rate_limited`` — a declared window is
exhausted; ``resets_at`` says when it is not. ``unavailable`` — the circuit
breaker is open or half-open for this candidate. ``unknown`` — Redis did not
answer, or (for a candidate) the model declares no quota windows to report on;
either way there is nothing to confidently call ``available``.
"""

BreakerStateOut = Literal["closed", "open", "half_open"]


class WindowStatus(BaseModel):
    """One declared quota window's live budget, for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window: Literal["rpm", "rpd", "tpm", "tpd"]
    limit: int
    remaining: int
    resets_at: datetime


class CandidateStatus(BaseModel):
    """One ``(provider, model)`` pair's live status, inside a slot's entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    status: SlotStatus
    breaker_state: BreakerStateOut
    resets_at: datetime | None = None
    """When this candidate is expected to become available again. ``None`` when
    ``status`` is ``available`` or ``unknown`` — there is nothing to wait out."""

    windows: tuple[WindowStatus, ...] = ()
    """Every window the model declares, regardless of whether any is exhausted.
    Empty when quota enforcement is off or the model declares none."""


class ModelEntry(BaseModel):
    """One logical slot — ``auto`` or a named slot from ``providers.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    object: Literal["model"] = "model"
    created: int
    """A fixed placeholder, not a real creation time. This gateway has no notion
    of when a *logical slot* was created — its candidates change with a config
    edit — and OpenAI's SDKs do not validate the field, only expect it to be
    present. Inventing a fresh value per boot would suggest a meaning it does
    not have."""

    owned_by: str | None
    """The primary candidate's provider. ``None`` for ``auto``, which has no
    single owner by construction."""

    status: SlotStatus
    resets_at: datetime | None = None
    description: str
    candidates: tuple[CandidateStatus, ...]


class ModelsResponse(BaseModel):
    """The full response body: OpenAI's list envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal["list"] = "list"
    data: tuple[ModelEntry, ...]
