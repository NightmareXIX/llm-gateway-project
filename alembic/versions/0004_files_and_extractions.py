"""files, file_extractions

Phase 4, Step 1. Two tables, per doc/reference/phase4.md §1.

``files`` is ownership: one row per (user, hash), content-addressed and
deduplicated at the object-store level (two users uploading identical bytes
write the same ``storage_path`` once, but get two rows here) — D24. The
``ix_files_file_hash`` index is separate from the ``(user_id, file_hash)``
unique constraint's own index because a dedup check ("does this object already
exist, for *any* user") looks up by hash alone.

``file_extractions`` is the perception lane's extraction cache — content-
addressed *and* global, keyed on ``file_hash`` by itself, because the extracted
text of a byte sequence is the same fact for everyone who holds those bytes
(D22). A row here is only ever produced by tier 2 (``llm``) or tier 3
(``local``); tier 0 reads the table rather than writing it and tier 1 never
produces a row at all, which the ``tier`` CHECK encodes.

Neither table exists yet in any running instance — this is the first migration
of the phase, and nothing reads or writes either table until later steps.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----------------------------------------------------------------- files --
    op.create_table(
        "files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_hash", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_files_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_files"),
        sa.UniqueConstraint("user_id", "file_hash", name="uq_files_user_id_file_hash"),
    )
    op.create_index("ix_files_file_hash", "files", ["file_hash"])

    # ------------------------------------------------------ file_extractions --
    op.create_table(
        "file_extractions",
        sa.Column("file_hash", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("extracted_by_provider", sa.Text(), nullable=False),
        sa.Column("extracted_by_model", sa.Text(), nullable=False),
        sa.Column("extraction_confidence", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "extraction_confidence in ('high', 'medium', 'low')",
            name="extraction_confidence_known",
        ),
        sa.CheckConstraint("tier in ('llm', 'local')", name="tier_known"),
        sa.PrimaryKeyConstraint("file_hash", name="pk_file_extractions"),
    )


def downgrade() -> None:
    op.drop_table("file_extractions")

    op.drop_index("ix_files_file_hash", table_name="files")
    op.drop_table("files")
