import os, logging, math, asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .services.coingecko import get_markets_top200, fetch_many_hourly
from .services.indicators import atr_from_closes, pct_change, last_close
from .services.regime import regime_flag
from .services.scorer import compute_scores
from .services.notifier import send_email
from .services.signals import Pick, SignalPack

_scheduler: AsyncIOScheduler | None = None

# zdieľaný stav pre API
LAST_SIGNAL: SignalPack | None = None

def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)

def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)

async def job_30m():
    global LAST_SIGNAL
    try:
        logging.info("job_30m start")
        # 1) načítaj top200 trh
        markets = await get_markets_top200("usd")
        # vyhoď stablecoiny
        STABLE_IDS = {"tether", "usd-coin", "dai", "usdd", "frax"}
        rows = []
        for m in markets:
            if m.get("id") in STABLE_IDS:
                continue
            vol24 = float(m.get("total_volume") or 0.0)
            if vol24 < _env_float("MIN_24H_VOLUME_USD", 10_000_000):
                continue
            rows.append({
                "id": m.get("id"),
                "symbol": (m.get("symbol") or "").upper(),
                "name": m.get("name"),
                "price": float(m.get("current_price") or 0.0),
                "vol24": vol24,
            })

        # predvýber na analýzu (šetrenie API) – top N podľa objemu
        PRESELECT = min(len(rows), _env_int("PRESELECT", 60))
        rows.sort(key=lambda x: x["vol24"], reverse=True)
        pre = rows[:PRESELECT]
        ids = [r["id"] for r in pre]

        # 2) hodinové grafy na 10 dní pre ATR a momentum
        charts = await fetch_many_hourly(ids, days=10, concurrency=4)

        enriched = []
        for r in pre:
            prices = [p[1] for p in charts.get(r["id"], {}).get("prices", [])]
            if len(prices) < 200:  # málo dát, preskoč
                continue
            close = prices[-1]
            # momentum (aproximácia z hourly)
            mom_3h = pct_change(close, prices[-4]) if len(prices) > 4 else 0.0
            mom_24h = pct_change(close, prices[-24]) if len(prices) > 24 else 0.0
            mom_7d = pct_change(close, prices[-24*7]) if len(prices) > 24*7 else 0.0
            # ATR
            atr = atr_from_closes(prices, period=14)
            atr_pct = (atr[-1] / close) if close else 0.0
            # trend flag – zjednodušene: pozitívny 7d moment
            trend_flag = 1 if mom_7d > 0 else 0

            enriched.append({
                **r,
                "price": close,
                "mom_3h": mom_3h,
                "mom_24h": mom_24h,
                "mom_7d": mom_7d,
                "atr_pct": atr_pct,
                "trend_flag": trend_flag,
            })

        # 3) režim trhu
        regime = await regime_flag()  # 1=risk-on, 0=risk-off
        regime_text = "risk-on" if regime == 1 else "risk-off"

        # 4) filtre + skóre
        ATR_PCT_MAX = _env_float("ATR_PCT_MAX", 0.08)  # 8% default
        filtered = [r for r in enriched if r["atr_pct"] <= ATR_PCT_MAX]

        weights = {
            "w1": _env_float("W1", 0.20),
            "w2": _env_float("W2", 0.25),
            "w3": _env_float("W3", 0.15),
            "w4": _env_float("W4", 0.20),
            "w5": _env_float("W5", 0.10),
            "w6": _env_float("W6", 0.10),
        }
        ranked = compute_scores(filtered, weights)
        top_k = _env_int("PICK_TOP", 4)
        picks = ranked[:top_k]

        # 5) priprav váhy (softmax zo skóre)
        import math
        if picks:
            scores = [p["score"] for p in picks]
            exps = [math.exp(s - max(scores)) for s in scores]
            ssum = sum(exps)
            for i, p in enumerate(picks):
                p["weight"] = round(exps[i] / ssum, 3)
        else:
            picks = []

        # 6) ak risk-off → pošli len upozornenie
        if regime == 0:
            html = f"<h3>Režim trhu: RISK-OFF ⚠️</h3><p>Odporúčanie: presun do stablecoinov (manuálne).</p>"
            send_email("Krypto Broker – RISK-OFF", html)
        else:
            # 7) pošli BUY odporúčanie (na schválenie)
            rows_html = "".join([
                f"<tr><td>{p['symbol']}</td><td>{p['name']}</td>"
                f"<td>{p['price']:.4f}</td><td>{p['score']:.3f}</td>"
                f"<td>{p['weight']:.3f}</td><td>{p['mom_24h']*100:.2f}%</td>"
                f"<td>{p['atr_pct']*100:.2f}%</td></tr>"
                for p in picks
            ])
            table = (
                "<table border='1' cellpadding='6' cellspacing='0'>"
                "<tr><th>Symbol</th><th>Názov</th><th>Cena</th><th>Skóre</th>"
                "<th>Váha</th><th>24h</th><th>ATR%</th></tr>" + rows_html + "</table>"
            )
            html = f"<h3>TOP {top_k} – návrh nákupu (schválenie manuálne)</h3><p>Režim: {regime_text}</p>{table}"
            send_email("Krypto Broker – TOP výber", html)

        # 8) ulož posledný signál do pamäte (na API)
        LAST_SIGNAL = SignalPack(
            created_at=datetime.utcnow().isoformat() + "Z",
            regime=regime_text,
            picks=[
                Pick(
                    id=p["id"],
                    symbol=p["symbol"],
                    name=p["name"],
                    price=float(p["price"]),
                    score=float(p["score"]),
                    weight=float(p.get("weight", 0.0)),
                    mom_24h=float(p["mom_24h"]),
                    atr_pct=float(p["atr_pct"]),
                )
                for p in picks
            ],
            note="risk-off upozornenie poslalo iba varovanie" if regime == 0 else "",
        )
        logging.info("job_30m done; regime=%s; picks=%d", regime_text, len(picks))
    except Exception as e:
        logging.exception("job_30m error: %s", e)

def create_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        tz = os.getenv("TZ", "UTC")
        minutes = int(os.getenv("REFRESH_MINUTES", "30"))
        _scheduler = AsyncIOScheduler(timezone=tz)
        _scheduler.add_job(
            job_30m,
            IntervalTrigger(minutes=minutes),
            id="job_30m",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return _scheduler
