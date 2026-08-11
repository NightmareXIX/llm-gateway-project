"""Contract A's supporting types (§2.1.1).

Pure data. Nothing in this module does I/O, reads a clock, or imports anything
from the rest of the app — which is what lets ``build_payload`` be a genuinely
pure function and therefore golden-file testable.

Every dataclass is frozen. A :class:`ModelSpec` describes a model, a
:class:`Completion` records what happened; neither is a mutable workspace, and
freezing them means a router that hands the same spec to three attempts cannot
have it altered underneath by the second one.

Three types here are *not* in the contract text. ``ProviderAdapter`` references
``ResolvedAttachment``, ``KeyValidation`` and ``QuotaHint`` in its signatures but
never defines them, so they are defined here with the fields their eventual
consumers need — the perception lane (Phase 4), the BYOK validation flow
(Phase 6), and the quota tracker (Phase 3) respectively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

FinishReason = Literal["stop", "length", "content_filter", "error"]

AttachmentMode = Literal["native", "injected"]
"""How a resolved attachment reaches the model.

``native`` — the bytes ride in the payload, because the answering model reads
this MIME type itself. ``injected`` — the perception lane's extracted text is
spliced into the prompt, because it does not.
"""

ExtractionConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One routable (provider, model) pair, with everything routing needs to know.

    Built by ``registry.py`` from ``config/providers.yaml``, never by hand outside
    tests. ``slot`` is the client-facing identity and ``provider``/``model`` are
    the wire names — the whole point of the indirection is that the second pair
    can change without any client noticing.
    """

    slot: str
    provider: str
    model: str
    context_window: int
    max_output_tokens: int
    supports_streaming: bool
    supports_vision: bool
    supports_pdf: bool
    supports_system_field: bool
    """Whether the system prompt is a top-level payload field (Gemini's
    ``system_instruction``) or a message inside the array (OpenAI's shape).

    The single most consequential shape difference between providers, and the one
    §4.7 of the overview predicts will push structure into the render pipeline."""

    max_file_bytes: int | None
    priority: int
    """Lower is tried first. Assigned from the candidate's position in the slot's
    list, so it always agrees with the configured failover order."""

    reserved_fraction: float = 0.0
    """D8: the share of this model's quota held for the perception lane."""

    @property
    def key(self) -> tuple[str, str]:
        """``(provider, model)`` — the identity quota and the breaker key on."""
        return (self.provider, self.model)

    def supports_mime(self, mime: str) -> bool:
        """Whether this model can read ``mime`` natively, no extraction needed.

        Render step 1 asks this before deciding between native embedding and the
        perception lane. Phase 1 answers ``False`` for everything, because Groq
        declares neither vision nor PDF support.
        """
        if mime == "application/pdf":
            return self.supports_pdf
        return self.supports_vision and mime.startswith("image/")


@dataclass(frozen=True, slots=True)
class GenParams:
    """Generation knobs, in the gateway's own vocabulary.

    Provider-neutral by construction: each adapter translates these into whatever
    its API calls them, and a parameter no provider agrees on does not belong
    here at all.
    """

    temperature: float = 1.0
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    stream: bool = False


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for one attempt.

    ``estimated`` is load-bearing rather than informational. Free tiers routinely
    omit usage from a response, and a quota tracker that cannot tell a counted
    token from a guessed one will drift until it either over- or under-spends a
    daily budget. Phase 3 reconciles estimates differently from measurements.
    """

    tokens_in: int
    tokens_out: int
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class Completion:
    """A finished non-streaming generation."""

    text: str
    usage: Usage
    finish_reason: FinishReason
    raw_id: str | None
    """The provider's own response id. Useless to us, invaluable in a support
    ticket — it is the only identifier their side can look up."""


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One incremental delta (Phase 2).

    ``usage`` is optional because providers disagree about when they report it:
    some send it on every chunk, some only on the last, some never.
    """

    delta: str
    finish_reason: str | None = None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAttachment:
    """A ``file_ref`` block after render step 1 has decided what to do with it.

    The bridge between Contract B's storage shape — which holds only a hash,
    never bytes and never text (invariant 6) — and what a payload actually needs.
    Resolution happens per request against the answering model's capabilities, so
    the same stored ``file_ref`` is native for Gemini and injected for Groq.

    Phase 1 never builds one of these: render step 1 returns an empty list until
    Phase 4.
    """

    file_hash: str
    filename: str
    mime: str
    size_bytes: int
    """The file's size. Named ``size_bytes`` rather than ``bytes`` — which is what
    the stored ``file_ref`` block calls it — because a field named ``bytes`` in a
    class that also has a ``bytes``-typed field shadows the builtin inside the
    class body, and the annotation below stops resolving."""

    mode: AttachmentMode
    data: bytes | None = None
    """Raw content, for ``mode="native"``. Never persisted, never logged."""

    text: str | None = None
    """Extracted text, for ``mode="injected"``."""

    confidence: ExtractionConfidence | None = None
    """How much to trust ``text``. ``low`` means local OCR produced it, which is
    what sets ``degraded`` on the response."""


@dataclass(frozen=True, slots=True)
class KeyValidation:
    """The result of a liveness check on a provider credential.

    Note what is absent: there is no "unknown" state. A check that could not
    reach the provider raises :class:`~app.providers.errors.Unavailable` instead
    of returning ``valid=False``, because the BYOK flow (§9.2) must not tell a
    user their key is bad when the truth is that the provider was down.
    """

    valid: bool
    provider: str
    detail: str | None = None
    """A message written for the user, on failure. Never contains the key."""

    models: tuple[str, ...] = ()
    """Models the key can reach, when the check was a model listing. Phase 6
    reads this to surface slots a private key unlocks (§9.7)."""


@dataclass(frozen=True, slots=True)
class QuotaHint:
    """Ground truth scraped from a provider's rate-limit response headers.

    Opportunistic and never required: it exists to correct drift in our own
    counters, not to replace them. Providers that publish nothing simply yield
    ``None``, and Phase 3's tracker carries on with what it counted itself.
    """

    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    requests_reset_s: float | None = None
    tokens_reset_s: float | None = None

    def is_empty(self) -> bool:
        """True when the response carried no usable hint at all."""
        return (
            self.requests_remaining is None
            and self.tokens_remaining is None
            and self.requests_reset_s is None
            and self.tokens_reset_s is None
        )
