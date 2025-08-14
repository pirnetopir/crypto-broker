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
from .services.indicators import atr_from_closes, pct_change
from .services.regime import regime_flag
from .services.scorer import compute_scores
from .services.notifier import send_email
from .services.signals import Pick, SignalPack
from .services.coinbase import get_coinbase_usd_symbols_cached
from .db import SessionLocal, Trade

_scheduler: Optional[AsyncIOScheduler] = None
LAST_SIGNAL: Optional[SignalPack] = None

STABLE_IDS = {
    "tether", "usd-coin", "dai", "usdd", "frax",
    "first-digital-usd", "paxos-standard", "true-usd",
    "paypal-usd", "gemini-dollar", "usdp"
}

def _env_float(name: str, default: float) -> float:
    try: return float(os.getenv(name, str(default)))
    except Exception: return float(default)

def _env_int(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except Exception: return int(default)

def _safe_send_email(subject: str, html: str) -> None:
    try: send_email(subject, html)
    except Exception as e:
        logging.warning("email send failed: %s", e)

def _enrich_from_prices(id_: str, prices: List[List[float]], vol24: float = 1.0) -> Optional[Dict]:
    closes = [p[1] for p in prices]
    if len(closes) < 200: return None
    close = closes[-1]
    mom_3h  = pct_change(close, closes[-4]) if len(closes) > 4 else 0.0
    mom_24h = pct_change(close, closes[-24]) if len(closes) > 24 else 0.0
    mom_7d  = pct_change(close, closes[-24*7]) if len(closes) > 24*7 else 0.0
    atr = atr_from_closes(closes, period=14)
    atr_pct = (atr[-1] / close) if close else 0.0
    trend_flag = 1 if mom_7d > 0 else 0
    symbol = id_[:6].upper(); name = id_
    return {
        "id": id_, "symbol": symbol, "name": name,
        "price": float(close), "vol24": float(vol24),
        "mom_3h": float(mom_3h), "mom_24h": float(mom_24h),
        "mom_7d": float(mom_7d), "atr_pct": float(atr_pct),
        "trend_flag": int(trend_flag),
    }

async def _build_and_store_signal(rows: List[Dict], regime: int) -> None:
    global LAST_SIGNAL
    regime_text = "risk-on" if regime == 1 else "risk-off"
    atr_pct_max = _env_float("ATR_PCT_MAX", 0.08)
    filtered = [r for r in rows if r["atr_pct"] <= atr_pct_max]
    weights = {
        "w1": _env_float("W1", 0.20),
        "w2": _env_float("W2", 0.25),
        "w3": _env_float("W3", 0.15),
        "w4": _env_float("W4", 0.20),
        "w5": _env_float("W5", 0.10),
        "w6": _env_float("W6", 0.10),
    }
    ranked = compute_scores(filtered, weights) if filtered else []
    top_k = _env_int("PICK_TOP", 10)
    picks = ranked[:top_k]
    if picks:
        scores = [p["score"] for p in picks]
        m = max(scores)
        exps = [math.exp(s - m) for s in scores]
        ssum = sum(exps) or 1.0
        for i, p in enumerate(picks):
            p["weight"] = round(exps[i] / ssum, 3)

    if regime == 0:
        _safe_send_email("Krypto Broker – RISK-OFF",
                         "<h3>Režim trhu: RISK-OFF ⚠️</h3><p>Odporúčanie: presun do stablecoinov (manuálne).</p>")
    else:
        rows_html = "".join([
            f"<tr><td>{p['symbol']}</td><td>{p['name']}</td>"
            f"<td>{p['price']:.6f}</td><td>{p['score']:.3f}</td>"
            f"<td>{p.get('weight', 0.0):.3f}</td><td>{p['mom_24h']*100:.2f}%</td>"
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
        ) for p in picks],
        note="risk-off upozornenie poslalo iba varovanie" if regime == 0 else "",
    )

async def _select_and_score(use_fresh_markets: bool, coinbase_only: bool) -> None:
    """
    use_fresh_markets=True -> ráno: aktualizuj TOP200 (TTL~1 min)
    use_fresh_markets=False -> obed/večer: použi cache (TTL~24h), neťahaj veľký zoznam
    """
    logging.info("scan start (fresh_markets=%s, coinbase_only=%s)", use_fresh_markets, coinbase_only)
    ttl = 1 if use_fresh_markets else 1440  # minúty
    markets = await get_markets_top200_cached("usd", ttl_minutes=ttl)
    reg = await regime_flag()

    rows: List[Dict] = []
    if markets:
        cb_symbols: Set[str] = set()
        if coinbase_only:
            try:
                cb_symbols = await get_coinbase_usd_symbols_cached(ttl_minutes=1440)
            except Exception:
                cb_symbols = set()

        min_vol = _env_float("MIN_24H_VOLUME_USD", 10_000_000)
        for m in markets:
            if m.get("id") in STABLE_IDS:
                continue
            symbol = (m.get("symbol") or "").upper()
            if coinbase_only and cb_symbols and symbol not in cb_symbols:
                continue
            vol24 = float(m.get("total_volume") or 0.0)
            if vol24 < min_vol:
                continue
            rows.append({
                "id": m.get("id"),
                "symbol": symbol,
                "name": m.get("name"),
                "price": float(m.get("current_price") or 0.0),
                "vol24": vol24,
            })

        preselect = _env_int("PRESELECT", 80)
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
        return

    # Fallback: nič v markets (výpadok)
    logging.warning("markets empty; skipping selection this run.")
    await _build_and_store_signal([], reg)

# --------- PUBLIC JOBS ----------

async def job_morning_scan() -> None:
    coinbase_only = os.getenv("COINBASE_ONLY", "0") == "1"
    await _select_and_score(use_fresh_markets=True, coinbase_only=coinbase_only)

async def job_noon_rescore() -> None:
    coinbase_only = os.getenv("COINBASE_ONLY", "0") == "1"
    await _select_and_score(use_fresh_markets=False, coinbase_only=coinbase_only)

async def job_evening_rescore() -> None:
    coinbase_only = os.getenv("COINBASE_ONLY", "0") == "1"
    await _select_and_score(use_fresh_markets=False, coinbase_only=coinbase_only)

# --------- WATCHLIST (otvorené obchody) ----------

async def job_watch_open_positions() -> None:
    """
    Každú hodinu:
      - načíta otvorené obchody,
      - dotiahne current USD price cez /simple/price,
      - aktualizuje high_water_usd,
      - ak drop od high_water <= -ALERT_DROP_PCT → pošli e-mail (s cooldownom).
    """
    try:
        db: Session = SessionLocal()
        rows: List[Trade] = db.query(Trade).filter(Trade.sold_eur.is_(None)).all()
        open_ids = list({t.coin_id for t in rows if t.coin_id})
        if not open_ids:
            return

        prices = await get_simple_prices(open_ids, vs="usd")
        drop_pct = _env_float("ALERT_DROP_PCT", 0.08)  # 8 %
        cooldown_h = _env_int("ALERT_COOLDOWN_HOURS", 12)

        alerted_any = False
        now = datetime.utcnow()

        for t in rows:
            cur = prices.get(t.coin_id)
            if cur is None:
                continue
            t.last_price_usd = cur

            # inicializácia high-water pri prvom vstupe
            if t.high_water_usd is None:
                # ak poznáme buy_price_usd, z neho; inak z current
                t.high_water_usd = float(t.buy_price_usd or cur)

            # update high-water
            if cur > float(t.high_water_usd or 0.0):
                t.high_water_usd = cur

            # drop od high-water
            if t.high_water_usd:
                drawdown = (cur / t.high_water_usd) - 1.0
            else:
                drawdown = 0.0

            should_alert = drawdown <= -drop_pct
            if should_alert:
                # cooldown
                if t.last_alert_at is None or (now - t.last_alert_at) >= timedelta(hours=cooldown_h):
                    subj = f"⚠️ Krypto Broker – prudký pokles {t.symbol}"
                    html = (
                        f"<h3>{t.symbol} – pokles od maxima o {abs(drawdown)*100:.2f}%</h3>"
                        f"<p>Aktuálna cena: {cur:.6f} USD<br>"
                        f"High-water: {float(t.high_water_usd):.6f} USD<br>"
                        f"Nákupná cena: {float(t.buy_price_usd or 0.0):.6f} USD</p>"
                        f"<p>Odporúčanie: zváž manuálny predaj / posun do stablecoinov.</p>"
                    )
                    _safe_send_email(subj, html)
                    t.last_alert_at = now
                    alerted_any = True

        db.commit()
        if alerted_any:
            logging.info("watchlist: alerts sent.")
    except Exception as e:
        logging.exception("watchlist error: %s", e)
    finally:
        try: db.close()
        except Exception: pass

# --------- SCHEDULER ----------

def create_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        tz = os.getenv("TZ", "Europe/Bratislava")
        _scheduler = AsyncIOScheduler(timezone=tz)

        # ráno: veľký sken (TOP200, voliteľne len Coinbase)
        _scheduler.add_job(job_morning_scan, CronTrigger(hour=7, minute=30),
                           id="job_morning", replace_existing=True, max_instances=1, coalesce=True)
        # obed + večer: len re-score z cache (neťaháme TOP list)
        _scheduler.add_job(job_noon_rescore, CronTrigger(hour=13, minute=0),
                           id="job_noon", replace_existing=True, max_instances=1, coalesce=True)
        _scheduler.add_job(job_evening_rescore, CronTrigger(hour=22, minute=0),
                           id="job_evening", replace_existing=True, max_instances=1, coalesce=True)

        # watchlist: každú hodinu v :05
        _scheduler.add_job(job_watch_open_positions, CronTrigger(minute=5),
                           id="job_watch", replace_existing=True, max_instances=1, coalesce=True)
    return _scheduler
