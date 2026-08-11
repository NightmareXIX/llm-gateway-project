"""users.email_verified

Phase 1, Step 3. A local mirror of Supabase's ``user_metadata.email_verified``
claim, refreshed on every upsert in ``app/db/repo/users.py``.

A separate migration rather than an edit to ``0001``: that revision is already
applied to running databases, and rewriting an applied migration means every
environment silently disagrees about what "head" contains.

``server_default false`` is what makes this safe on a non-empty table — existing
rows become unverified, which is the fail-closed direction. Any real user's row
corrects itself on their next authenticated request.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "email_verified")
