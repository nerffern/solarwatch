"""add share_token to sites

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-04 00:00:00.000000

Adds a nullable share_token column to the sites table.
NULL = sharing disabled for that site.
Non-null = a 32-char hex token that grants read-only access via /share/{token}.
Regenerating the token instantly revokes the old share link.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("share_token", sa.Text(), nullable=True))
    op.create_index(
        "idx_sites_share_token",
        "sites",
        ["share_token"],
        unique=True,
        postgresql_where=sa.text("share_token IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_sites_share_token", table_name="sites")
    op.drop_column("sites", "share_token")
