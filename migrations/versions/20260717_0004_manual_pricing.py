"""Add versioned manual pricing and quote snapshots.

Revision ID: 20260717_0004
Revises: 20260717_0003
"""

import sqlalchemy as sa
from alembic import op

from app.db.models import AdminPermission, ManualPriceSequence, ManualProviderPrice

revision = "20260717_0004"
down_revision = "20260717_0003"
branch_labels = None
depends_on = None


def _columns(table: str) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    AdminPermission.__table__.create(bind, checkfirst=True)
    ManualPriceSequence.__table__.create(bind, checkfirst=True)
    ManualProviderPrice.__table__.create(bind, checkfirst=True)

    columns = _columns("price_quotes")
    if "manual_price_id" not in columns:
        op.add_column("price_quotes", sa.Column("manual_price_id", sa.String(36)))
        op.create_foreign_key(
            "fk_price_quotes_manual_price_id_manual_provider_prices",
            "price_quotes",
            "manual_provider_prices",
            ["manual_price_id"],
            ["id"],
        )
        op.create_index("ix_price_quotes_manual_price_id", "price_quotes", ["manual_price_id"])
    if "price_version" not in columns:
        op.add_column("price_quotes", sa.Column("price_version", sa.Integer()))
    if "price_source" not in columns:
        op.add_column("price_quotes", sa.Column("price_source", sa.String(32)))
        op.execute("UPDATE price_quotes SET price_source = 'API'")
        op.alter_column("price_quotes", "price_source", nullable=False)
    if "expected_profit_som" not in columns:
        op.add_column("price_quotes", sa.Column("expected_profit_som", sa.BigInteger()))
        op.execute(
            "UPDATE price_quotes SET expected_profit_som = sale_price_som - provider_cost_som"
        )
        op.alter_column("price_quotes", "expected_profit_som", nullable=False)
    if columns.get("provider_service_id", {}).get("nullable") is False:
        op.alter_column(
            "price_quotes",
            "provider_service_id",
            existing_type=sa.String(36),
            nullable=True,
        )


def downgrade() -> None:
    # SQLite cannot ALTER a nullable column back to NOT NULL. In local/test
    # databases the baseline migration owns the full schema and will remove
    # these objects on its own when the downgrade continues to base.
    if op.get_bind().dialect.name == "sqlite":
        return
    columns = _columns("price_quotes")
    if "provider_service_id" in columns:
        # Manual-only quotes must be removed before this downgrade.
        op.execute("DELETE FROM price_quotes WHERE provider_service_id IS NULL")
        op.alter_column(
            "price_quotes",
            "provider_service_id",
            existing_type=sa.String(36),
            nullable=False,
        )
    if "expected_profit_som" in columns:
        op.drop_column("price_quotes", "expected_profit_som")
    if "price_source" in columns:
        op.drop_column("price_quotes", "price_source")
    if "price_version" in columns:
        op.drop_column("price_quotes", "price_version")
    if "manual_price_id" in columns:
        op.drop_index("ix_price_quotes_manual_price_id", table_name="price_quotes")
        op.drop_constraint(
            "fk_price_quotes_manual_price_id_manual_provider_prices",
            "price_quotes",
            type_="foreignkey",
        )
        op.drop_column("price_quotes", "manual_price_id")
    ManualProviderPrice.__table__.drop(op.get_bind(), checkfirst=True)
    ManualPriceSequence.__table__.drop(op.get_bind(), checkfirst=True)
    AdminPermission.__table__.drop(op.get_bind(), checkfirst=True)
