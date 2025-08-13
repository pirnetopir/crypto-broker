import os, logging, math, asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .services.coingecko import get_markets_top200_cached, fetch_many_hourly
from .services.indicators import atr_from_closes, pct_change
from .services.regime import regime_flag
from .services.scorer import compute_scores
from .services.notifier import send_email
from .services.signals import Pick, SignalPack

# jediný scheduler v procese
_scheduler: AsyncIOScheduler | None = None
LAST_SIGNAL: SignalPack | None = None

DEFAULT_BOOTSTRAP_IDS = os.getenv(
    "BOOTSTRAP_IDS",
    "bitcoin,ethereum,binancecoin,solana,ripple,cardano,dogecoin,tron,polkadot,chainlink,litecoin,uniswap,avalanche-2"
).split(",")

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

def _safe_send_email(subject: str, html: str):
    try:
        send_email(subject, html)
    except Exception as e:
        logging.warning("email send failed: %s", e)

def _enrich_from_prices(id_: str, prices: list[list[float]], vol24: float = 1.0):
    """Vyrobí jeden riadok metrík z hourly cien; ak dát je málo, vráti None."""
    closes = [p[1] for p in prices]
    if len(closes) < 200:
        return None
    close = closes[-1]
    mom_3h = pct_change(close, closes[-4]) if len(closes) > 4 else 0.0
    mom_24h = pct_change(close, closes[-24]) if len(closes) > 24 else 0.0
    mom_7d = pct_change(close, closes[-24*7]) if len(closes) > 24*7 else 0.0
    atr = atr_from_closes(closes, period=14)
    atr_pct = (atr[-1] / close) if close else 0.0
    trend_flag = 1 if mom_7d > 0 else 0
    symbol = id_[:6].upper()  # fallback symbol (bez ďalšieho volania)
    name = id_
    return {
        "id": id_,
        "symbol": symbol,
        "name": name,
        "price": close,
        "vol24": vol24,
        "mom_3h": mom_3h,
        "mom_24h": mom_24h,
        "mom_7d": mom_7d,
        "atr_pct": atr_pct,
        "trend_flag": trend_flag,
    }

async def _build_and_notify(rows: list[dict], regime: int):
    """Zoradí, vyberie top N, pošle e-mail (bez blokovania signal-u) a uloží LAST_SIGNAL."""
    global LAST_SIGNAL
    regime_text = "risk-on" if regime == 1 else "risk-off"

    ATR_PCT_MAX = _env_float("ATR_PCT_MAX", 0.08)
    filtered = [r for r in rows if r["atr_pct"] <= ATR_PCT_MAX]

    weights = {
        "w1": _env_float("W1", 0.20),
        "w2": _env_float("W2", 0.25),
        "w3": _env_float("W3", 0.15),
        "w4": _env_float("W4", 0.20),
        "w5": _env_float("W5", 0.10),
        "w6": _env_float("W6", 0.10),
    }
    ranked = compute_scores(filtered, weights) if filtered else []
    top_k = _env_int("PICK_TOP", 4)
    picks = ranked[:top_k]

    # váhy (softmax)
    if picks:
        scores = [p["score"] for p in picks]
        exps = [math.exp(s - max(scores)) for s in scores]
        ssum = sum(exps) or 1.0
        for i, p in enumerate(picks):
            p["weight"] = round(exps[i] / ssum, 3)

    # e-mail (neblokuje ukladanie LAST_SIGNAL)
    if regime == 0:
        _safe_send_email(
            "Krypto Broker – RISK-OFF",
            f"<h3>Režim trhu: RISK-OFF ⚠️</h3><p>Odporúčanie: presun do stablecoinov (manuálne).</p>"
        )
    else:
        rows_html = "".join([
            f"<tr><td>{p['symbol']}</td><td>{p['name']}</td>"
            f"<td>{p['price']:.4f}</td><td>{p['score']:.3f}</td>"
            f"<td>{p.get('weight', 0.0):.3f}</td><td>{p['mom_24h']*100:.2f}%</td>"
            f"<td>{p['atr_pct']*100:.2f}%</td></tr>"
            for p in picks
        ])
        table = (
            "<table border='1' cellpadding='6' cellspacing='0'>"
            "<tr><th>Symbol</th><th>Názov</th><th>Cena</th><th>Skóre</th>"
            "<th>Váha</th><th>24h</th><th>ATR%</th></tr>" + rows_html + "</table>"
        )
        _safe_send_email("Krypto Broker – TOP výber", f"<h3>TOP {top_k} – návrh nákupu</h3><p>Režim: {regime_text}</p>{table}")

    # ulož posledný signál pre /signal
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

async def job_30m():
    try:
        logging.info("job_30m start")
        # 1) načítaj top200 (cache 12h)
        markets = await get_markets_top200_cached("usd", ttl_minutes=720)
        regime = await regime_flag()  # 1=risk-on, 0=risk-off

        rows = []
        if markets:
            # vyhoď stablecoiny a nízke volume
            STABLE_IDS = {"tether", "usd-coin", "dai", "usdd", "frax"}
            MIN_VOL = _env_float("MIN_24H_VOLUME_USD", 10_000_000)
            for m in markets:
                if m.get("id") in STABLE_IDS:
                    continue
                vol24 = float(m.get("total_volume") or 0.0)
                if vol24 < MIN_VOL:
                    continue
                rows.append({
                    "id": m.get("id"),
                    "symbol": (m.get("symbol") or "").upper(),
                    "name": m.get("name"),
                    "price": float(m.get("current_price") or 0.0),
                    "vol24": vol24,
                })

            # predvýber podľa objemu
            PRESELECT = min(len(rows), _env_int("PRESELECT", 30))
            rows.sort(key=lambda x: x["vol24"], reverse=True)
            pre = rows[:PRESELECT]
            ids = [r["id"] for r in pre]

            # 2) hourly grafy
            charts = await fetch_many_hourly(ids, days=10)

            # 3) obohatenie metrikami
            enriched = []
            vol_by_id = {r["id"]: r["vol24"] for r in pre}
            for cid, data in charts.items():
                row = _enrich_from_prices(cid, data.get("prices", []), vol24=vol_by_id.get(cid, 1.0))
                if row:
                    enriched.append(row)

            await _build_and_notify(enriched, regime)
            logging.info("job_30m done (top200 path); picks=%d", len(enriched))
            return

        # FALLBACK: markets prázdne – použi pevný zoznam veľkých coinov
        logging.warning("markets empty (rate-limit?). Using BOOTSTRAP_IDS fallback.")
        ids = [i.strip() for i in DEFAULT_BOOTSTRAP_IDS if i.strip()]
        charts = await fetch_many_hourly(ids, days=10)
        enriched = []
        for cid, data in charts.items():
            row = _enrich_from_prices(cid, data.get("prices", []), vol24=1.0)
            if row:
                enriched.append(row)

        await _build_and_notify(enriched, regime)
        logging.info("job_30m done (fallback path); picks=%d", len(enriched))

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
