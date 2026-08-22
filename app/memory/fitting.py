"""D4 — making a history fit a context window (§2.2.5 step 4).

The one piece of the render pipeline with a real algorithm in it. Everything else
in :mod:`app.memory.render` is orchestration; this decides what gets thrown away
when a conversation outgrows the model that has to read it.

**Nothing here is provider-aware, and nothing here renders.** ``fit`` is handed a
``project`` callable — "what will this message look like once the adapter is done
with it" — and an ``estimator``. That inversion is deliberate: the adapter stays
the only thing that shapes a payload (§2.1.3), so fitting measures the adapter's
output rather than producing a second, competing rendering of the same history.
It also means Phase 3 can swap ``chars/4`` for ``tiktoken`` by passing a different
callable, with no edit to the algorithm below.

**Nothing here mutates.** Messages are frozen dataclasses and a shorter history is
a new list; a truncated document is a new :class:`ResolvedAttachment` built with
``replace``. The caller's history is the same object it was before, which matters
because Phase 2's router hands one history to up to three attempts and the second
attempt must not inherit the first's truncation.

**Documents are truncated, never stored truncated.** Invariant 6 says a
``file_ref`` block holds only a hash; the text lives in ``file_extractions`` and is
resolved per request. So shortening a document here is a rendering decision that
expires with the request, and improving the extractor later still improves old
conversations.

The order of operations is §2.2.5's, not one chosen for convenience: pin, drop
whole turns oldest-first, mark the gap, and only then start cutting into document
text. Dropping a turn loses something the user said; truncating a document loses
part of something they attached. The spec puts messages first, so this does too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from app.memory.canonical import (
    CanonicalMessage,
    ContentBlock,
    omission_marker,
    with_content,
)
from app.providers.errors import ContextTooLong
from app.providers.types import GenParams, ModelSpec, ResolvedAttachment

SAFETY_MARGIN = 0.05
"""§2.2.5 step 3. Head-room for the gap between our estimate and the provider's
tokenizer — the two never agree exactly, and being wrong in the cheap direction
costs a few thousand tokens of history while being wrong in the other costs a
round trip and a ``ContextTooLong``."""

CHARS_PER_TOKEN = 4
"""The same ratio ``groq.py`` estimates with. Kept as a separate constant rather
than imported from the adapter: this one measures canonical text and that one
measures a built payload, and a future tokenizer swap will not necessarily happen
to both at once."""

PER_MESSAGE_TOKEN_OVERHEAD = 4
"""Role name plus the delimiters every chat format wraps a message in."""

MIN_DOCUMENT_CHARS = 200
"""How short a document may be cut before truncating it further stops being
worth doing. A 40-character fragment of a PDF is not a degraded answer, it is a
misleading one — past this point the next document is a better thing to cut."""

TRUNCATION_NOTICE = "\n[… document truncated to fit the context window]"

FitStrategy = Literal["truncate", "summarize"]
"""§2.2.7's strategy switch, present from v1 so summarization drops in without
restructuring the call. ``truncate`` is D4 and is all v1 implements."""

TokenEstimator = Callable[[str], int]

Projector = Callable[[CanonicalMessage, Mapping[str, ResolvedAttachment]], str]
"""How a message will look to the model. Supplied by :mod:`app.memory.render`."""


@dataclass(frozen=True, slots=True)
class FitResult:
    """What survived, and what it cost to get there."""

    messages: list[CanonicalMessage]
    attachments: list[ResolvedAttachment]
    messages_dropped: int
    documents_truncated: int
    input_tokens: int
    """The estimate the fitting decisions were made against. Not the number the
    provider will report, and not the number :class:`~app.memory.render.RenderReport`
    carries — that one comes from the adapter, measured on the real payload."""


def estimate_text_tokens(text: str) -> int:
    """Characters over four. Cheap on purpose — see :data:`CHARS_PER_TOKEN`."""
    return len(text) // CHARS_PER_TOKEN


def input_budget(spec: ModelSpec, params: GenParams) -> int:
    """§2.2.5 step 3: ``context_window - max_output_tokens - safety_margin``.

    The output figure is the *effective* cap — what ``build_payload`` will clamp
    ``max_tokens`` to — rather than the model's ceiling. Reserving 32k of output
    room for a request that asked for 512 tokens would throw away history to make
    space nothing was ever going to use.

    May return a non-positive number if a model is configured with an output
    ceiling that swallows its own context window. The caller raises on that; it is
    a configuration bug, not a request the fitting step can rescue.
    """
    reserved_out = (
        min(params.max_tokens, spec.max_output_tokens)
        if params.max_tokens is not None
        else spec.max_output_tokens
    )
    return spec.context_window - reserved_out - int(spec.context_window * SAFETY_MARGIN)


def fit(
    messages: list[CanonicalMessage],
    attachments: list[ResolvedAttachment],
    *,
    budget: int,
    spec: ModelSpec,
    project: Projector,
    strategy: FitStrategy = "truncate",
    estimator: TokenEstimator = estimate_text_tokens,
) -> FitResult:
    """Make ``messages`` fit ``budget``, or explain why they cannot.

    Raises :class:`~app.providers.errors.ContextTooLong` when there is nothing
    left to give — which is the right error even though nothing was sent yet: it
    is the same condition the provider would report, it carries ``limit_tokens``,
    and ``to_app_error`` already turns it into a 400 the user can act on.
    """
    if strategy == "summarize":
        raise NotImplementedError(
            "the SUMMARIZE fitting strategy is the §2.2.7 seam; see app/memory/summarize.py. "
            "v1 fits by truncation (D4)."
        )

    by_hash = {attachment.file_hash: attachment for attachment in attachments}
    costs = [_cost(message, by_hash, project, estimator) for message in messages]
    total = sum(costs)

    if total <= budget:
        return FitResult(
            messages=list(messages),
            attachments=list(attachments),
            messages_dropped=0,
            documents_truncated=0,
            input_tokens=total,
        )

    survivors, costs, dropped = _drop_oldest_turns(
        messages, costs, by_hash, project, estimator, budget=budget
    )
    total = sum(costs)

    if total <= budget:
        return FitResult(
            messages=survivors,
            attachments=list(attachments),
            messages_dropped=dropped,
            documents_truncated=0,
            input_tokens=total,
        )

    survivors, attachments, truncated, total = _truncate_documents(
        survivors, attachments, project, estimator, budget=budget, total=total
    )

    if total > budget:
        raise ContextTooLong(
            f"this conversation needs about {total} input tokens and the model allows {budget}",
            provider=spec.provider,
            model=spec.model,
            limit_tokens=budget,
        )

    return FitResult(
        messages=survivors,
        attachments=attachments,
        messages_dropped=dropped,
        documents_truncated=truncated,
        input_tokens=total,
    )


# --------------------------------------------------------------------------- #
# Dropping turns
# --------------------------------------------------------------------------- #
def _drop_oldest_turns(
    messages: list[CanonicalMessage],
    costs: list[int],
    by_hash: Mapping[str, ResolvedAttachment],
    project: Projector,
    estimator: TokenEstimator,
    *,
    budget: int,
) -> tuple[list[CanonicalMessage], list[int], int]:
    """Drop whole turns from the front until it fits, and mark the gap.

    Two things are never dropped: the system message, because it is the only
    instruction the model has, and the last user message, because it is the
    question. Everything between them goes oldest-first in user/assistant pairs —
    pair-wise so the model is never shown an answer to a question that is no
    longer in the history, which reads as a non-sequitur rather than as missing
    context.
    """
    pinned = _pinned_indices(messages)
    groups = _droppable_groups(messages, pinned)

    alive = [True] * len(messages)
    dropped = 0

    for group in groups:
        for index in group:
            alive[index] = False
        dropped += len(group)

        # The marker lands *inside* a surviving message rather than beside it, so
        # its cost is whatever that one message's new text comes to — recomputed
        # here, and nearly free when it merges into a marker already present.
        survivors, survivor_costs = _apply_marker(
            messages, alive, costs, by_hash, project, estimator, dropped=dropped
        )
        if sum(survivor_costs) <= budget:
            return survivors, survivor_costs, dropped

    survivors, survivor_costs = _apply_marker(
        messages, alive, costs, by_hash, project, estimator, dropped=dropped
    )
    return survivors, survivor_costs, dropped


def _pinned_indices(messages: list[CanonicalMessage]) -> set[int]:
    """The system message and the final question — never dropped."""
    pinned: set[int] = set()

    if messages and messages[0].role == "system":
        pinned.add(0)

    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            pinned.add(index)
            break
    else:
        # No user message at all. Unreachable from the chat endpoint, but a
        # history that is all system and assistant should still keep its tail
        # rather than being emptied.
        if messages:
            pinned.add(len(messages) - 1)

    return pinned


def _droppable_groups(messages: list[CanonicalMessage], pinned: set[int]) -> list[list[int]]:
    """Droppable indices, bundled into whole turns, oldest group first."""
    droppable = [index for index in range(len(messages)) if index not in pinned]
    groups: list[list[int]] = []

    position = 0
    while position < len(droppable):
        index = droppable[position]
        group = [index]
        position += 1

        # A user turn takes its answer with it, when the answer is the very next
        # message and is also droppable.
        if (
            messages[index].role == "user"
            and position < len(droppable)
            and droppable[position] == index + 1
            and messages[index + 1].role == "assistant"
        ):
            group.append(droppable[position])
            position += 1

        groups.append(group)

    return groups


def _apply_marker(
    messages: list[CanonicalMessage],
    alive: list[bool],
    costs: list[int],
    by_hash: Mapping[str, ResolvedAttachment],
    project: Projector,
    estimator: TokenEstimator,
    *,
    dropped: int,
) -> tuple[list[CanonicalMessage], list[int]]:
    """Rebuild the surviving history with one omission marker in it.

    *One* marker, always. If the earliest survivor already carries one — a history
    that was truncated before and is being rendered again — the counts are merged
    rather than a second marker appended. Two markers in one message describe the
    same gap twice and tell the model nothing the sum does not.

    Markers on messages that are themselves dropped are **carried forward** into
    the surviving one. Without that, truncating an already-truncated conversation
    quietly shrinks the reported gap: the marker saying four turns were missing
    goes away with the message that held it, and the model is told the history is
    more complete than it is.
    """
    survivors = [message for index, message in enumerate(messages) if alive[index]]
    survivor_costs = [cost for index, cost in enumerate(costs) if alive[index]]

    if not dropped or not survivors:
        return survivors, survivor_costs

    carried = sum(
        block["omitted_count"]
        for index, message in enumerate(messages)
        if not alive[index]
        for block in message.content
        if block["type"] == "omission_marker"
    )

    target = 1 if survivors[0].role == "system" and len(survivors) > 1 else 0
    marked = _with_omission_marker(survivors[target], dropped + carried)

    survivors[target] = marked
    survivor_costs[target] = _cost(marked, by_hash, project, estimator)
    return survivors, survivor_costs


def _with_omission_marker(message: CanonicalMessage, dropped: int) -> CanonicalMessage:
    """A copy of ``message`` whose content records ``dropped`` missing messages."""
    content: list[ContentBlock] = list(message.content)

    for index, block in enumerate(content):
        if block["type"] == "omission_marker":
            content[index] = omission_marker(block["omitted_count"] + dropped)
            return with_content(message, content)

    return with_content(message, [omission_marker(dropped), *content])


# --------------------------------------------------------------------------- #
# Truncating documents
# --------------------------------------------------------------------------- #
def _truncate_documents(
    messages: list[CanonicalMessage],
    attachments: list[ResolvedAttachment],
    project: Projector,
    estimator: TokenEstimator,
    *,
    budget: int,
    total: int,
) -> tuple[list[CanonicalMessage], list[ResolvedAttachment], int, int]:
    """Cut into injected document text, largest document first.

    Only ``injected`` attachments can be cut — a natively embedded file is bytes
    the provider parses itself, and slicing a PDF in half produces a corrupt file
    rather than a shorter one.

    The head is kept. §2.2.5's Phase 4 note prefers head-plus-summary; a summary
    does not exist until the perception lane does, and the head of a document is
    where its title, abstract and first table live.
    """
    current = list(attachments)
    exhausted: set[int] = set()
    truncated = 0

    while total > budget:
        index = _largest_truncatable(current, exhausted)
        if index is None:
            break

        attachment = current[index]
        text = attachment.text or ""
        excess_chars = (total - budget) * CHARS_PER_TOKEN + len(TRUNCATION_NOTICE)
        keep = max(MIN_DOCUMENT_CHARS, len(text) - excess_chars)
        shortened = text[:keep] + TRUNCATION_NOTICE

        # The notice has a length of its own, so a document already near the floor
        # can "shrink" into a longer string. Refusing a non-shrinking cut is what
        # keeps this loop finite — without it, such a document is picked, rewritten
        # to the same size, and picked again forever.
        if len(shortened) >= len(text):
            exhausted.add(index)
            continue

        current[index] = replace(attachment, text=shortened)
        truncated += 1
        if keep == MIN_DOCUMENT_CHARS:
            # At the floor. Anything still over budget has to come from the next
            # document, not from this one.
            exhausted.add(index)

        by_hash = {item.file_hash: item for item in current}
        total = sum(_cost(message, by_hash, project, estimator) for message in messages)

    return messages, current, truncated, total


def _largest_truncatable(attachments: list[ResolvedAttachment], exhausted: set[int]) -> int | None:
    """The injected document with the most text left to give, if any has any."""
    best: int | None = None
    best_length = MIN_DOCUMENT_CHARS

    for index, attachment in enumerate(attachments):
        if index in exhausted or attachment.mode != "injected" or attachment.text is None:
            continue
        if len(attachment.text) > best_length:
            best, best_length = index, len(attachment.text)

    return best


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
def native_tokens(attachments: Iterable[ResolvedAttachment]) -> int:
    """D27: what the native attachments among these cost, in tokens.

    **Native only, and that is trap 8.** An injected attachment's cost is
    already inside the text ``project`` produced and the estimator has counted
    it; adding ``token_cost`` on top would charge one document twice. Native
    bytes are the opposite case — a character-based estimator cannot see them
    at all (trap 9), so this is the only place their size enters the
    arithmetic.

    Public because :mod:`app.memory.render` needs the same sum for
    ``RenderReport.estimated_tokens``, and two implementations of "which
    attachments cost extra" is one implementation that eventually disagrees.
    """
    return sum(attachment.token_cost for attachment in attachments if attachment.mode == "native")


def _cost(
    message: CanonicalMessage,
    by_hash: Mapping[str, ResolvedAttachment],
    project: Projector,
    estimator: TokenEstimator,
) -> int:
    """One message's share of the budget: its text, plus any bytes riding on it.

    Charged per message rather than per turn so the attachment's cost leaves
    the total when the message carrying it is dropped — a forty-page PDF the
    fitting step just discarded is not still occupying ten thousand tokens of
    a budget it no longer touches.
    """
    attachments = (
        by_hash[block["file_hash"]]
        for block in message.content
        if block["type"] == "file_ref" and block["file_hash"] in by_hash
    )
    return (
        estimator(project(message, by_hash))
        + PER_MESSAGE_TOKEN_OVERHEAD
        + native_tokens(attachments)
    )
