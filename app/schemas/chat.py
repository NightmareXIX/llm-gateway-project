"""Wire models for ``POST /v1/chat/completions``.

**OpenAI-shaped, plus provenance.** The body a client parses is the familiar
``choices[0].message.content`` / ``usage`` structure, so an existing SDK can point
at this gateway unchanged. What is added — :class:`ServedBy` and the four fields
beside it — is the D1/D2 disclosure, and it is present from the very first
response Phase 1 ever returns. Phase 1 cannot substitute anything, so those
fields are constant; shipping them anyway is what stops Phase 2 from being a
breaking change for every client.

The names deliberately match the ``done`` event of §1.1's SSE protocol
field-for-field — ``served_by``, ``requested_slot``, ``substituted``,
``attempts``, ``usage``, ``degraded``. A frontend that learns to render a
non-streaming answer's provenance has, by construction, already learned to render
a streamed one's.

Request/response models only. Nothing here imports from the rest of the app or
knows what a ``ModelSpec`` is; ``app/api/v1/chat.py`` does the translating.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

InputRole = Literal["system", "user"]
"""What a client may author.

``assistant`` is absent on purpose. The gateway owns conversational state
(Contract B), so an assistant turn is something *it* produced and recorded with
provenance — accepting one from a client would let the caller forge history,
and it violates invariant 5 (``meta.provider_used`` non-null on every assistant
message) the moment it is stored. Prior turns come from ``conversation_id``, not
from the request body.
"""


class InputMessage(BaseModel):
    """One turn the caller is contributing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: InputRole = "user"
    content: str = Field(min_length=1)
    """Plain text. Content blocks are Contract B's storage shape, not the client's
    input shape — a ``file_ref`` is produced by ``POST /v1/files`` in Phase 4 and
    referenced by hash, never inlined here."""


class ChatCompletionRequest(BaseModel):
    """The request body.

    Either ``conversation_id`` plus a new turn, or just the turns to open a new
    thread with. There is no third form: history is never supplied by the client,
    because the gateway is the thing that owns it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = "auto"
    """A logical slot — ``auto``, ``general``, ``fast`` — never a provider's model
    name. The indirection is why swapping which model serves a slot is a YAML
    edit no client can observe."""

    messages: list[InputMessage] = Field(min_length=1)
    conversation_id: UUID | None = None
    """Continue an existing thread. Absent, a new one is created and its id comes
    back on the response."""

    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    stop: list[str] = Field(default_factory=list, max_length=4)
    stream: bool = False
    """Phase 2. Refused with a 400 rather than silently downgraded to a
    non-streaming answer — a client that asked for deltas and got one blob would
    have no way to tell that from a very fast model."""

    @field_validator("messages")
    @classmethod
    def _system_message_is_first(cls, messages: list[InputMessage]) -> list[InputMessage]:
        """Invariant 1, caught at the edge rather than at the append.

        A system message anywhere but the front cannot be stored at ``seq = 0``.
        Rejecting it here makes the failure a 422 naming the field, instead of an
        ``InvariantViolation`` that surfaces as a generic 500 three layers down.
        (Whether a *leading* system message is legal also depends on the
        conversation being new; that half needs the database, so the endpoint
        owns it.)
        """
        if any(message.role == "system" for message in messages[1:]):
            raise ValueError("a system message may only be the first message")
        return messages

    @field_validator("messages")
    @classmethod
    def _ends_with_a_user_turn(cls, messages: list[InputMessage]) -> list[InputMessage]:
        """There has to be something to answer.

        A request carrying only a system prompt asks the model to respond to
        nothing. Invariant 3 would tolerate it — consecutive user messages are
        legal, and so is a lone system message — so nothing further down the
        stack would object; the model would simply improvise, and the stored
        thread would read as though the user had said something they did not.
        """
        if messages[-1].role != "user":
            raise ValueError("the final message must be a user message")
        return messages


class ServedBy(BaseModel):
    """Which model actually answered — the frontend's model indicator reads this.

    Always populated, never optional. D1 and D2 both resolve to "substitute
    silently, then disclose", and this object is the disclosure; making it
    conditional would make the honesty conditional.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: str
    provider: str
    model: str


class UsageOut(BaseModel):
    """Token accounting, in OpenAI's field names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    estimated: bool = False
    """True when the provider reported no usage and these are our own numbers.
    Not an OpenAI field; free tiers omit usage often enough that a client
    charting token spend needs to know which points are measurements."""


class AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["assistant"] = "assistant"
    content: str


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = 0
    message: AssistantMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    """The response body: OpenAI's shape, plus everything the gateway knows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """The gateway's ``request_id`` — the same value in the ``X-Request-ID``
    header and on every log line for this call. A user quoting it from the UI is
    quoting a log query."""

    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    """The provider's wire name, for OpenAI compatibility. The slot the client
    asked for is ``requested_slot``, and what served it is ``served_by``."""

    choices: list[Choice]
    usage: UsageOut

    # ---- provenance: the §1.1 `done` fields ------------------------------- #
    served_by: ServedBy
    requested_slot: str
    substituted: bool = False
    """True only when the client named a specific slot and a different one
    answered. Resolving ``auto`` to a concrete slot is not substitution — nothing
    was overridden — so Phase 1 can only ever emit ``false``."""

    attempts: int = 1
    """How many provider attempts this message took (D1). Always 1 until Phase 2
    has something to fail over to."""

    degraded: bool = False
    """Set when an attachment reached the model through local OCR rather than a
    model that can read it (Phase 4)."""

    # ---- gateway state ----------------------------------------------------- #
    conversation_id: UUID
    message_id: UUID
    """The stored assistant message. The client needs it to key a re-render or a
    future edit against the row that actually exists."""
