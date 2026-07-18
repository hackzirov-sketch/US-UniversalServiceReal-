"""Add Telegram Web App sessions, balances and farm state.

Revision ID: 20260718_0010
Revises: 20260718_0009
"""

import sqlalchemy as sa
from alembic import op

revision = "20260718_0010"
down_revision = "20260718_0009"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _checks(table: str) -> set[str]:
    if table not in _tables():
        return set()
    return {
        item["name"] for item in sa.inspect(op.get_bind()).get_check_constraints(table)
    }


def _add_index(table: str, name: str, columns: list[str], *, unique: bool = False) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    user_columns = _columns("users")
    for name in ("bonus_balance_som", "farm_points", "ranking_points"):
        if name not in user_columns:
            op.add_column(
                "users", sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")
            )
    for name, expression in (
        ("bonus_balance_nonnegative", "bonus_balance_som >= 0"),
        ("farm_points_nonnegative", "farm_points >= 0"),
        ("ranking_points_nonnegative", "ranking_points >= 0"),
    ):
        if name not in _checks("users") and f"ck_users_{name}" not in _checks("users"):
            op.create_check_constraint(name, "users", expression)

    payment_columns = _columns("payments")
    for name, column_type in (
        ("receipt_file_unique_id", sa.String(255)),
        ("receipt_checksum", sa.String(64)),
        ("review_note", sa.String(500)),
    ):
        if name not in payment_columns:
            op.add_column("payments", sa.Column(name, column_type))
    _add_index("payments", "ix_payments_receipt_file_unique_id", ["receipt_file_unique_id"])
    _add_index("payments", "ix_payments_receipt_checksum", ["receipt_checksum"])

    if "web_sessions" not in _tables():
        op.create_table(
            "web_sessions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("csrf_hash", sa.String(64), nullable=False),
            sa.Column("admin_session_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        )
    _add_index("web_sessions", "ix_web_sessions_user_id", ["user_id"])
    _add_index("web_sessions", "ix_web_sessions_token_hash", ["token_hash"], unique=True)
    _add_index("web_sessions", "ix_web_sessions_expires_at", ["expires_at"])
    _add_index("web_sessions", "ix_web_sessions_revoked_at", ["revoked_at"])

    if "telegram_auth_replays" not in _tables():
        op.create_table(
            "telegram_auth_replays",
            sa.Column("init_data_hash", sa.String(64), primary_key=True),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _add_index("telegram_auth_replays", "ix_telegram_auth_replays_telegram_id", ["telegram_id"])
    _add_index("telegram_auth_replays", "ix_telegram_auth_replays_expires_at", ["expires_at"])

    if "farm_profiles" not in _tables():
        op.create_table(
            "farm_profiles",
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("energy", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("water", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("seeds", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("xp", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint("energy >= 0", name="farm_energy_nonnegative"),
            sa.CheckConstraint("water >= 0", name="farm_water_nonnegative"),
            sa.CheckConstraint("seeds >= 0", name="farm_seeds_nonnegative"),
            sa.CheckConstraint("xp >= 0", name="farm_xp_nonnegative"),
            sa.CheckConstraint("level > 0", name="farm_level_positive"),
        )
    if "farm_plots" not in _tables():
        op.create_table(
            "farm_plots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("slot", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(32), nullable=False, server_default="EMPTY"),
            sa.Column("crop", sa.String(32)),
            sa.Column("planted_at", sa.DateTime(timezone=True)),
            sa.Column("ready_at", sa.DateTime(timezone=True)),
            sa.Column("harvested_at", sa.DateTime(timezone=True)),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.UniqueConstraint("user_id", "slot", name="uq_farm_plot_user_slot"),
            sa.CheckConstraint("slot >= 0", name="farm_plot_slot_nonnegative"),
            sa.CheckConstraint("version > 0", name="farm_plot_version_positive"),
        )
    _add_index("farm_plots", "ix_farm_plots_user_id", ["user_id"])

    if "farm_rewards" not in _tables():
        op.create_table(
            "farm_rewards",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("plot_id", sa.String(36), sa.ForeignKey("farm_plots.id"), nullable=False),
            sa.Column("amount", sa.BigInteger(), nullable=False),
            sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reversed_at", sa.DateTime(timezone=True)),
            sa.Column("reversed_by_telegram_id", sa.BigInteger()),
            sa.CheckConstraint("amount > 0", name="farm_reward_amount_positive"),
        )
    _add_index("farm_rewards", "ix_farm_rewards_user_id", ["user_id"])
    _add_index("farm_rewards", "ix_farm_rewards_plot_id", ["plot_id"])


def downgrade() -> None:
    web_tables = (
        "farm_rewards",
        "farm_plots",
        "farm_profiles",
        "telegram_auth_replays",
        "web_sessions",
    )
    for table in web_tables:
        if table in _tables():
            op.drop_table(table)
    for name in ("receipt_checksum", "receipt_file_unique_id", "review_note"):
        if name in _columns("payments"):
            op.drop_column("payments", name)
    for name in ("ranking_points", "farm_points", "bonus_balance_som"):
        if name in _columns("users"):
            op.drop_column("users", name)
