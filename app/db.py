import os
import datetime as dt
from typing import Optional

from sqlalchemy import create_engine, Integer, String, Float, DateTime, inspect, text, ForeignKey
from sqlalchemy.orm import declarative_base, Mapped, mapped_column, sessionmaker, relationship

def _normalize_db_url(url: str) -> str:
    if not url:
        return url
    if "+psycopg" in url or "+psycopg2" in url or "+asyncpg" in url:
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url

DATABASE_URL = _normalize_db_url(os.getenv("DATABASE_URL", ""))
if not DATABASE_URL:
    raise RuntimeError("Chýba DATABASE_URL")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# -------- Trades --------
class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    coin_id: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(120))

    invested_eur: Mapped[float] = mapped_column(Float)
    invested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())

    sold_eur: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sold_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # nákup a sledovanie (USD/EUR/FX)
    buy_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fx_eurusd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)      # kurz pri nákupe
    entry_price_eur: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    units: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    high_water_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # SL/TP návrhy (USD)
    sl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tp1_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tp2_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # pingy
    last_alert_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_heads_up_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_profit_ping_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_stale_ping_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

# -------- Signals history (na cooldown a späť) --------
class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: dt.datetime.utcnow())

    picks: Mapped[list["SignalPick"]] = relationship("SignalPick", back_populates="signal", cascade="all, delete-orphan")

class SignalPick(Base):
    __tablename__ = "signal_picks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(Integer, ForeignKey("signals.id", ondelete="CASCADE"))
    coin_id: Mapped[str] = mapped_column(String(100), index=True)
    symbol: Mapped[str] = mapped_column(String(24))
    score: Mapped[float] = mapped_column(Float)

    signal: Mapped[Signal] = relationship("Signal", back_populates="picks")

def _ensure_columns() -> None:
    insp = inspect(engine)
    if "trades" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("trades")}
    to_add = []
    add = lambda s: to_add.append("ADD COLUMN " + s)
    if "fx_eurusd" not in cols: add("fx_eurusd DOUBLE PRECISION NULL")
    if "entry_price_eur" not in cols: add("entry_price_eur DOUBLE PRECISION NULL")
    if "units" not in cols: add("units DOUBLE PRECISION NULL")
    if "sl_usd" not in cols: add("sl_usd DOUBLE PRECISION NULL")
    if "tp1_usd" not in cols: add("tp1_usd DOUBLE PRECISION NULL")
    if "tp2_usd" not in cols: add("tp2_usd DOUBLE PRECISION NULL")
    if "last_heads_up_at" not in cols: add("last_heads_up_at TIMESTAMP NULL")
    if "last_profit_ping_at" not in cols: add("last_profit_ping_at TIMESTAMP NULL")
    if "last_stale_ping_at" not in cols: add("last_stale_ping_at TIMESTAMP NULL")
    if to_add:
        with engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE trades {", ".join(to_add)}'))

def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
