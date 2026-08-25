"""Initial schema — all tables from v1.0.0 ORM models.

Revision ID: 001
Revises: None
Create Date: 2026-07-02 00:00:00.000000

This migration creates the full set of tables that the ORM models
define at v1.0.0 release.  If you are starting from an existing
database created via ``create_all`` (dev bootstrap), run::

    alembic stamp 001

to mark the baseline without re-running DDL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── orders ────────────────────────────────────────────────────────────────
    # Matches src/persistence/orders.py :: OrderORM
    op.create_table(
        "orders",
        sa.Column("order_id", sa.String(64), primary_key=True),
        sa.Column("client_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("exchange_order_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("order_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(20, 8), server_default="0"),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("stop_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("average_fill_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("total_fee", sa.Numeric(20, 8), server_default="0"),
        sa.Column("reduce_only", sa.Boolean, server_default="false"),
        sa.Column("signal_id", sa.String(64), nullable=True),
        sa.Column("strategy_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])

    # ── trades ────────────────────────────────────────────────────────────────
    # Matches src/persistence/trades.py :: TradeORM
    op.create_table(
        "trades",
        sa.Column("trade_id", sa.String(64), primary_key=True),
        sa.Column("position_id", sa.String(64), nullable=False, index=True),
        sa.Column("signal_id", sa.String(64), nullable=True),
        sa.Column("strategy_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("exit_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("gross_pnl", sa.Numeric(20, 8), nullable=False),
        sa.Column("fees", sa.Numeric(20, 8), nullable=False),
        sa.Column("funding_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("initial_risk", sa.Numeric(20, 8), nullable=True),
        sa.Column("leverage", sa.Integer, server_default="1"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(15, 2), server_default="0"),
    )
    op.create_index("ix_trades_symbol", "trades", ["symbol"])
    op.create_index("ix_trades_opened_at", "trades", ["opened_at"])
    op.create_index("ix_trades_strategy_id", "trades", ["strategy_id"])

    # ── signals ───────────────────────────────────────────────────────────────
    # Matches src/persistence/signals.py :: SignalORM
    op.create_table(
        "signals",
        sa.Column("signal_id", sa.String(64), primary_key=True),
        sa.Column("strategy_id", sa.String(64), nullable=False, index=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("strength", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=False),
        sa.Column("take_profit", sa.Numeric(20, 8), nullable=False),
        sa.Column("atr", sa.Numeric(20, 8), nullable=True),
        sa.Column("market_structure", sa.String(20), server_default=""),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_signals_strategy_id", "signals", ["strategy_id"])
    op.create_index("ix_signals_symbol_generated", "signals", ["symbol", "generated_at"])

    # ── metric_snapshots ──────────────────────────────────────────────────────
    # Matches src/persistence/metrics.py :: MetricSnapshotORM
    op.create_table(
        "metric_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False, index=True),
        sa.Column("equity", sa.Numeric(20, 8), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("total_pnl", sa.Numeric(20, 8), server_default="0"),
        sa.Column("drawdown_pct", sa.Float, server_default="0"),
        sa.Column("win_rate", sa.Float, server_default="0"),
        sa.Column("open_positions", sa.Integer, server_default="0"),
        sa.Column("total_trades", sa.Integer, server_default="0"),
    )
    op.create_index("ix_metrics_timestamp", "metric_snapshots", ["snapshot_at"])


def downgrade() -> None:
    op.drop_table("metric_snapshots")
    op.drop_table("signals")
    op.drop_table("trades")
    op.drop_table("orders")
