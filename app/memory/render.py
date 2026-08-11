"""The render pipeline — Contract B §2.2.5.

One function, six ordered steps. **Every provider payload in the system comes out
of here**, which is the property that makes the rest of the design hold: if a
payload could also be built by calling ``adapter.build_payload`` directly, then
attachment resolution, budgeting and D4 truncation are all optional, and the one
that gets skipped will be the one that mattered.

    resolve attachments → materialize → budget → fit → adapt → report

**What this module owns is *how a message looks*; what it does not own is *how a
payload is shaped*.** Step 5 hands off to the adapter and the adapter has the last
word. Steps 2 to 4 need to know the size of what the adapter will produce, so
:func:`materialize` computes a text projection of a message — deliberately a
*measurement*, not a second rendering that could drift from the real one. The one
piece of formatting this module genuinely owns is :func:`document_envelope`,
because that envelope is a prompt-safety decision (§2.2.5 step 2) rather than a
wire-format one, and every adapter must wrap extracted text identically.

**Why it is ``async`` when Phase 1 does no I/O.** Step 1 reads object storage and
the ``file_extractions`` table in Phase 4. Retrofitting ``async`` onto a function
this central means touching every call site, which is the exact churn building the
whole pipeline in Phase 1 exists to avoid.

**One addition to the contract's signature.** §2.2.5 writes ``render(history,
spec, params, adapter)``; this takes a keyword-only ``resolver`` after those four,
defaulted, so the contract's form still works verbatim. The alternative is a
module-level singleton that tests have to monkeypatch, and Phase 4's resolver
needs a database session it cannot get from a global. The same reasoning, and the
same shape, as the ``model`` keyword ``base.py`` adds to ``parse_error``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.logging import get_logger
from app.memory import fitting
from app.memory.canonical import CanonicalMessage, FileRefBlock, validate
from app.providers.base import ProviderAdapter
from app.providers.errors import ContextTooLong
from app.providers.types import GenParams, ModelSpec, ResolvedAttachment

logger = get_logger("app.memory.render")


@dataclass(frozen=True, slots=True)
class RenderReport:
    """What the pipeline did, for the log line and the frontend's notices.

    Two audiences, which is why it is returned rather than only logged.
    ``messages_dropped`` and ``documents_truncated`` are the honest-degradation
    story D4 owes the user — an answer built on two thirds of a conversation
    should say so. ``estimated_tokens`` is what Phase 3's quota tracker reserves
    against.
    """

    messages_dropped: int = 0
    documents_truncated: int = 0
    estimated_tokens: int = 0
    """The **adapter's** estimate, measured on the finished payload.

    Not the number the fitting step made its decisions with. Fitting measures
    canonical text before a payload exists; this measures the payload that will
    actually be sent, so it is the one worth reserving quota against and the one
    worth comparing to reported usage afterwards."""

    attachments_native: int = 0
    attachments_injected: int = 0
    degraded: bool = False
    """Set when an attachment reached the model through local OCR rather than a
    model that can read it (Phase 4). Copied onto ``MessageMeta.degraded`` and
    rendered by the frontend indicator, per §1.1."""

    @property
    def truncated(self) -> bool:
        """Whether anything was lost making this fit."""
        return self.messages_dropped > 0 or self.documents_truncated > 0


class AttachmentResolver(Protocol):
    """Step 1: decide how each referenced file reaches the model.

    Phase 4's implementation runs the perception lane — cache, then native
    passthrough, then Gemini extraction, then local OCR — and the decision depends
    on ``spec``, because the same stored ``file_ref`` is a native PDF for Gemini
    and injected text for Groq.
    """

    async def resolve(
        self, refs: list[FileRefBlock], spec: ModelSpec
    ) -> list[ResolvedAttachment]: ...


class NoAttachments:
    """Phase 1's resolver: there is no perception lane yet.

    Returns nothing for a history that references nothing, which is every history
    Phase 1 can produce — ``POST /v1/files`` does not exist, so no ``file_ref``
    block can be written. A history that somehow *does* carry one raises instead
    of resolving to an empty list: silently dropping the reference would answer a
    question about a document the model never saw, and the user would have no way
    to tell that from a bad answer.
    """

    async def resolve(self, refs: list[FileRefBlock], spec: ModelSpec) -> list[ResolvedAttachment]:
        if refs:
            raise NotImplementedError(
                f"{len(refs)} file reference(s) reached the render pipeline, but the perception "
                f"lane lands in Phase 4; {spec.provider}/{spec.model} cannot be sent this history"
            )
        return []


def document_envelope(attachment: ResolvedAttachment) -> str:
    """Wrap extracted document text in the §2.2.5 step 2 delimiter.

    Delimited rather than concatenated so the model can tell document content from
    user instruction — the cheapest prompt-injection mitigation there is, and the
    difference between summarizing a PDF and obeying one.

    Lives here rather than in an adapter because every provider must wrap
    extracted text the same way. An adapter that invented its own envelope would
    change what the model is being told while the golden files, which only ever
    cover one provider each, all stayed green.
    """
    confidence = f' confidence="{attachment.confidence}"' if attachment.confidence else ""
    text = attachment.text or ""
    return (
        f'<document name="{attachment.filename}" source="extracted"{confidence}>\n'
        f"{text}\n"
        f"</document>"
    )


def materialize(message: CanonicalMessage, attachments: Mapping[str, ResolvedAttachment]) -> str:
    """§2.2.5 step 2: the text a message will contribute to the prompt.

    A *projection for measurement*, not a rendering. The adapter shapes the real
    payload in step 5 and may frame this text differently — Gemini splits it into
    ``parts``, a future provider might not join blocks with a blank line. What
    every adapter agrees on is the content: this text, these documents, this
    marker. Sizing that is all steps 3 and 4 need, and it is the most that can be
    measured without duplicating the adapter.

    A ``file_ref`` with no resolved attachment contributes nothing rather than
    raising. Step 1 is what enforces resolution, and it already ran; the adapters
    raise on the same condition in step 5. Being lenient in the middle keeps the
    error message the caller sees pointing at the resolver rather than at the
    tape measure.
    """
    parts: list[str] = []

    for block in message.content:
        if block["type"] == "text":
            parts.append(block["text"])
        elif block["type"] == "omission_marker":
            parts.append(f"[{block['omitted_count']} earlier messages omitted]")
        elif block["type"] == "file_ref":
            attachment = attachments.get(block["file_hash"])
            if attachment is None:
                continue
            if attachment.mode == "injected":
                parts.append(document_envelope(attachment))
            else:
                # Native bytes are not prompt text, and base64 length is not a
                # token count. Providers bill multimodal input on their own terms;
                # Phase 4 gives `ResolvedAttachment` a token cost to use here.
                parts.append(f"[{attachment.mime} attachment: {attachment.filename}]")

    return "\n\n".join(parts)


async def render(
    history: list[CanonicalMessage],
    spec: ModelSpec,
    params: GenParams,
    adapter: ProviderAdapter,
    *,
    resolver: AttachmentResolver | None = None,
    strategy: fitting.FitStrategy = "truncate",
) -> tuple[dict[str, Any], RenderReport]:
    """Canonical history in, provider payload out.

    Raises :class:`~app.providers.errors.ContextTooLong` when the history cannot
    be made to fit, and :class:`~app.memory.canonical.InvariantViolation` when it
    should never have been stored in the first place.
    """
    # Step 0 — the invariants. Not one of the six, and checked anyway: history
    # arrives from a JSONB column that a migration, a manual fix or a future bulk
    # write could have left in a state no live code path would produce, and the
    # renderer is the last place that can notice before a provider is told about
    # it. `validate` is pure and O(n), which is what makes this affordable per
    # request rather than only in tests.
    validate(history)

    # Step 1 — resolve attachments.
    refs = [block for message in history for block in message.file_refs()]
    attachments = await (resolver or NoAttachments()).resolve(refs, spec)
    native = sum(1 for attachment in attachments if attachment.mode == "native")
    injected = len(attachments) - native
    degraded = any(attachment.confidence == "low" for attachment in attachments)

    # Steps 2 to 4 — materialize, budget, fit. `materialize` is passed in rather than
    # imported by `fitting`, so the algorithm stays ignorant of how a message
    # looks and this module stays the only place that decides.
    budget = fitting.input_budget(spec, params)
    if budget <= 0:
        raise ContextTooLong(
            f"{spec.provider}/{spec.model} is configured with a {spec.max_output_tokens}-token "
            f"output ceiling inside a {spec.context_window}-token window, leaving no room for "
            f"input; fix config/providers.yaml",
            provider=spec.provider,
            model=spec.model,
            limit_tokens=spec.context_window,
        )

    result = fitting.fit(
        history,
        attachments,
        budget=budget,
        spec=spec,
        project=materialize,
        strategy=strategy,
    )

    # Step 5 — adapt.
    payload = adapter.build_payload(result.messages, spec, params, result.attachments)

    # Step 6 — report.
    report = RenderReport(
        messages_dropped=result.messages_dropped,
        documents_truncated=result.documents_truncated,
        estimated_tokens=adapter.estimate_tokens(payload),
        attachments_native=native,
        attachments_injected=injected,
        degraded=degraded,
    )

    if report.truncated:
        # Info, not debug. Truncation silently changes the answer a user gets, so
        # "why did this reply ignore what I said earlier?" has to be answerable
        # from production logs rather than only by reproducing it.
        logger.info(
            "render.truncated",
            provider=spec.provider,
            model=spec.model,
            slot=spec.slot,
            messages_dropped=report.messages_dropped,
            documents_truncated=report.documents_truncated,
            budget_tokens=budget,
            estimated_tokens=report.estimated_tokens,
        )
    else:
        logger.debug(
            "render.completed",
            provider=spec.provider,
            model=spec.model,
            slot=spec.slot,
            messages=len(result.messages),
            estimated_tokens=report.estimated_tokens,
        )

    return payload, report
