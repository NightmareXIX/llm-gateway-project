"""requests.ttft_ms, requests.quota_scope

Phase 3, Step 1. Neither column is anything this step's logic needs — both are
the "add it now anyway" doctrine of development-plan.md's Step 2, applied to
numbers the code already computes and currently throws away.

``ttft_ms`` (nullable): Phase 2's plan called for it, ``StreamCompleted.ttft_ms``
and ``StreamResult.ttft_ms`` already carry the number end to end, and no column
ever landed to hold it. Every streamed turn measures time-to-first-token and
discards it.

``quota_scope`` (not null, ``server_default='system'``): what paid for this
request. Constant today by construction — every call site passes ``system`` — but
the moment Phase 6's BYOK resolver exists, every row written before this
migration is unambiguous rather than merely assumed to have been system-scoped.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("requests", sa.Column("ttft_ms", sa.Integer(), nullable=True))
    op.add_column(
        "requests",
        sa.Column(
            "quota_scope",
            sa.Text(),
            server_default=sa.text("'system'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("requests", "quota_scope")
    op.drop_column("requests", "ttft_ms")
