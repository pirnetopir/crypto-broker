import os
import datetime as dt
from typing import Optional

from sqlalchemy import create_engine, Integer, String, Float, DateTime, inspect, text, Boolean
from sqlalchemy.orm import declarative_base, Mapped, mapped_column, sessionmaker

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
    raise RuntimeError("Chýba DATABASE_URL v prostredí (Render Postgres).")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

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

    # NOVÉ: nákupná cena v USD (pre watchlist), priebežné ceny a alerty
    buy_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high_water_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # najvyššia cena od nákupu
    last_price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_alert_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

def _ensure_columns() -> None:
    insp = inspect(engine)
    if "trades" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("trades")}
    to_add = []
    if "buy_price_usd" not in cols:
        to_add.append("ADD COLUMN buy_price_usd DOUBLE PRECISION NULL")
    if "high_water_usd" not in cols:
        to_add.append("ADD COLUMN high_water_usd DOUBLE PRECISION NULL")
    if "last_price_usd" not in cols:
        to_add.append("ADD COLUMN last_price_usd DOUBLE PRECISION NULL")
    if "last_alert_at" not in cols:
        to_add.append("ADD COLUMN last_alert_at TIMESTAMP NULL")
    if to_add:
        with engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE trades {", ".join(to_add)}'))

def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
