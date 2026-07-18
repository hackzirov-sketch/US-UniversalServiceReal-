"""Add one encrypted primary payment card and receipt review fields.

Revision ID: 20260717_0006
Revises: 20260717_0005
"""

import sqlalchemy as sa
from alembic import op

from app.db.models import PaymentCard

revision = "20260717_0006"
down_revision = "20260717_0005"
branch_labels = None
depends_on = None


PAYMENT_COLUMNS = (
    ("payment_card_id", sa.Column("payment_card_id", sa.String(36), nullable=True)),
    ("approved_amount_som", sa.Column("approved_amount_som", sa.BigInteger(), nullable=True)),
    (
        "card_number_first4_snapshot",
        sa.Column("card_number_first4_snapshot", sa.String(4), nullable=True),
    ),
    (
        "card_number_last4_snapshot",
        sa.Column("card_number_last4_snapshot", sa.String(4), nullable=True),
    ),
    (
        "card_holder_name_snapshot",
        sa.Column("card_holder_name_snapshot", sa.String(128), nullable=True),
    ),
    ("receipt_file_id", sa.Column("receipt_file_id", sa.String(255), nullable=True)),
    ("receipt_file_type", sa.Column("receipt_file_type", sa.String(32), nullable=True)),
    ("receipt_mime_type", sa.Column("receipt_mime_type", sa.String(64), nullable=True)),
    ("receipt_file_size", sa.Column("receipt_file_size", sa.Integer(), nullable=True)),
    ("submitted_at", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True)),
    (
        "reviewed_by_admin_id",
        sa.Column("reviewed_by_admin_id", sa.BigInteger(), nullable=True),
    ),
)


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    PaymentCard.__table__.create(bind, checkfirst=True)

    existing_columns = _column_names("payments")
    added_card_id = False
    for name, column in PAYMENT_COLUMNS:
        if name not in existing_columns:
            op.add_column("payments", column)
            added_card_id = added_card_id or name == "payment_card_id"

    if added_card_id and bind.dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_payments_payment_card_id_payment_cards",
            "payments",
            "payment_cards",
            ["payment_card_id"],
            ["id"],
        )

    payment_indexes = _index_names("payments")
    if "ix_payments_payment_card_id" not in payment_indexes:
        op.create_index("ix_payments_payment_card_id", "payments", ["payment_card_id"])
    if "ix_payments_status" not in payment_indexes:
        op.create_index("ix_payments_status", "payments", ["status"])
    if "ix_payments_review_queue" not in payment_indexes:
        op.create_index("ix_payments_review_queue", "payments", ["status", "submitted_at"])

    unique_names = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_unique_constraints("ledger_entries")
    }
    ledger_indexes = _index_names("ledger_entries")
    if "uq_ledger_entries_payment_id" not in unique_names | ledger_indexes:
        if bind.dialect.name == "sqlite":
            op.create_index(
                "uq_ledger_entries_payment_id",
                "ledger_entries",
                ["payment_id"],
                unique=True,
            )
        else:
            op.create_unique_constraint(
                "uq_ledger_entries_payment_id", "ledger_entries", ["payment_id"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    unique_names = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_unique_constraints("ledger_entries")
    }
    if "uq_ledger_entries_payment_id" in unique_names:
        op.drop_constraint("uq_ledger_entries_payment_id", "ledger_entries", type_="unique")
    for index_name in (
        "ix_payments_review_queue",
        "ix_payments_status",
        "ix_payments_payment_card_id",
    ):
        if index_name in _index_names("payments"):
            op.drop_index(index_name, table_name="payments")
    foreign_keys = {
        key["name"] for key in sa.inspect(bind).get_foreign_keys("payments") if key["name"]
    }
    if "fk_payments_payment_card_id_payment_cards" in foreign_keys:
        op.drop_constraint(
            "fk_payments_payment_card_id_payment_cards", "payments", type_="foreignkey"
        )
    for name, _column in reversed(PAYMENT_COLUMNS):
        if name in _column_names("payments"):
            op.drop_column("payments", name)
    PaymentCard.__table__.drop(bind, checkfirst=True)
