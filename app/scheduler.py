import os
import math
import logging
import asyncio
from datetime import datetime
from typing import List, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .services.coingecko import get_markets_top200_cached, fetch_many_hourly
from .services.indicators import atr_from_closes, pct_change
from .services.regime import regime_flag
from .services.scorer import compute_scores
from .services.notifier import send_email
from .services.signals import Pick, SignalPack

# ---------------------------------------------------------------------
# Globálny scheduler + posledný signál
# ---------------------------------------------------------------------

_scheduler: Optional[AsyncIOScheduler] = None
LAST_SIGNAL: Optional[SignalPack] = None

# Základný fallback zoznam veľkých coinov, ak top-200 zlyhá (rate-limit/výpadok)
DEFAULT_BOOTSTRAP_IDS: List[str] = os.getenv(
    "BOOTSTRAP_IDS",
    "bitcoin,ethereum,binancecoin,solana,ripple,cardano,dogecoin,tron,polkadot,chainlink,litecoin,uniswap,avalanche-2"
).split(",")

# Stablecoiny, ktoré vylúčime z výberu
STABLE_IDS = {
    "tether", "usd-coin", "dai", "usdd", "frax",
    "first-digital-usd", "paxos-standard", "true-usd",
    "paypal-usd", "gemini-dollar", "usdp"
}


# ---------------------------------------------------------------------
# Pomocné funkcie
# ---------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)

def _safe_send_email(subject: str, html: str) -> None:
    try:
        send_email(subject, html)
    except Exception as e:
        logging.warning("email send failed: %s", e)

def _enrich_from_prices(id_: str, prices: List[List[float]], vol24: float = 1.0) -> Optional[Dict]:
    """
    Z hourly/denných close hodnôt vypočíta základné metriky pre scoring.
    Ak je málo dát, vráti None.
    """
    closes = [p[1] for p in prices]
    if len(closes) < 200:
        return None

    close = closes[-1]
    mom_3h = pct_change(close, closes[-4]) if len(closes) > 4 else 0.0
    mom_24h = pct_change(close, closes[-24]) if len(closes) > 24 else 0.0
    mom_7d = pct_change(close, closes[-24 * 7]) if len(closes) > 24 * 7 else 0.0
    atr = atr_from_closes(closes, period=14)
    atr_pct = (atr[-1] / close) if close else 0.0
    trend_flag = 1 if mom_7d > 0 else 0

    # symbol/name fallback (bez ďalšieho volania)
    symbol = id_[:6].upper()
    name = id_

    return {
        "id": id_,
        "symbol": symbol,
        "name": name,
        "price": float(close),
        "vol24": float(vol24),
        "mom_3h": float(mom_3h),
        "mom_24h": float(mom_24h),
        "mom_7d": float(mom_7d),
        "atr_pct": float(atr_pct),
        "trend_flag": int(trend_flag),
    }

async def _build_and_notify(rows: List[Dict], regime: int) -> None:
    """
    Zoradí podľa skóre, vyberie top N, pošle e-mail (bez blokovania signálu),
    a uloží LAST_SIGNAL pre /signal endpoint.
    """
    global LAST_SIGNAL
    regime_text = "risk-on" if regime == 1 else "risk-off"

    # filtre + váhy zo scoringu
    atr_pct_max = _env_float("ATR_PCT_MAX", 0.08)  # 8 %
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

    top_k = _env_int("PICK_TOP", 4)
    picks = ranked[:top_k]

    # váhy (softmax)
    if picks:
        scores = [p["score"] for p in picks]
        m = max(scores)
        exps = [math.exp(s - m) for s in scores]
        ssum = sum(exps) or 1.0
        for i, p in enumerate(picks):
            p["weight"] = round(exps[i] / ssum, 3)

    # e-mail (nebráni uloženiu signálu)
    if regime == 0:
        _safe_send_email(
            "Krypto Broker – RISK-OFF",
            "<h3>Režim trhu: RISK-OFF ⚠️</h3><p>Odporúčanie: presun do stablecoinov (manuálne).</p>",
        )
    else:
        rows_html = "".join([
            f"<tr><td>{p['symbol']}</td><td>{p['name']}</td>"
            f"<td>{p['price']:.6f}</td><td>{p['score']:.3f}</td>"
            f"<td>{p.get('weight', 0.0):.3f}</td><td>{p['mom_24h']*100:.2f}%</td>"
            f"<td>{p['atr_pct']*100:.2f}%</td></tr>"
            for p in picks
        ])
        table = (
            "<table border='1' cellpadding='6' cellspacing='0'>"
            "<tr><th>Symbol</th><th>Názov</th><th>Cena</th><th>Skóre</th>"
            "<th>Váha</th><th>24h</th><th>ATR%</th></tr>" + rows_html + "</table>"
        )
        _safe_send_email(
            f"Krypto Broker – TOP {top_k} – návrh nákupu",
            f"<h3>TOP {top_k} – návrh nákupu</h3><p>Režim: {regime_text}</p>{table}"
        )

    # uloženie posledného signálu (aj keď nie sú picks, nech máme režim a čas)
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


# ---------------------------------------------------------------------
# Hlavný job
# ---------------------------------------------------------------------

async def job_30m() -> None:
    """
    Hlavná úloha:
    - načíta top-200 (s cache),
    - vyhodí stablecoiny a nízky objem,
    - stiahne grafy pre PRESELECT coinov,
    - spočíta metriky, režim trhu, pošle e-mail a uloží LAST_SIGNAL.
    """
    try:
        logging.info("job_30m start")

        # Režim trhu (BTC)
        reg = await regime_flag()  # 1=risk-on, 0=risk-off

        # Zoznam trhu (cache ≈ 6 hodín)
        markets = await get_markets_top200_cached("usd", ttl_minutes=360)

        rows: List[Dict] = []
        if markets:
            # filtrovanie + predvýber podľa objemu
            min_vol = _env_float("MIN_24H_VOLUME_USD", 10_000_000)
            for m in markets:
                if m.get("id") in STABLE_IDS:
                    continue
                vol24 = float(m.get("total_volume") or 0.0)
                if vol24 < min_vol:
                    continue
                rows.append({
                    "id": m.get("id"),
                    "symbol": (m.get("symbol") or "").upper(),
                    "name": m.get("name"),
                    "price": float(m.get("current_price") or 0.0),
                    "vol24": vol24,
                })

            preselect = _env_int("PRESELECT", 80)  # odporúčané pre Demo pri 3x/deň
            rows.sort(key=lambda x: x["vol24"], reverse=True)
            pre = rows[:min(len(rows), preselect)]
            ids = [r["id"] for r in pre]

            # Grafy (CoinGecko Demo: držíme concurrency=1, spánok z ENV)
            charts = await fetch_many_hourly(ids, days=10)

            # Obohatenie o metriky
            enriched: List[Dict] = []
            vol_by_id = {r["id"]: r["vol24"] for r in pre}
            for cid, data in charts.items():
                row = _enrich_from_prices(cid, data.get("prices", []), vol24=vol_by_id.get(cid, 1.0))
                if row:
                    enriched.append(row)

            await _build_and_notify(enriched, reg)
            logging.info("job_30m done (top200); enriched=%d", len(enriched))
            return

        # FALLBACK – ak markets prázdne, použijeme pevný zoznam
        logging.warning("markets empty (rate-limit/outage). Using BOOTSTRAP_IDS fallback.")
        ids = [i.strip() for i in DEFAULT_BOOTSTRAP_IDS if i.strip()]
        charts = await fetch_many_hourly(ids, days=10)
        enriched = []
        for cid, data in charts.items():
            row = _enrich_from_prices(cid, data.get("prices", []), vol24=1.0)
            if row:
                enriched.append(row)

        await _build_and_notify(enriched, reg)
        logging.info("job_30m done (fallback); enriched=%d", len(enriched))

    except Exception as e:
        logging.exception("job_30m error: %s", e)


# ---------------------------------------------------------------------
# Konfigurácia plánovača – 3× denne
# ---------------------------------------------------------------------

def create_scheduler() -> AsyncIOScheduler:
    """
    Spúšťaj job presne o 07:30, 13:00 a 22:00 (čas podľa TZ).
    TZ berieme z ENV (predvolene Europe/Bratislava).
    """
    global _scheduler
    if _scheduler is None:
        tz = os.getenv("TZ", "Europe/Bratislava")
        _scheduler = AsyncIOScheduler(timezone=tz)

        # ráno 07:30
        _scheduler.add_job(
            job_30m,
            CronTrigger(hour=7, minute=30),
            id="job_morning",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # obed 13:00
        _scheduler.add_job(
            job_30m,
            CronTrigger(hour=13, minute=0),
            id="job_noon",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # večer 22:00
        _scheduler.add_job(
            job_30m,
            CronTrigger(hour=22, minute=0),
            id="job_evening",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return _scheduler
