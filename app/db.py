import os
import datetime as dt
from typing import Optional, List

from sqlalchemy import create_engine, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base, Mapped, mapped_column, sessionmaker

# ----------------------------------------------------------------------
# Pomocná funkcia: prepnúť URL na driver "psycopg" (v3)
# Render dáva typicky "postgres://..." alebo "postgresql://..."
# SQLAlchemy bez špecifikácie drivera skúsi psycopg2 -> chyba.
# ----------------------------------------------------------------------
def _normalize_db_url(url: str) -> str:
    if not url:
        return url
    # už špecifikovaný driver? nechaj tak
    if "+psycopg" in url or "+psycopg2" in url or "+asyncpg" in url:
        return url
    # postgres://  -> postgresql+psycopg://
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    # postgresql:// -> postgresql+psycopg://
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Chýba DATABASE_URL v prostredí (Render Postgres).")

DATABASE_URL = _normalize_db_url(DATABASE_URL)

# Engine + session
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

# ----------------------------------------------------------------------
# Model tabuľky "trades"
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# Inicializácia DB (vytvorenie tabuliek)
# ----------------------------------------------------------------------
def init_db() -> None:
    Base.metadata.create_all(bind=engine)
