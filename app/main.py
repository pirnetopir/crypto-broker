import os
import logging
from typing import Optional, List

from fastapi import FastAPI, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import scheduler as sched
from .services.notifier import send_email
from .services.coingecko import ping as cg_ping_api
from .db import SessionLocal, init_db, Trade

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="crypto-broker")
templates = Jinja2Templates(directory="templates")

def _envi(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except: return int(default)

def _envf(name: str, default: float) -> float:
    try: return float(os.getenv(name, str(default)))
    except: return float(default)

@app.on_event("startup")
def _start_scheduler():
    init_db()
    sch = sched.create_scheduler()
    sch.start()
    app.state.scheduler = sch
    logging.info("Scheduler started (cron: 07:30, 13:00, 22:00; TZ %s)", os.getenv("TZ", "Europe/Bratislava"))

@app.get("/")
def root():
    return {"status": "ok", "app": "crypto-broker", "scheduler": "running"}

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

@app.get("/run-now")
async def run_now():
    await sched.job_morning_scan()
    return {"ok": True}

@app.get("/test-email")
def test_email():
    try:
        send_email("Krypto Broker – test email", "<h3>Fungujem ✅</h3><p>Test z tvojej aplikácie.</p>")
        return {"ok": True, "sent_to": os.getenv("EMAIL_TO")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/cg-ping")
async def cg_ping_route():
    data = await cg_ping_api()
    return {"ok": True, "plan": os.getenv("COINGECKO_PLAN"), "resp": data}

@app.get("/dashboard")
def dashboard(request: Request):
    preselect = os.getenv("PRESELECT", "80")
    tz = os.getenv("TZ", "Europe/Bratislava")
    return templates.TemplateResponse("dashboard.html", {"request": request, "preselect": preselect, "tz": tz})

# -------- Trades API --------
class TradeIn(BaseModel):
    coin_id: str
    symbol: str
    name: str
    invested_eur: float = Field(gt=0)
    buy_price_usd: Optional[float] = None
    sl_usd: Optional[float] = None
    tp1_usd: Optional[float] = None
    tp2_usd: Optional[float] = None
    note: Optional[str] = None

class TradeCloseIn(BaseModel):
    sold_eur: float = Field(gt=0)

class BatchInvestIn(BaseModel):
    items: List[TradeIn]

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
        "buy_price_usd": t.buy_price_usd,
        "fx_eurusd": t.fx_eurusd,
        "entry_price_eur": t.entry_price_eur,
        "units": t.units,
        "high_water_usd": t.high_water_usd,
        "last_price_usd": t.last_price_usd,
        "sl_usd": t.sl_usd,
        "tp1_usd": t.tp1_usd,
        "tp2_usd": t.tp2_usd,
    }
    # realized
    if t.sold_eur is not None:
        pnl = t.sold_eur - t.invested_eur
        roi = (t.sold_eur / t.invested_eur - 1.0) * 100.0 if t.invested_eur > 0 else None
    else:
        pnl = None; roi = None
    d["pnl_eur"] = pnl; d["roi_pct"] = roi

    # unrealized (ak otvorená)
    if t.sold_eur is None and t.units and t.last_price_usd and t.fx_eurusd:
        curr_eur = (t.units * t.last_price_usd) / t.fx_eurusd
        upnl = curr_eur - t.invested_eur
        uroi = (curr_eur / t.invested_eur - 1.0) * 100.0 if t.invested_eur > 0 else None
        d["unrealized_pnl_eur"] = upnl
        d["unrealized_roi_pct"] = uroi
    else:
        d["unrealized_pnl_eur"] = None
        d["unrealized_roi_pct"] = None
    return d

def _risk_checks(db: Session, add_amount_eur: float) -> Optional[str]:
    total_cap = _envf("TOTAL_CAPITAL_EUR", 1000.0)
    per_coin_max = _envf("PER_COIN_MAX_PCT", 0.35)
    max_open = _envi("MAX_OPEN_POS", 5)

    open_trades = db.query(Trade).filter(Trade.sold_eur.is_(None)).all()
    if len(open_trades) >= max_open:
        return f"Dosiahnutý limit otvorených pozícií ({max_open})."

    invested_open = sum(t.invested_eur for t in open_trades)
    if invested_open + add_amount_eur > total_cap:
        return f"Investícia presahuje kapitál ({total_cap} EUR)."

    # per-coin check v create_trade (lebo nevieme symbol tu)
    return None

@app.post("/api/trades")
def create_trade(body: TradeIn):
    db: Session = SessionLocal()
    try:
        err = _risk_checks(db, body.invested_eur)
        if err: return {"ok": False, "error": err}

        FX = _envf("FX_EURUSD", 1.10)
        # nájdi posledný pick kvôli ATR-odhadom, ak nemáme SL/TP v tele
        atr_pct = None
        if sched.LAST_SIGNAL:
            for p in sched.LAST_SIGNAL.picks:
                if p.id == body.coin_id:
                    atr_pct = p.atr_pct
                    if body.buy_price_usd is None:
                        body.buy_price_usd = p.price
                    break

        t = Trade(
            coin_id=body.coin_id, symbol=body.symbol, name=body.name,
            invested_eur=float(body.invested_eur), note=body.note or "",
            buy_price_usd=float(body.buy_price_usd) if body.buy_price_usd is not None else None,
            fx_eurusd=FX,
        )
        # units & entry EUR
        if t.buy_price_usd:
            t.entry_price_eur = t.buy_price_usd / FX
            t.units = (t.invested_eur * FX) / t.buy_price_usd
            t.high_water_usd = t.buy_price_usd
            t.last_price_usd = t.buy_price_usd

        # SL/TP návrhy (ak chýbajú)
        slm = _envf("ATR_SL_MULT", 1.5)
        tp1m = _envf("ATR_TP1_MULT", 2.0)
        tp2m = _envf("ATR_TP2_MULT", 3.0)
        if t.buy_price_usd and atr_pct:
            if body.sl_usd is None:
                t.sl_usd = t.buy_price_usd * (1.0 - slm * atr_pct)
            if body.tp1_usd is None:
                t.tp1_usd = t.buy_price_usd * (1.0 + tp1m * atr_pct)
            if body.tp2_usd is None:
                t.tp2_usd = t.buy_price_usd * (1.0 + tp2m * atr_pct)
        else:
            t.sl_usd = body.sl_usd
            t.tp1_usd = body.tp1_usd
            t.tp2_usd = body.tp2_usd

        # per-coin limit
        if t.invested_eur and t.entry_price_eur:
            per_coin_limit = _envf("PER_COIN_MAX_PCT", 0.35) * _envf("TOTAL_CAPITAL_EUR", 1000.0)
            if t.invested_eur > per_coin_limit:
                return {"ok": False, "error": f"Max na coin je {per_coin_limit:.2f} € (PER_COIN_MAX_PCT)."}

        db.add(t); db.commit(); db.refresh(t)
        return {"ok": True, "id": t.id, "trade": _to_dict_trade(t)}
    finally:
        db.close()

@app.post("/api/trades/batch")
def batch_trades(body: BatchInvestIn):
    db: Session = SessionLocal()
    try:
        total = sum(item.invested_eur for item in body.items)
        err = _risk_checks(db, total)
        if err: return {"ok": False, "error": err}

        res = []
        for item in body.items:
            # per-coin limit
            per_coin_limit = _envf("PER_COIN_MAX_PCT", 0.35) * _envf("TOTAL_CAPITAL_EUR", 1000.0)
            if item.invested_eur > per_coin_limit:
                return {"ok": False, "error": f"{item.symbol}: max na coin {per_coin_limit:.2f} € (PER_COIN_MAX_PCT)."}
            r = create_trade(item)  # použijeme rovnakú logiku
            if not r.get("ok"):
                return r
            res.append(r["trade"])
        return {"ok": True, "items": res}
    finally:
        db.close()

@app.post("/api/trades/{tid}/close")
def close_trade(tid: int, body: TradeCloseIn):
    db: Session = SessionLocal()
    try:
        t = db.get(Trade, tid)
        if not t: return {"ok": False, "error": "Trade not found"}
        if t.sold_eur is not None: return {"ok": False, "error": "Trade already closed"}
        from datetime import datetime as _dt
        t.sold_eur = float(body.sold_eur); t.sold_at = _dt.utcnow()
        db.commit(); db.refresh(t)
        return {"ok": True, "trade": _to_dict_trade(t)}
    finally:
        db.close()

@app.delete("/api/trades/{tid}")
def delete_trade(tid: int):
    db: Session = SessionLocal()
    try:
        t = db.get(Trade, tid)
        if not t: return {"ok": False, "error": "Trade not found"}
        if t.sold_eur is not None:
            return {"ok": False, "error": "Trade already closed – delete not allowed"}
        db.delete(t); db.commit()
        return {"ok": True}
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

@app.get("/api/trades.csv")
def export_trades_csv():
    db: Session = SessionLocal()
    try:
        rows: List[Trade] = db.query(Trade).order_by(Trade.invested_at.desc()).all()
        lines = ["id,symbol,name,invested_eur,invested_at,sold_eur,sold_at,units,buy_price_usd,fx_eurusd,entry_price_eur,sl_usd,tp1_usd,tp2_usd,note"]
        for t in rows:
            def esc(x):
                if x is None: return ""
                s = str(x).replace('"','""')
                return f'"{s}"'
            lines.append(",".join([
                esc(t.id), esc(t.symbol), esc(t.name), esc(t.invested_eur), esc(t.invested_at),
                esc(t.sold_eur), esc(t.sold_at), esc(t.units), esc(t.buy_price_usd),
                esc(t.fx_eurusd), esc(t.entry_price_eur), esc(t.sl_usd), esc(t.tp1_usd), esc(t.tp2_usd), esc(t.note),
            ]))
        csv = "\n".join(lines)
        return Response(content=csv, media_type="text/csv")
    finally:
        db.close()
