"""Database layer (requirement: database for persistence).

The connection string comes from an environment variable (12-Factor rule III:
config lives in the environment, not in the code). On Render, DATABASE_URL is set
in the web service's environment. Locally, if no URL is set, we fall
back to a SQLite file so the app can run without any cloud account.

Three tables:
  holdings           -> YOUR PORTFOLIO, entered by you (the CRUD entity)
  prices             -> MARKET DATA, latest price per stock, fetched from Finnhub
  portfolio_reports  -> one summary row per run of the daily background job
"""

import os

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    func,
)
from sqlalchemy.pool import NullPool

metadata = MetaData()

holdings = Table(
    "holdings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ticker", String(10), nullable=False),          # text, one of the Mag 7
    Column("company_name", String(100), nullable=False),   # filled in automatically from the ticker
    Column("shares", Integer, nullable=False),             # integer
    Column("buy_price", Numeric(12, 2), nullable=False),   # decimal
    Column("buy_date", Date, nullable=False),              # date
    Column("is_active", Boolean, nullable=False, default=True),  # boolean
    Column("notes", Text, nullable=True),
    Column("attachment_url", Text, nullable=True),         # path of the file in storage (/files/...)
    Column("attachment_name", String(255), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

prices = Table(
    "prices",
    metadata,
    Column("symbol", String(10), primary_key=True),
    Column("price", Numeric(12, 4), nullable=False),        # current / last price
    Column("change", Numeric(12, 4), nullable=True),        # vs previous close
    Column("change_pct", Numeric(8, 4), nullable=True),
    Column("prev_close", Numeric(12, 4), nullable=True),
    Column("fetched_at", DateTime(timezone=True), nullable=False),  # when our job fetched it
)

reports = Table(
    "portfolio_reports",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("trigger", String(20), nullable=False),          # "cron" or "manual"
    Column("prices_status", String(200), nullable=True),    # e.g. "8/8 updated" or the error
    Column("holdings_count", Integer, nullable=False),      # active positions
    Column("total_cost", Numeric(16, 2), nullable=False),
    Column("market_value", Numeric(16, 2), nullable=True),
    Column("pnl", Numeric(16, 2), nullable=True),
    Column("pnl_pct", Numeric(8, 2), nullable=True),
    Column("spy_change_pct", Numeric(8, 2), nullable=True),
    Column("report_url", Text, nullable=True),              # path of the CSV file in storage (/files/...)
)


def _database_url() -> str:
    url = (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or "sqlite:///./local.db"
    )
    # Neon gives "postgres://" or "postgresql://"; SQLAlchemy needs to know
    # which driver to use (psycopg version 3).
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


_engine = None


def get_engine():
    """Create the engine once per process and make sure the tables exist."""
    global _engine
    if _engine is None:
        url = _database_url()
        if url.startswith("sqlite"):
            _engine = create_engine(url, connect_args={"check_same_thread": False})
        else:
            # Serverless functions start and stop often, so we don't keep a
            # pool of idle connections; Neon's own pooler handles that.
            _engine = create_engine(url, poolclass=NullPool)
        metadata.create_all(_engine)
    return _engine

