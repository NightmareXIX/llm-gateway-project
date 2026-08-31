"""Wire models for ``/v1/admin/*`` (Phase 7 Step 3, D44).

Self-scoped usage surface: every field here answers a question about the
calling principal's own traffic, or their own resolved quota — never anyone
else's. See ``app/api/admin.py``'s module docstring for what "admin" means in
this codebase.

Request/response models only; nothing here imports from the rest of the app
beyond ``app.db.repo.requests``' aggregate dataclasses, which are its
vocabulary. The quota route reuses ``app.schemas.models.ModelsResponse``
directly rather than a shape defined here — see ``api/admin.py``'s docstring
on why the two pages must not be able to disagree.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

Window = Literal["1h", "24h", "7d"]


class VolumePointOut(BaseModel):
    """One bucket of the request-volume series. Mirrors ``requests.VolumePoint``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket_start: datetime
    total: int
    errors: int
    cache_hits: int


class ProviderSliceOut(BaseModel):
    """One ``(provider, model)`` pair's share of real upstream calls, priced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str | None
    requests: int
    tokens_in: int
    tokens_out: int
    simulated_cost: Decimal | None
    """D46: ``None`` when this (provider, model) has no ``pricing.yaml`` entry —
    never ``Decimal("0")``, which would understate the total in the flattering
    direction (trap 7)."""


class OutcomeSummaryOut(BaseModel):
    """Mirrors ``requests.OutcomeSummary`` field for field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    ok: int
    errors: int
    cache_hits: int
    replays: int
    substituted: int
    multi_attempt: int
    tokens_in: int
    tokens_out: int
    wasted_tokens_out: int


class PoolSplitOut(BaseModel):
    """Shared-pool vs. private-key traffic, with an approximate cost per side.

    D45's ``pool_split`` returns token counts only — it has no per-(provider,
    model) breakdown to run ``simulated_cost`` against directly, unlike
    ``provider_distribution``. The costs here are a *blended* estimate: the
    same window's overall priced rate (this response's ``total_cost`` divided
    by the total priced tokens behind it) applied to each side's own token
    counts. This is a dashboard approximation, not a ledger: it is exact only
    in the degenerate case where every priced request in the window shares one
    per-token price (one model, and either all-input or all-output tokens) —
    a model's input and output tokens are priced differently in general, and
    the blend does not preserve each side's own input/output ratio. ``None``
    on both sides only when nothing in the window was priced at all; a side
    with real priced traffic elsewhere in the window but zero tokens of its
    own costs exactly ``Decimal("0")`` — spent nothing, not unpriced.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    shared_requests: int
    shared_tokens_in: int
    shared_tokens_out: int
    shared_cost: Decimal | None
    private_requests: int
    private_tokens_in: int
    private_tokens_out: int
    private_cost: Decimal | None


class UsageOverview(BaseModel):
    """``GET /v1/admin/usage``'s full body — one window, four aggregates, costed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window: Window
    since: datetime
    """The floored start of the window — the same instant ``volume``'s first
    bucket starts at, so the series and the summary above it never disagree
    about what "this window" means."""
    until: datetime

    outcomes: OutcomeSummaryOut
    volume: tuple[VolumePointOut, ...]
    providers: tuple[ProviderSliceOut, ...]
    pool_split: PoolSplitOut

    total_cost: Decimal | None
    """Sum of every priced ``providers`` slice's ``simulated_cost``. ``None``
    only when nothing in the window was priced at all — a window with at
    least one priced request and some unpriced ones still reports a real,
    partial total (D46) rather than folding the gap into it as zero."""
    currency: str | None
    """The price table's currency, or ``None`` when ``total_cost`` is ``None``
    — there is nothing to denominate."""
    unpriced_requests: int
    """Requests served by a real provider call whose (provider, model) has no
    ``pricing.yaml`` entry — counted here, never silently priced at zero."""


class RequestOut(BaseModel):
    """One ``requests`` row, mapped for the "your last few calls" table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    created_at: datetime
    requested_slot: str | None
    served_slot: str | None
    provider: str | None
    model: str | None
    status: str
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None
    ttft_ms: int | None
    cache_hit: bool
    substituted: bool
    error_code: str | None
    quota_scope: str
    """``"system"`` for the shared pool, or a ``user_id`` for a private-key
    attempt (Phase 6) — always the caller's own id when it is not ``"system"``,
    since this route already scopes to ``principal.user_id``."""


class RequestsOut(BaseModel):
    """``GET /v1/admin/requests``'s full body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data: tuple[RequestOut, ...]
