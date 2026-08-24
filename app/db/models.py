"""SQLAlchemy 2.0 declarative models — the Phase 1 tables.

Two things this module is careful about.

**Constraints live in the database where SQL can express them.** Contract B
(§2.2.3) lists six invariants for the canonical message schema. Four of them are
structural and are enforced here as CHECKs and unique indexes, so a bug in the
application layer cannot write a row that violates them. The other two — gap-free
``seq`` and ``meta.provider_used`` on assistant messages — are transaction-level
and semantic concerns respectively, enforced in ``app/memory/canonical.py`` and
``app/db/repo/messages.py``.

**No Postgres ENUM types.** ``role`` and ``tier`` are closed sets, but altering
an enum is migration-hostile and both values are already validated by pydantic on
the way in. Text plus a CHECK gets the same guarantee and stays cheap to change.

Columns that Phase 1 never writes — ``substituted``, ``attempts``,
``wasted_tokens_out``, ``pinned_model`` — are here on purpose. Adding a column to
a live free-tier Postgres later is more annoying than carrying four unused ones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import new_uuid

# Alembic can only emit a DROP for a constraint it can name. Without a naming
# convention, autogenerate produces anonymous CHECKs and indexes whose downgrade
# path silently does nothing.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base. Every model in the gateway hangs off this metadata."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # RUF012 wants a ClassVar annotation; SQLAlchemy already declares one on
    # DeclarativeBase and re-annotating it here fights the ORM's own typing.
    type_annotation_map = {  # noqa: RUF012
        # `text` rather than `varchar(n)`: Postgres stores them identically and a
        # length bump is then never a migration.
        str: Text(),
        datetime: DateTime(timezone=True),
        UUID: postgresql.UUID(as_uuid=True),
        dict[str, Any]: postgresql.JSONB,
        list[Any]: postgresql.JSONB,
    }


def _pk() -> Mapped[UUID]:
    """A UUID primary key, minted in Python with a server-side safety net."""
    return mapped_column(
        primary_key=True,
        default=new_uuid,
        server_default=text("gen_random_uuid()"),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
class User(Base):
    """Local mirror of a Supabase Auth user (§1.2).

    ``id`` is the Supabase ``sub`` claim, not a value we generate — the row is
    upserted on the first authenticated request. Passwords never touch this table;
    Supabase owns them.
    """

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("tier in ('free', 'plus')", name="tier_known"),)

    id: Mapped[UUID] = _pk()
    email: Mapped[str] = mapped_column(unique=True, nullable=False)
    """Lower-cased by the auth layer before write, so uniqueness is real."""

    email_verified: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    """Mirror of the Supabase ``user_metadata.email_verified`` claim, refreshed on
    every upsert so a user who confirms later stops looking stale here.

    Supabase is the authority — with "Confirm email" on it should never issue a
    session to an unconfirmed user. This column exists so the state is queryable
    without decoding a token, and so ``app/auth/jwt.py`` has something to check
    against as defence in depth."""

    tier: Mapped[str] = mapped_column(server_default=text("'free'"), nullable=False)
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- #
# api_keys
# --------------------------------------------------------------------------- #
class ApiKey(Base):
    """A gateway-issued ``gw_live_`` key — the programmatic half of D7.

    Only the SHA-256 hash is stored. ``key_prefix`` and ``last_4`` exist purely so
    the settings UI can show something recognizable; the key itself is displayed
    exactly once, at creation.
    """

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_user_id", "user_id"),)

    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(nullable=False)
    last_4: Mapped[str] = mapped_column(nullable=False)
    nickname: Mapped[str | None] = mapped_column(default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #
class Conversation(Base):
    """A thread of canonical messages, owned by exactly one user.

    Every read of this table is ownership-scoped in the SQL itself
    (``WHERE id = :cid AND user_id = :uid``), which is what the composite index
    below serves — it is also the sidebar's ordering.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_id_updated_at", "user_id", text("updated_at DESC")),
    )

    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(default=None)

    preferred_slot: Mapped[str] = mapped_column(server_default=text("'auto'"), nullable=False)
    """The user's standing slot choice. ``'auto'`` is a sentinel, not a slot in
    ``config/providers.yaml`` — the router expands it into a candidate list."""

    pinned_model: Mapped[str | None] = mapped_column(default=None)
    """D3 seam: set by the first tool call, honoured by the router thereafter.
    Nothing writes it in v1."""

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #
class Message(Base):
    """One canonical message. Contract B §2.2.4.

    ``content`` is a list of content blocks and ``meta`` is a ``MessageMeta`` dict;
    both are stored as JSONB and both are shaped by ``app/memory/canonical.py``.
    A provider's request body is never stored here — that is what makes swapping
    providers a rendering concern rather than a migration.

    The constraints below are Contract B's invariants 1 and 4. Invariant 2
    (gap-free ``seq``) is enforced by the append transaction in
    ``app/db/repo/messages.py``; invariants 3 and 5 are semantic and live in
    ``canonical.validate()``.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Invariant 2, the uniqueness half. Also the ordering index — do not add a
        # second index on (conversation_id, seq); this constraint already backs one.
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_id_seq"),
        CheckConstraint("role in ('system', 'user', 'assistant')", name="role_known"),
        CheckConstraint("seq >= 0", name="seq_non_negative"),
        # Invariant 4: content is never empty. An empty generation is an error, not
        # a stored message.
        CheckConstraint(
            "jsonb_typeof(content) = 'array' and jsonb_array_length(content) > 0",
            name="content_non_empty_array",
        ),
        # Invariant 1: a system message always sits at seq 0, which together with
        # the unique constraint above already caps a conversation at one system
        # message. The partial index below is therefore redundant *today* — it is
        # kept as the direct, self-documenting statement of the invariant, and as
        # the thing that still holds if the seq rule is ever relaxed.
        CheckConstraint("role <> 'system' or seq = 0", name="system_message_is_first"),
        Index(
            "uq_messages_one_system_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("role = 'system'"),
        ),
    )

    id: Mapped[UUID] = _pk()
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(nullable=False)

    content: Mapped[list[Any]] = mapped_column(nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    schema_version: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("1"), nullable=False
    )
    """Versioning stored JSONB from day one. Migrating untagged JSONB is miserable."""

    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
class Request(Base):
    """One row per inbound gateway request — the usage and debugging surface.

    Deletion semantics differ per foreign key on purpose. Deleting a user removes
    their rows; deleting an API key or a conversation must *not*, or the usage
    dashboard loses history every time someone rotates a key or tidies up a thread.
    """

    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_user_id_created_at", "user_id", text("created_at DESC")),
        Index("ix_requests_provider_created_at", "provider", text("created_at DESC")),
    )

    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    api_key_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), default=None
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), default=None
    )

    requested_slot: Mapped[str | None] = mapped_column(default=None)
    served_slot: Mapped[str | None] = mapped_column(default=None)
    provider: Mapped[str | None] = mapped_column(default=None)
    model: Mapped[str | None] = mapped_column(default=None)

    substituted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    attempts: Mapped[list[Any]] = mapped_column(
        default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    """D1's attempt trail. One ``messages`` row per logical message, but every
    failover attempt is recorded here. Empty until Phase 2."""

    tokens_in: Mapped[int | None] = mapped_column(Integer, default=None)
    tokens_out: Mapped[int | None] = mapped_column(Integer, default=None)
    wasted_tokens_out: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    """Tokens generated by an attempt that was then discarded (D1). Really spent,
    so they count against quota even though the text never reached the client."""

    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(nullable=False)
    """``ok`` | ``error`` in Phase 1. Left unconstrained: Phases 2-3 add values and
    a CHECK here would mean a migration each time."""

    error_code: Mapped[str | None] = mapped_column(default=None)
    cache_hit: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)

    ttft_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    """Time to first token, streamed turns only. Phase 2's ``StreamCompleted`` and
    ``StreamResult`` have carried this number end to end since Step 9; this is
    the first column that keeps it instead of discarding it at the end of the
    request."""

    quota_scope: Mapped[str] = mapped_column(Text, server_default=text("'system'"), nullable=False)
    """What paid for this request — ``"system"`` or a ``user_id`` (Contract C's
    ``Scope``). Constant by construction until Phase 6's BYOK resolver exists;
    written now so every row is unambiguous rather than assumed system-scoped."""

    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
class File(Base):
    """One user's right to reference an uploaded object. Phase 4, D23/D24.

    The bytes and the object are content-addressed and global — two users
    uploading identical bytes write the same ``storage_path`` once. The *right
    to reference* a hash is per user, which is what this row is: ownership
    lives here, not on the object store, and every ``file_ref`` that enters a
    message is validated against it with ``WHERE file_hash = ANY(:hashes) AND
    user_id = :uid`` before a single message row is written (D24) — a hash the
    caller does not own is a 404, the same rule ``conversations`` follows.
    """

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("user_id", "file_hash", name="uq_files_user_id_file_hash"),
        Index("ix_files_file_hash", "file_hash"),
    )

    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    file_hash: Mapped[str] = mapped_column(nullable=False)
    """SHA-256 of the bytes, computed while streaming the upload (trap 3). The
    identity everything else in the perception lane keys on — never the object
    store path, which is derived from this rather than the other way around."""

    filename: Mapped[str] = mapped_column(nullable=False)
    mime: Mapped[str] = mapped_column(nullable=False)
    """The *sniffed* type, not the client's declared ``Content-Type`` (trap 2) —
    ``POST /v1/files`` stores what the leading bytes actually are."""

    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(nullable=False)
    """Content-addressed: ``{hash[:2]}/{hash}`` in ``FILES_BUCKET``. Never a
    signed or public URL — the bucket stays private, and the only reader of the
    bytes is the gateway itself, resolving an attachment for a model (D23)."""

    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- #
# file_extractions
# --------------------------------------------------------------------------- #
class FileExtraction(Base):
    """The perception lane's extraction cache. Phase 4, D22/D25.

    Keyed on ``file_hash`` alone, unlike :class:`File` — content-addressed *and*
    global, because the extracted text of a byte sequence is a property of
    those bytes, not of who uploaded them. Re-extracting it per user would
    spend the scarcest budget in the fleet to compute the same string twice.

    A row here exists only for tier 2 (``llm``) or tier 3 (``local``) — tier 0
    (``cache``) reads this table rather than writing it, and tier 1
    (``native``) never produces one at all, which is exactly what the ``tier``
    CHECK below encodes.
    """

    __tablename__ = "file_extractions"
    __table_args__ = (
        CheckConstraint(
            "extraction_confidence in ('high', 'medium', 'low')",
            name="extraction_confidence_known",
        ),
        CheckConstraint("tier in ('llm', 'local')", name="tier_known"),
    )

    file_hash: Mapped[str] = mapped_column(primary_key=True)

    text: Mapped[str] = mapped_column(nullable=False)
    """D28's four labelled sections (summary first), never raw concatenation —
    the envelope that lets a model tell document content from instruction."""

    extracted_by_provider: Mapped[str] = mapped_column(nullable=False)
    """``"local"`` for a tier-3 row — there is no upstream provider to name,
    and the alternative (``NULL``) would make every query that groups by
    provider carry a null-handling special case for exactly one tier."""

    extracted_by_model: Mapped[str] = mapped_column(nullable=False)
    extraction_confidence: Mapped[str] = mapped_column(nullable=False)
    """``high``/``medium`` from tier 2, always ``low`` from tier 3 — never
    absent, since ``low`` is what sets ``RenderReport.degraded``."""

    tier: Mapped[str] = mapped_column(nullable=False)
    """``llm`` or ``local`` — which tier actually produced this row. Never
    ``cache`` or ``native``; see the class docstring."""

    pages: Mapped[int | None] = mapped_column(Integer, default=None)
    """Known before the payload is built for a native attachment too (D27) —
    an image has none, so this stays nullable rather than defaulting to zero
    and reading as "zero pages"."""

    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- #
# provider_keys
# --------------------------------------------------------------------------- #
class ProviderKey(Base):
    """A BYOK credential. Phase 6, D36/D37.

    ``project-overview.md`` §6 describes ``owner_type='system'`` rows backing
    the shared pool. D37 decided against that: the shared pool stays in
    ``Settings`` (env-injected, checked at boot, no database round trip on the
    hot path), so **only ``owner_type='user'`` rows are ever written in v1**.
    The column and its CHECK are created exactly as §6 specifies so a later
    phase can populate the other half without a migration.

    ``encrypted_key`` is a Fernet token (``app/core/crypto.py``) — the
    plaintext never reaches this table. There is deliberately no index on
    ``encrypted_key`` and no uniqueness on ``last_4``: nothing looks a key up
    by its value, only by owner, which is what
    ``ix_provider_keys_owner_id_provider`` serves.
    """

    __tablename__ = "provider_keys"
    __table_args__ = (
        CheckConstraint("owner_type in ('system', 'user')", name="owner_type_known"),
        # owner_id is null exactly when the row is system-owned.
        CheckConstraint(
            "(owner_type = 'system' and owner_id is null) "
            "or (owner_type = 'user' and owner_id is not null)",
            name="owner_type_matches_owner_id",
        ),
        CheckConstraint(
            "validation_status in ('valid', 'invalid', 'unverified')",
            name="validation_status_known",
        ),
        Index("ix_provider_keys_owner_id_provider", "owner_id", "provider"),
        # §9.5's granularity: one live key per (user, provider) — the reason
        # "replace" is remove-then-add rather than a rotation flow (D36).
        # Partial so a soft-deleted row never blocks re-adding.
        Index(
            "uq_provider_keys_owner_id_provider_active",
            "owner_id",
            "provider",
            unique=True,
            postgresql_where=text("owner_type = 'user' and is_active"),
        ),
    )

    id: Mapped[UUID] = _pk()
    owner_type: Mapped[str] = mapped_column(nullable=False)
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None
    )
    provider: Mapped[str] = mapped_column(nullable=False)
    """Matches a ``providers.yaml`` key. Deliberately not a foreign key to
    anything — the provider registry is config, not a table."""

    encrypted_key: Mapped[str] = mapped_column(nullable=False)
    last_4: Mapped[str] = mapped_column(nullable=False)
    """The only part of the credential that ever leaves this table."""

    nickname: Mapped[str | None] = mapped_column(default=None)

    validation_status: Mapped[str] = mapped_column(
        server_default=text("'unverified'"), nullable=False
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    """Written at resolve time, not at success time (D38) — throttled the same
    way ``api_keys.last_used_at`` is, and for the same reason: threading a
    post-success callback through both lanes to answer "when was this handed
    out" more precisely would be machinery for a column nobody sorts on."""

    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
    """Remove is a soft delete, like ``api_keys.revoke`` — ``requests`` never
    references this table directly, but the settings UI still benefits from
    being able to show a key that was removed rather than pretending it never
    existed. The partial unique index above is what makes re-adding after a
    soft delete possible."""

    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- #
# user_quota_allocations
# --------------------------------------------------------------------------- #
class UserQuotaAllocation(Base):
    """D39's personal daily cap on the shared pool — an override table.

    Stores the *cap*, not the *count*. The live count is Redis
    (``keys.user_allocation``, Phase 6 Step 7's ``q:{user_id}:{provider}:
    {model}:alloc:rpd``); a row here says "this user's slice of the shared
    pool for this (provider, model) is N requests/day". No row and no
    configured tier default in ``config/limits.yaml`` means no personal cap —
    today's behaviour. ``project-overview.md`` §6 also names ``daily_used``
    and ``window_reset_at`` on this table; they are deliberately absent here,
    because the live count already lives in Redis and two perpetually-null
    columns would be worse than none (D39) — the same "the overview names a
    table, Redis holds the counter" precedent ``file_extractions``/
    ``provider_quota_state`` already set.

    Read-only in Phase 6 Step 1: rows are written by hand or a seed script
    until an admin surface exists (Phase 7's ``api/admin.py``).
    """

    __tablename__ = "user_quota_allocations"
    __table_args__ = (
        CheckConstraint("daily_cap > 0", name="daily_cap_positive"),
        UniqueConstraint(
            "user_id",
            "provider",
            "model",
            name="uq_user_quota_allocations_user_id_provider_model",
        ),
    )

    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(nullable=False)
    daily_cap: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
