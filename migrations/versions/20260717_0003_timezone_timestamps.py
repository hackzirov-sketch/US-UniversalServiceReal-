"""Store mixin timestamps with timezone information.

Revision ID: 20260717_0003
Revises: 20260717_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "20260717_0003"
down_revision = "20260717_0002"
branch_labels = None
depends_on = None

TABLES = ("users", "providers", "provider_services", "pricing_rules", "orders")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.alter_column(
            table,
            "created_at",
            existing_type=sa.DateTime(timezone=False),
            type_=sa.DateTime(timezone=True),
            postgresql_using="created_at AT TIME ZONE 'UTC'",
        )
        op.alter_column(
            table,
            "updated_at",
            existing_type=sa.DateTime(timezone=False),
            type_=sa.DateTime(timezone=True),
            postgresql_using="updated_at AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.alter_column(
            table,
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(timezone=False),
            postgresql_using="created_at AT TIME ZONE 'UTC'",
        )
        op.alter_column(
            table,
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(timezone=False),
            postgresql_using="updated_at AT TIME ZONE 'UTC'",
        )
