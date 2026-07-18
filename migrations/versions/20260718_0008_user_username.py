"""Add username to users for @username admin lookup and support relay.

Revision ID: 20260718_0008
Revises: 20260718_0007
"""

import sqlalchemy as sa
from alembic import op

revision = "20260718_0008"
down_revision = "20260718_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "username" not in columns:
        op.add_column("users", sa.Column("username", sa.String(length=32), nullable=True))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("users")}
    if "ix_users_username" not in indexes:
        op.create_index("ix_users_username", "users", ["username"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_username" in indexes:
        op.drop_index("ix_users_username", table_name="users")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "username" in columns:
        op.drop_column("users", "username")
