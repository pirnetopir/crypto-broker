import os
import math
import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from .services.coingecko import (
    get_markets_top200_cached,
    fetch_many_hourly,
    get_simple_prices,
)
from .services.indicators import atr_from_closes, pct_change, ema, rsi
from .services.regime import regime_flag
from .services.scorer import compute_scores
from .services.notifier import send_email
from .services.signals import Pick, SignalPack
from .services.coinbase import get_coinbase_usd_symbols_cached
from .db import SessionLocal, Trade, Signal, SignalPick

_scheduler: Optional[AsyncIOScheduler] = None
LAST_SIGNAL: Optional[SignalPack] = None

STABLE_IDS = {
    "tether","usd-coin","dai","usdd","frax","first-digital-usd","paxos-standard","true-usd","paypal-usd","gemini-dollar","usdp"
}

def _envf(name: str, default: float) -> float:
    try: return float(os.getenv(name, str(default)))
    except: return float(default)

def _envi(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except: return int(default)

def _safe_send_email(subject: str, html: str) -> None:
    try: send_email(subject, html)
    except Exception as e:
        logging.warning("email send failed: %s", e)

def _enrich_from_prices(id_: str, prices: List[List[float]], vol24: float = 1.0) -> Optional[Dict]:
    closes = [p[1] for p in prices]
    if len(closes) < 200:
        return None
    close = closes[-1]
    mom_3h  = pct_change(close, closes[-4]) if len(closes) > 4 else 0.0
    mom_24h = pct_change(close, closes[-24]) if len(closes) > 24 else 0.0
    mom_7d  = pct_change(close, closes[-24*7]) if len(closes) > 24*7 else 0.0
    atr = atr_from_closes(closes, period=14)
    atr_pct = (atr[-1] / close) if close else 0.0
    em50 = ema(closes, 50)[-1]
    em100 = ema(closes, 100)[-1]
    rsi14 = rsi(closes, 14)[-1]
    symbol = id_[:6].upper(); name = id_
    return {
        "id": id_, "symbol": symbol, "name": name,
        "price": float(close), "vol24": float(vol24),
        "mom_3h": float(mom_3h), "mom_24h": float(mom_24h), "mom_7d": float(mom_7d),
        "atr_pct": float(atr_pct),
        "ema50": float(em50), "ema100": float(em100),
        "ema_above_50": 1 if close > em50 else 0,
        "ema_above_100": 1 if close > em100 else 0,
        "rsi": float(rsi14),
        "trend_flag": 1 if mom_7d > 0 else 0,
        # mini sparkline: posledných 50 close
        "spark": [float(x) for x in closes[-50:]],
    }

async def _persist_signal(picks: List[Dict]) -> None:
    db: Session = SessionLocal()
    try:
        s = Signal()
        db.add(s); db.flush()
        for p in picks:
            db.add(SignalPick(signal_id=s.id, coin_id=p["id"], symbol=p["symbol"], score=float(p["score"])))
        db.commit()
    except Exception as e:
        logging.warning("persist signal failed: %s", e)
    finally:
        db.close()

async def _cooldown_filter(rows: List[Dict]) -> List[Dict]:
    n = _envi("COOLDOWN_BEHS", 0)
    if n <= 0:
        return rows
    db: Session = SessionLocal()
    try:
        out: List[Dict] = []
        for r in rows:
            # pozri posledné n signálov pre coin
            q = db.query(SignalPick).filter(SignalPick.coin_id == r["id"]).order_by(SignalPick.id.desc()).limit(n)
            last = list(q)
            if len(last) < n:
                out.append(r); continue
            # ak bol v každom z posledných n a skóre klesá -> preskoč tento beh
            scores = [lp.score for lp in last]
            if all(isinstance(x, float) for x in scores) and r.get("score") is not None:
                if r["score"] < scores[0] and scores == sorted(scores, reverse=True):
                    # posledné skóre klesajúce: preskoč
                    continue
            out.append(r)
        return out
    except Exception:
        return rows
    finally:
        db.close()

async def _build_and_store_signal(rows: List[Dict], regime: int) -> None:
    global LAST_SIGNAL
    regime_text = "risk-on" if regime == 1 else "risk-off"

    atr_pct_max = _envf("ATR_PCT_MAX", 0.08)
    ema_filter = _envi("EMA_FILTER", 0)
    rsi_max = _envi("RSI_MAX", 80)

    # základné filtre
    filtered = []
    for r in rows:
        if r["atr_pct"] > atr_pct_max:
            continue
        if ema_filter in (50,100):
            if ema_filter == 50 and r.get("ema_above_50",0) == 0: continue
            if ema_filter == 100 and r.get("ema_above_100",0) == 0: continue
        if rsi_max > 0 and r.get("rsi",50.0) > rsi_max:
            continue
        filtered.append(r)

    weights = {
        "w1": _envf("W1", 0.20),
        "w2": _envf("W2", 0.25),
        "w3": _envf("W3", 0.15),
        "w4": _envf("W4", 0.20),
        "w5": _envf("W5", 0.10),
        "w6": _envf("W6", 0.10),
    }
    ranked = compute_scores(filtered, weights) if filtered else []
    ranked = await _cooldown_filter(ranked)

    top_k = _envi("PICK_TOP", 10)
    picks = ranked[:top_k]
    if picks:
        scores = [p["score"] for p in picks]
        m = max(scores)
        exps = [math.exp(s - m) for s in scores]
        ssum = sum(exps) or 1.0
        for i, p in enumerate(picks):
            p["weight"] = round(exps[i] / ssum, 3)

    # e-maily
    if regime == 0:
        _safe_send_email("Krypto Broker – RISK-OFF",
                         "<h3>Režim trhu: RISK-OFF ⚠️</h3><p>Odporúčanie: presun do stablecoinov (manuálne).</p>")
    else:
        rows_html = "".join([
            f"<tr><td>{p['symbol']}</td><td>{p['name']}</td>"
            f"<td>{p['price']:.6f}</td><td>{p['score']:.3f}</td>"
            f"<td>{p.get('weight',0.0):.3f}</td><td>{p['mom_24h']*100:.2f}%</td>"
            f"<td>{p['atr_pct']*100:.2f}%</td></tr>"
            for p in picks
        ])
        table = ("<table border='1' cellpadding='6' cellspacing='0'>"
                 "<tr><th>Symbol</th><th>Názov</th><th>Cena</th><th>Skóre</th>"
                 "<th>Váha</th><th>24h</th><th>ATR%</th></tr>" + rows_html + "</table>")
        _safe_send_email(f"Krypto Broker – TOP {top_k} – návrh nákupu",
                         f"<h3>TOP {top_k} – návrh nákupu</h3><p>Režim: {regime_text}</p>{table}")

    LAST_SIGNAL = SignalPack(
        created_at=datetime.utcnow().isoformat() + "Z",
        regime=regime_text,
        picks=[Pick(
            id=p["id"], symbol=p["symbol"], name=p["name"],
            price=float(p["price"]), score=float(p["score"]),
            weight=float(p.get("weight", 0.0)),
            mom_24h=float(p["mom_24h"]), atr_pct=float(p["atr_pct"]),
            spark=p.get("spark", []),
        ) for p in picks],
        note=("risk-off upozornenie poslalo iba varovanie" if regime == 0 else ""),
    )

    # ulož históriu pre cooldown/backtest
    try:
        await _persist_signal(picks)
    except Exception:
        pass

async def _select_and_score(use_fresh_markets: bool, coinbase_only: bool) -> None:
    logging.info("scan start (fresh_markets=%s, coinbase_only=%s)", use_fresh_markets, coinbase_only)
    ttl = 1 if use_fresh_markets else 1440
    markets = await get_markets_top200_cached("usd", ttl_minutes=ttl)
    reg = await regime_flag()

    if not markets:
        logging.warning("markets empty; skipping selection")
        await _build_and_store_signal([], reg)
        return

    # coinbase filter
    cb_symbols: Set[str] = set()
    if coinbase_only:
        try: cb_symbols = await get_coinbase_usd_symbols_cached(ttl_minutes=1440)
        except Exception: cb_symbols = set()

    min_vol = _envf("MIN_24H_VOLUME_USD", 10_000_000)
    rows: List[Dict] = []
    for m in markets:
        if m.get("id") in STABLE_IDS: continue
        symbol = (m.get("symbol") or "").upper()
        if coinbase_only and cb_symbols and symbol not in cb_symbols:
            continue
        vol24 = float(m.get("total_volume") or 0.0)
        if vol24 < min_vol: continue
        rows.append({
            "id": m.get("id"),
            "symbol": symbol,
            "name": m.get("name"),
            "price": float(m.get("current_price") or 0.0),
            "vol24": vol24,
        })

    preselect = _envi("PRESELECT", 80)
    rows.sort(key=lambda x: x["vol24"], reverse=True)
    pre = rows[:min(len(rows), preselect)]
    ids = [r["id"] for r in pre]
    charts = await fetch_many_hourly(ids, days=10)

    enriched: List[Dict] = []
    vol_by_id = {r["id"]: r["vol24"] for r in pre}
    for cid, data in charts.items():
        row = _enrich_from_prices(cid, data.get("prices", []), vol24=vol_by_id.get(cid, 1.0))
        if row: enriched.append(row)

    await _build_and_store_signal(enriched, reg)
    logging.info("scan done; enriched=%d", len(enriched))

# ---------- PUBLIC JOBS ----------
async def job_morning_scan() -> None:
    coinbase_only = os.getenv("COINBASE_ONLY", "0") == "1"
    await _select_and_score(use_fresh_markets=True, coinbase_only=coinbase_only)

async def job_noon_rescore() -> None:
    coinbase_only = os.getenv("COINBASE_ONLY", "0") == "1"
    await _select_and_score(use_fresh_markets=False, coinbase_only=coinbase_only)

async def job_evening_rescore() -> None:
    coinbase_only = os.getenv("COINBASE_ONLY", "0") == "1"
    await _select_and_score(use_fresh_markets=False, coinbase_only=coinbase_only)

# ---------- WATCHLIST (vylepšený) ----------
async def job_watch_open_positions() -> None:
    try:
        db: Session = SessionLocal()
        rows: List[Trade] = db.query(Trade).filter(Trade.sold_eur.is_(None)).all()
        open_ids = list({t.coin_id for t in rows if t.coin_id})
        if not open_ids:
            return
        prices = await get_simple_prices(open_ids, vs="usd")

        drop = _envf("ALERT_DROP_PCT", 0.08)
        heads_up = _envf("ALERT_HEADS_UP_PCT", 0.05)
        cooldown_h = _envi("ALERT_COOLDOWN_HOURS", 12)
        p_lock = _envf("PROFIT_LOCK_PCT", 0.15)
        stale_days = _envi("STALE_DAYS", 7)

        now = datetime.utcnow()
        changed = False

        for t in rows:
            cur = prices.get(t.coin_id)
            if cur is None:
                continue
            t.last_price_usd = cur
            if t.high_water_usd is None:
                t.high_water_usd = float(t.buy_price_usd or cur)
            if cur > float(t.high_water_usd or 0.0):
                t.high_water_usd = cur

            # heads-up / action
            hw = float(t.high_water_usd or cur)
            drawdown = (cur / hw) - 1.0 if hw else 0.0

            def can_send(ts):
                return ts is None or (now - ts) >= timedelta(hours=cooldown_h)

            if drawdown <= -heads_up and can_send(t.last_heads_up_at):
                _safe_send_email(
                    f"ℹ️ Heads-up {t.symbol}: -{abs(drawdown)*100:.2f}% od maxima",
                    f"<p>Aktuálna: {cur:.6f} USD · High-water: {hw:.6f} USD</p>"
                )
                t.last_heads_up_at = now; changed = True

            if drawdown <= -drop and can_send(t.last_alert_at):
                _safe_send_email(
                    f"⚠️ Action {t.symbol}: -{abs(drawdown)*100:.2f}% od maxima",
                    f"<p>Aktuálna: {cur:.6f} USD · High-water: {hw:.6f} USD<br/>Zváž manuálny predaj / posun do stablecoinov.</p>"
                )
                t.last_alert_at = now; changed = True

            # profit-lock ping (ak zisk výrazný)
            if t.buy_price_usd:
                gain = (cur / t.buy_price_usd) - 1.0
                if gain >= p_lock and can_send(t.last_profit_ping_at):
                    _safe_send_email(
                        f"✅ Profit {t.symbol}: +{gain*100:.2f}%",
                        f"<p>Navrhujem posunúť stop-loss (trailing, napr. podľa ATR) alebo vybrať časť zisku.</p>"
                    )
                    t.last_profit_ping_at = now; changed = True

            # stale ping (dlho nič)
            if (now - t.invested_at) >= timedelta(days=stale_days) and can_send(t.last_stale_ping_at):
                _safe_send_email(
                    f"⏳ Stále otvorené: {t.symbol}",
                    f"<p>Pozícia otvorená {t.invested_at.isoformat()}Z – zváž uvoľnenie kapitálu.</p>"
                )
                t.last_stale_ping_at = now; changed = True

        if changed:
            db.commit()
    except Exception as e:
        logging.exception("watchlist error: %s", e)
    finally:
        try: db.close()
        except: pass

# ---------- SCHEDULER ----------
def create_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        tz = os.getenv("TZ", "Europe/Bratislava")
        _scheduler = AsyncIOScheduler(timezone=tz)

        _scheduler.add_job(job_morning_scan, CronTrigger(hour=7, minute=30),
                           id="job_morning", replace_existing=True, max_instances=1, coalesce=True)
        _scheduler.add_job(job_noon_rescore, CronTrigger(hour=13, minute=0),
                           id="job_noon", replace_existing=True, max_instances=1, coalesce=True)
        _scheduler.add_job(job_evening_rescore, CronTrigger(hour=22, minute=0),
                           id="job_evening", replace_existing=True, max_instances=1, coalesce=True)
        _scheduler.add_job(job_watch_open_positions, CronTrigger(minute=5),
                           id="job_watch", replace_existing=True, max_instances=1, coalesce=True)
    return _scheduler
