"""Allow catalog entries without a provider price.

Revision ID: 20260717_0002
Revises: 20260717_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20260717_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_services") as batch_op:
        batch_op.alter_column(
            "provider_price_som",
            existing_type=sa.BigInteger(),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("provider_services") as batch_op:
        batch_op.alter_column(
            "provider_price_som",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
