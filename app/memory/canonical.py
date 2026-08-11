"""Contract B — the canonical conversation schema (§2.2).

The gateway owns conversational state; providers are stateless. This module
defines the *only* shape that state is ever stored in. A provider's request body
never reaches Postgres, which is what keeps "add a fourth provider" a rendering
concern rather than a migration.

**Blocks are ``TypedDict``s, not dataclasses.** ``messages.content`` is a JSONB
column, so a block that already *is* a dict crosses the storage boundary with no
conversion layer at all — no ``to_jsonb`` on write, no reconstruction on read.
Under mypy the union narrows on the ``type`` literal, so ``block["type"] ==
"text"`` gives the same static safety ``isinstance`` would. What the type system
cannot do is police data arriving *from* the database, which is what
:func:`parse_content` is for.

**Nothing here does I/O, and nothing here reads a clock.** ``validate`` is a pure
function over a list, which is what makes the six invariants cheap enough to
assert on every append rather than only in tests.

Violations raise :class:`InvariantViolation`, deliberately *not* an
``AppError``. A history that breaks an invariant is our bug, not something the
caller can act on, so it must land in the catch-all handler as a generic 500 with
a ``request_id`` — never as a message written for a user.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, TypedDict, TypeGuard, get_args
from uuid import UUID

SCHEMA_VERSION = 1
"""Stamped on every message written. §2.2.4: migrating untagged JSONB is
miserable, and the tag costs two bytes."""

Role = Literal["system", "user", "assistant"]
ROLES: tuple[Role, ...] = get_args(Role)

ExtractionTier = Literal["cache", "native", "llm", "local"]

TruncationReason = Literal["context_truncation"]


# --------------------------------------------------------------------------- #
# Content blocks
# --------------------------------------------------------------------------- #
class TextBlock(TypedDict):
    """Plain text — the only block Phase 1 ever writes."""

    type: Literal["text"]
    text: str


class FileRefBlock(TypedDict):
    """A pointer to an uploaded file. Invariant 6 lives in this shape.

    There is no field for bytes and no field for extracted text, and that is the
    whole point: extraction is resolved at *render* time from ``file_extractions``
    by hash, so improving the extractor retroactively improves every old
    conversation. A block that cached the extraction would freeze it forever.

    ``bytes`` is the file's size, not its content.
    """

    type: Literal["file_ref"]
    file_hash: str
    filename: str
    mime: str
    bytes: int


class OmissionMarkerBlock(TypedDict):
    """The visible scar left by D4 truncation.

    Inserted by the fitting step when older turns are dropped to fit a context
    window, so the model — and anyone reading the transcript later — can tell the
    difference between "the user never said that" and "we could not afford to
    send it".
    """

    type: Literal["omission_marker"]
    omitted_count: int
    reason: TruncationReason


ContentBlock = TextBlock | FileRefBlock | OmissionMarkerBlock

RESERVED_BLOCK_TYPES = frozenset({"tool_call", "tool_result", "summary"})
"""Block types the contract names but v1 does not implement (§2.2.2, §2.2.7).

Reserved rather than merely absent. ``parse_content`` rejects them with a message
saying *which* phase owns them, so the failure mode for a premature implementation
is a loud, self-explaining error rather than a block silently ignored by every
renderer downstream.
"""

_KNOWN_BLOCK_TYPES = frozenset({"text", "file_ref", "omission_marker"})

# Exact key sets, checked in both directions. Missing keys are the obvious bug;
# *extra* keys are the interesting one, because that is how invariant 6 gets
# broken — a `file_ref` that also carries `extracted` or `text` re-introduces
# precisely the frozen-extraction problem the invariant exists to prevent.
_BLOCK_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "text": {"text": str},
    "file_ref": {"file_hash": str, "filename": str, "mime": str, "bytes": int},
    "omission_marker": {"omitted_count": int, "reason": str},
}


def text_block(text: str) -> TextBlock:
    """Build a text block. Call sites use this rather than a dict literal."""
    return {"type": "text", "text": text}


def file_ref_block(*, file_hash: str, filename: str, mime: str, bytes: int) -> FileRefBlock:
    """Build a file reference.

    ``bytes`` shadows the builtin, deliberately: the contract names the field
    ``bytes`` and a constructor whose keyword differs from the stored key is a
    small trap for every future call site.
    """
    return {
        "type": "file_ref",
        "file_hash": file_hash,
        "filename": filename,
        "mime": mime,
        "bytes": bytes,
    }


def omission_marker(omitted_count: int) -> OmissionMarkerBlock:
    """Build the D4 truncation marker."""
    return {
        "type": "omission_marker",
        "omitted_count": omitted_count,
        "reason": "context_truncation",
    }


def is_text(block: ContentBlock) -> TypeGuard[TextBlock]:
    """Narrow to :class:`TextBlock`. Sugar over ``block["type"] == "text"``."""
    return block["type"] == "text"


def is_file_ref(block: ContentBlock) -> TypeGuard[FileRefBlock]:
    """Narrow to :class:`FileRefBlock`."""
    return block["type"] == "file_ref"


# --------------------------------------------------------------------------- #
# Message metadata
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MessageMeta:
    """Provenance for one message — §2.2.2.

    This is what makes the D1/D2 disclosure free at render time: ``served_by``,
    ``substituted`` and ``attempts`` are already persisted, so the frontend's
    model indicator is reading stored fact rather than re-deriving anything.

    Every field except the first four is unused in Phase 1 and populated from
    Phase 2 onward. They are declared now because a stored JSONB shape that grows
    later is a data migration, and this one is cheap to get right up front.
    """

    provider_used: str | None = None
    model_used: str | None = None
    slot_used: str | None = None
    requested_slot: str | None = None
    substituted: bool = False
    attempts: int = 1
    tokens_in: int | None = None
    tokens_out: int | None = None
    wasted_tokens_out: int = 0
    degraded: bool = False
    extraction_tier: ExtractionTier | None = None

    def to_jsonb(self) -> dict[str, Any]:
        """Serialize for the ``messages.meta`` column.

        Written in full rather than sparsely — ``None`` and default values are
        included. A reader should never have to know which app version wrote a
        row to know whether a missing key means "false" or "not recorded yet".
        """
        return {
            "provider_used": self.provider_used,
            "model_used": self.model_used,
            "slot_used": self.slot_used,
            "requested_slot": self.requested_slot,
            "substituted": self.substituted,
            "attempts": self.attempts,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "wasted_tokens_out": self.wasted_tokens_out,
            "degraded": self.degraded,
            "extraction_tier": self.extraction_tier,
        }

    @classmethod
    def from_jsonb(cls, raw: object) -> MessageMeta:
        """Rebuild from a stored value, tolerating absent keys.

        Deliberately lenient in one direction only: unknown keys are ignored
        (a row written by a newer version stays readable), missing keys fall back
        to the field default (a row written by an older one does too). Wrong
        *types* are not tolerated — those are a bug, not a version skew.
        """
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise InvariantViolation(f"meta must be a JSON object, got {type(raw).__name__}")

        def _opt_str(key: str) -> str | None:
            value = raw.get(key)
            if value is not None and not isinstance(value, str):
                raise InvariantViolation(f"meta.{key} must be a string or null")
            return value

        def _opt_int(key: str) -> int | None:
            value = raw.get(key)
            if value is not None and not _is_int(value):
                raise InvariantViolation(f"meta.{key} must be an integer or null")
            return value

        def _int(key: str, default: int) -> int:
            value = raw.get(key, default)
            if not _is_int(value):
                raise InvariantViolation(f"meta.{key} must be an integer")
            return value

        def _bool(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if not isinstance(value, bool):
                raise InvariantViolation(f"meta.{key} must be a boolean")
            return value

        tier = raw.get("extraction_tier")
        if tier is not None and tier not in get_args(ExtractionTier):
            raise InvariantViolation(f"meta.extraction_tier is not a known tier: {tier!r}")

        return cls(
            provider_used=_opt_str("provider_used"),
            model_used=_opt_str("model_used"),
            slot_used=_opt_str("slot_used"),
            requested_slot=_opt_str("requested_slot"),
            substituted=_bool("substituted", False),
            attempts=_int("attempts", 1),
            tokens_in=_opt_int("tokens_in"),
            tokens_out=_opt_int("tokens_out"),
            wasted_tokens_out=_int("wasted_tokens_out", 0),
            degraded=_bool("degraded", False),
            extraction_tier=tier,
        )


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    """One stored message — §2.2.2.

    Frozen because a message is a record of something that happened. The fitting
    step (D4) produces a *new* list of messages rather than mutating history; use
    :func:`with_content` if a derived copy is needed.

    ``seq``, not ``created_at``, is the ordering key. Two messages written inside
    the same millisecond are indistinguishable by timestamp, and a conversation
    whose turn order depends on clock resolution is a conversation that will
    eventually render backwards.
    """

    id: UUID
    conversation_id: UUID
    role: Role
    content: list[ContentBlock]
    meta: MessageMeta
    created_at: datetime
    seq: int
    schema_version: int = SCHEMA_VERSION

    def text(self) -> str:
        """Concatenate the text blocks. Convenience for logging and titles."""
        return "".join(block["text"] for block in self.content if is_text(block))

    def file_refs(self) -> list[FileRefBlock]:
        """Every file reference in this message, in order."""
        return [block for block in self.content if is_file_ref(block)]


def with_content(message: CanonicalMessage, content: list[ContentBlock]) -> CanonicalMessage:
    """A copy of ``message`` carrying different blocks.

    The rendering path rewrites content — injecting an extracted document, or
    truncating an oversized one — without touching what is stored.
    """
    return replace(message, content=content)


# --------------------------------------------------------------------------- #
# Violations
# --------------------------------------------------------------------------- #
class InvariantViolation(Exception):
    """A canonical-schema rule was broken.

    Not an ``AppError``: there is no message here that a client could act on, and
    nothing they did caused it. It travels to the catch-all handler, gets logged
    with a traceback, and the caller receives a generic 500 plus a ``request_id``.

    ``invariant`` carries the number from §2.2.3 when the failure maps to one of
    the six, which is what makes a log line say *which* rule broke rather than
    only that something did.
    """

    def __init__(self, message: str, *, invariant: int | None = None) -> None:
        self.invariant = invariant
        prefix = f"[invariant {invariant}] " if invariant is not None else ""
        super().__init__(f"{prefix}{message}")


def _is_int(value: object) -> TypeGuard[int]:
    """``bool`` is a subclass of ``int``; for a token count it is never intended."""
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# Parsing — the database boundary
# --------------------------------------------------------------------------- #
def parse_block(raw: object) -> ContentBlock:
    """Validate one stored block and hand it back as a typed dict.

    Returns the *same* object rather than a copy: it is already the right shape,
    and copying would only invite the two representations to drift.
    """
    if not isinstance(raw, dict):
        raise InvariantViolation(f"a content block must be an object, got {type(raw).__name__}")

    block_type = raw.get("type")
    if not isinstance(block_type, str):
        raise InvariantViolation("a content block must carry a string 'type'")

    if block_type in RESERVED_BLOCK_TYPES:
        raise InvariantViolation(
            f"block type {block_type!r} is reserved and not implemented in v1 "
            "(tool_call/tool_result are D3; summary is the §2.2.7 seam)"
        )
    if block_type not in _KNOWN_BLOCK_TYPES:
        raise InvariantViolation(f"unknown content block type {block_type!r}")

    fields = _BLOCK_FIELDS[block_type]
    expected = set(fields) | {"type"}
    actual = set(raw)

    missing = expected - actual
    if missing:
        raise InvariantViolation(
            f"{block_type} block is missing {sorted(missing)}",
            invariant=6 if block_type == "file_ref" else None,
        )

    extra = actual - expected
    if extra:
        # Invariant 6, stated as an error rather than a comment. A `file_ref`
        # carrying `extracted`, `text` or `data` is the exact failure mode the
        # invariant exists to prevent, and it is silent unless something refuses
        # it here.
        raise InvariantViolation(
            f"{block_type} block carries unexpected keys {sorted(extra)}; "
            "a file_ref stores only the hash, never bytes and never extracted text",
            invariant=6 if block_type == "file_ref" else None,
        )

    for name, expected_type in fields.items():
        value = raw[name]
        ok = _is_int(value) if expected_type is int else isinstance(value, expected_type)
        if not ok:
            raise InvariantViolation(
                f"{block_type}.{name} must be {getattr(expected_type, '__name__', expected_type)}"
            )

    if block_type == "omission_marker":
        if raw["reason"] not in get_args(TruncationReason):
            raise InvariantViolation(f"omission_marker.reason is unknown: {raw['reason']!r}")
        if raw["omitted_count"] < 1:
            raise InvariantViolation("omission_marker.omitted_count must be at least 1")

    if block_type == "file_ref" and raw["bytes"] < 0:
        raise InvariantViolation("file_ref.bytes must not be negative")

    # Validated field by field above; the cast is the payoff for that work.
    return raw  # type: ignore[return-value]


def parse_content(raw: object) -> list[ContentBlock]:
    """Validate a stored ``messages.content`` value.

    Invariant 4 is enforced here as well as by the DB CHECK, because content
    arrives from application code long before it reaches Postgres and an empty
    generation should be rejected at the point it is produced, not two layers
    later by a constraint violation nobody can read.
    """
    if not isinstance(raw, list):
        raise InvariantViolation(f"content must be an array, got {type(raw).__name__}", invariant=4)
    if not raw:
        raise InvariantViolation(
            "content is empty; an empty generation is an error, not a stored message",
            invariant=4,
        )
    return [parse_block(block) for block in raw]


def parse_meta(raw: object) -> MessageMeta:
    """Validate a stored ``messages.meta`` value."""
    return MessageMeta.from_jsonb(raw)


def parse_role(raw: object) -> Role:
    """Validate a stored ``messages.role`` value."""
    # Membership in `ROLES` narrows `object` to `Role`, so no cast is needed here
    # — which is the argument for declaring `ROLES` from `get_args(Role)` rather
    # than as a separate literal that could drift out of step with the type.
    if raw not in ROLES:
        raise InvariantViolation(f"unknown role {raw!r}")
    return raw


# --------------------------------------------------------------------------- #
# The six invariants (§2.2.3)
# --------------------------------------------------------------------------- #
def validate_message(
    *,
    role: Role,
    content: list[ContentBlock],
    meta: MessageMeta,
    seq: int,
) -> None:
    """The invariants that a single message can break on its own.

    Invariants 1 (the ``seq = 0`` half), 4 and 5. Split out from :func:`validate`
    so the append path can check a message it is about to write without loading
    the conversation it is being written into.
    """
    if not content:
        raise InvariantViolation(
            "content is empty; an empty generation is an error, not a stored message",
            invariant=4,
        )

    # Invariant 1, the positional half. Note the direction: a system message must
    # be at seq 0, but seq 0 need not be a system message — a conversation
    # without a system prompt starts at seq 0 with a user turn. Reading the
    # invariant as a biconditional is the natural mistake, so it is not one.
    if role == "system" and seq != 0:
        raise InvariantViolation(
            f"a system message must be the first message in a conversation, got seq={seq}",
            invariant=1,
        )

    # Invariant 5. Stated as a biconditional on purpose — the missing half
    # ("null on every user message") is what stops a user message from
    # accidentally inheriting the previous assistant's provenance and quietly
    # corrupting the usage numbers.
    if role == "assistant" and meta.provider_used is None:
        raise InvariantViolation(
            "assistant messages must record which provider produced them",
            invariant=5,
        )
    if role == "user" and meta.provider_used is not None:
        raise InvariantViolation(
            f"user messages must not claim a provider, got {meta.provider_used!r}",
            invariant=5,
        )


def validate_append(previous: CanonicalMessage | None, *, role: Role, seq: int) -> None:
    """The ordering invariants, checked against only the preceding message.

    Invariants 1, 2 and 3. ``previous`` is the current tail of the conversation,
    or ``None`` when it is still empty — which is all the context the ordering
    rules actually need, so the append path never has to read a whole history to
    write one row.
    """
    if previous is None:
        if seq != 0:
            raise InvariantViolation(
                f"the first message in a conversation must be seq 0, got {seq}", invariant=2
            )
        return

    if seq != previous.seq + 1:
        raise InvariantViolation(
            f"seq must be gap-free: appending {seq} after {previous.seq}", invariant=2
        )

    if role == "system":
        raise InvariantViolation(
            "a conversation may hold at most one system message, at seq 0", invariant=1
        )

    # Invariant 3, in the asymmetric form the contract specifies. Consecutive
    # user messages are real — someone sends a follow-up before the answer
    # arrives, or a retry lands. Consecutive assistant messages are not: every
    # assistant turn answers something, so two in a row means a turn was lost.
    if role == "assistant" and previous.role == "assistant":
        raise InvariantViolation(
            "two consecutive assistant messages; every assistant turn answers a user turn",
            invariant=3,
        )


def validate(messages: list[CanonicalMessage]) -> None:
    """Enforce all six invariants over a whole conversation history.

    The complete statement of §2.2.3, and the thing the render pipeline asserts
    before it builds a payload. Four of the six are also enforced by the schema
    or the append transaction; they are re-checked here because history reaches
    the renderer from a JSONB column that a migration, a manual fix, or a future
    bulk write could have left in a state no live code path would produce.

    Raises the *first* violation found rather than collecting them: the second
    complaint about a corrupt history is rarely independent of the first.
    """
    if not messages:
        return

    seen_seqs: set[int] = set()
    previous: CanonicalMessage | None = None

    for index, message in enumerate(messages):
        if message.seq in seen_seqs:
            raise InvariantViolation(f"duplicate seq {message.seq}", invariant=2)
        seen_seqs.add(message.seq)

        # Ordering is the caller's responsibility to get right, and silently
        # sorting a mis-ordered list here would hide the bug that produced it.
        if previous is not None and message.seq <= previous.seq:
            raise InvariantViolation(
                f"messages must be ordered by seq: {message.seq} follows {previous.seq}",
                invariant=2,
            )

        if index == 0 and message.seq != 0:
            raise InvariantViolation(f"history must start at seq 0, got {message.seq}", invariant=2)

        validate_message(
            role=message.role, content=message.content, meta=message.meta, seq=message.seq
        )
        validate_append(previous, role=message.role, seq=message.seq)
        previous = message
