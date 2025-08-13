import os
import logging
from typing import Optional, List

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# Dôležité: importujeme celý modul scheduler (nie len premenné/funkcie)
from . import scheduler as sched
from .services.notifier import send_email
from .services.coingecko import ping as cg_ping_api
from .db import SessionLocal, init_db, Trade

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="crypto-broker")

# Jinja2 šablóny pre /dashboard
templates = Jinja2Templates(directory="templates")


# ----------------------------
# Startup: DB + scheduler
# ----------------------------
@app.on_event("startup")
def _start_scheduler():
    init_db()  # inicializácia DB (Render Postgres)
    sch = sched.create_scheduler()
    sch.start()
    app.state.scheduler = sch
    logging.info("Scheduler started (cron: 07:30, 13:00, 22:00; TZ %s)", os.getenv("TZ", "Europe/Bratislava"))


# ----------------------------
# Základne info
# ----------------------------
@app.get("/")
def root():
    return {"status": "ok", "app": "crypto-broker", "scheduler": "running"}


# ----------------------------
# Posledný signál
# ----------------------------
@app.get("/signal")
def get_signal():
    if sched.LAST_SIGNAL is None:
        return {"ready": False, "message": "Zatiaľ nie je signál. Počkaj na plánovaný beh alebo použi /run-now."}
    return {
        "ready": True,
        "created_at": sched.LAST_SIGNAL.created_at,
        "regime": sched.LAST_SIGNAL.regime,
        "picks": [p.__dict__ for p in sched.LAST_SIGNAL.picks],
        "note": sched.LAST_SIGNAL.note,
    }


# ----------------------------
# Manuálne spustenie výpočtu
# ----------------------------
@app.get("/run-now")
async def run_now():
    await sched.job_30m()
    return {"ok": True}


# ----------------------------
# Test odoslania e-mailu
# ----------------------------
@app.get("/test-email")
def test_email():
    try:
        send_email(
            subject="Krypto Broker – test email",
            html="<h3>Fungujem ✅</h3><p>Toto je test z tvojej aplikácie na Renderi.</p>",
        )
        return {"ok": True, "sent_to": os.getenv("EMAIL_TO")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----------------------------
# CoinGecko ping (diagnostika)
# ----------------------------
@app.get("/cg-ping")
async def cg_ping_route():
    data = await cg_ping_api()
    return {"ok": True, "plan": os.getenv("COINGECKO_PLAN"), "resp": data}


# ----------------------------
# Dashboard (HTML)
# ----------------------------
@app.get("/dashboard")
def dashboard(request: Request):
    preselect = os.getenv("PRESELECT", "80")
    tz = os.getenv("TZ", "Europe/Bratislava")
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "preselect": preselect, "tz": tz},
    )


# ----------------------------
# PnL/Trades API
# ----------------------------
class TradeIn(BaseModel):
    coin_id: str
    symbol: str
    name: str
    invested_eur: float = Field(gt=0)
    note: Optional[str] = None


class TradeCloseIn(BaseModel):
    sold_eur: float = Field(gt=0)


def _to_dict_trade(t: Trade):
    d = {
        "id": t.id,
        "coin_id": t.coin_id,
        "symbol": t.symbol,
        "name": t.name,
        "invested_eur": t.invested_eur,
        "invested_at": t.invested_at.isoformat() + "Z",
        "sold_eur": t.sold_eur,
        "sold_at": t.sold_at.isoformat() + "Z" if t.sold_at else None,
        "note": t.note or "",
    }
    if t.sold_eur is not None:
        pnl = t.sold_eur - t.invested_eur
        roi = (t.sold_eur / t.invested_eur - 1.0) * 100.0 if t.invested_eur > 0 else None
    else:
        pnl = None
        roi = None
    d["pnl_eur"] = pnl
    d["roi_pct"] = roi
    return d


@app.post("/api/trades")
def create_trade(body: TradeIn):
    db: Session = SessionLocal()
    try:
        t = Trade(
            coin_id=body.coin_id,
            symbol=body.symbol,
            name=body.name,
            invested_eur=float(body.invested_eur),
            note=body.note or "",
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return {"ok": True, "id": t.id, "trade": _to_dict_trade(t)}
    finally:
        db.close()


@app.post("/api/trades/{tid}/close")
def close_trade(tid: int, body: TradeCloseIn):
    db: Session = SessionLocal()
    try:
        t = db.get(Trade, tid)
        if not t:
            return {"ok": False, "error": "Trade not found"}
        if t.sold_eur is not None:
            return {"ok": False, "error": "Trade already closed"}
        from datetime import datetime as _dt
        t.sold_eur = float(body.sold_eur)
        t.sold_at = _dt.utcnow()
        db.commit()
        db.refresh(t)
        return {"ok": True, "trade": _to_dict_trade(t)}
    finally:
        db.close()


@app.get("/api/trades")
def list_trades():
    db: Session = SessionLocal()
    try:
        rows: List[Trade] = db.query(Trade).order_by(Trade.invested_at.desc()).all()
        data = [_to_dict_trade(t) for t in rows]
        invested_total = sum(t.invested_eur for t in rows)
        realized_total = sum((t.sold_eur or 0.0) for t in rows if t.sold_eur is not None)
        realized_pnl = sum((t.sold_eur - t.invested_eur) for t in rows if t.sold_eur is not None)
        return {
            "ok": True,
            "items": data,
            "summary": {
                "invested_total_eur": invested_total,
                "realized_total_eur": realized_total,
                "realized_pnl_eur": realized_pnl,
                "closed_trades": sum(1 for t in rows if t.sold_eur is not None),
                "open_trades": sum(1 for t in rows if t.sold_eur is None),
            },
        }
    finally:
        db.close()
