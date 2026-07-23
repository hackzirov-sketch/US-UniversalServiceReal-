"""Replace the external provider identity with direct fulfillment.

Revision ID: 20260723_0011
Revises: 20260718_0010
"""

import sqlalchemy as sa
from alembic import op

revision = "20260723_0011"
down_revision = "20260718_0010"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if "providers" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE providers
                SET code = 'DIRECT',
                    name = 'Direct fulfillment',
                    enabled = false,
                    status = 'DISABLED'
                WHERE code = 'MYXVEST'
                """
            )
        )
    if "manual_provider_prices" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE manual_provider_prices
                SET service_key = 'DIRECT:' || substr(service_key, 9)
                WHERE service_key LIKE 'MYXVEST:%'
                """
            )
        )
    if "manual_price_sequences" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE manual_price_sequences
                SET service_key = 'DIRECT:' || substr(service_key, 9)
                WHERE service_key LIKE 'MYXVEST:%'
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if "manual_provider_prices" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE manual_provider_prices
                SET service_key = 'MYXVEST:' || substr(service_key, 8)
                WHERE service_key LIKE 'DIRECT:%'
                """
            )
        )
    if "manual_price_sequences" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE manual_price_sequences
                SET service_key = 'MYXVEST:' || substr(service_key, 8)
                WHERE service_key LIKE 'DIRECT:%'
                """
            )
        )
    if "providers" in tables:
        bind.execute(
            sa.text(
                """
                UPDATE providers
                SET code = 'MYXVEST',
                    name = 'Myxvest',
                    enabled = false,
                    status = 'DISABLED'
                WHERE code = 'DIRECT'
                """
            )
        )
