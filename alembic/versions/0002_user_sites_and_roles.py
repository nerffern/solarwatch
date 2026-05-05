"""add user_sites table and site_admin/site_viewer roles

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-04 00:00:00.000000

Adds:
  - user_sites junction table (user_id → web_users, site_id → sites)
  - site_admin and site_viewer system roles
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # user_sites — maps non-admin users to the sites they can access
    op.create_table(
        "user_sites",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["web_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "site_id"),
    )

    # Insert new system roles — ignore if already exists
    op.execute(
        """
        INSERT INTO roles (name, description, is_system, enabled)
        VALUES
          ('site_admin',  'Site administrator — manage assigned sites and their users', TRUE, TRUE),
          ('site_viewer', 'Read-only access to assigned sites', TRUE, TRUE)
        ON CONFLICT (name) DO UPDATE
          SET is_system = TRUE, enabled = TRUE
        """
    )


def downgrade() -> None:
    op.drop_table("user_sites")
    op.execute(
        "DELETE FROM roles WHERE name IN ('site_admin', 'site_viewer') AND is_system = TRUE"
    )
