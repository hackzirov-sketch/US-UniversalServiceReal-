"""Add database-backed Telegram button designs.

Revision ID: 20260718_0007
Revises: 20260717_0006
"""

from alembic import op

from app.db.models import ButtonDesign

revision = "20260718_0007"
down_revision = "20260717_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ButtonDesign.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    ButtonDesign.__table__.drop(op.get_bind(), checkfirst=True)
