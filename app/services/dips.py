from typing import List, Dict, Optional
import math

# Pomocné indikátory berieme z tvojho modulu
from .indicators import pct_change, atr_from_closes, ema, rsi

STABLE_SYMBOLS = {
    "USDT","USDC","DAI","FDUSD","TUSD","USDD","USDP","GUSD","EURS","EURC"
}

def _is_stable(m: Dict) -> bool:
    sym = (m.get("symbol") or "").upper()
    nm = (m.get("name") or "").lower()
    cid = (m.get("id") or "").lower()
    if sym in STABLE_SYMBOLS:
        return True
    if "stable" in nm:  # např. "Stablecoin"
        return True
    if "usd" in cid or "usd" in nm:  # first-digital-usd, true-usd, ...
        return True
    return False

def _metrics_from_prices(closes: List[float]) -> Optional[Dict]:
    if not closes or len(closes) < 24*7+2:
        return None
    close = float(closes[-1])
    m3h  = pct_change(close, closes[-4])  if len(closes) > 4      else 0.0
    m24h = pct_change(close, closes[-24]) if len(closes) > 24     else 0.0
    m7d  = pct_change(close, closes[-24*7]) if len(closes) > 24*7 else 0.0
    atr  = atr_from_closes(closes, period=14)
    atrp = (atr[-1] / close) if close else 0.0
    e10  = ema(closes, 10)[-1]
    rsi14 = rsi(closes, 14)[-1]
    return {
        "price": close,
        "mom_3h": float(m3h),
        "mom_24h": float(m24h),
        "mom_7d": float(m7d),
        "atr_pct": float(atrp),
        "ema10": float(e10),
        "rsi": float(rsi14),
    }

def pick_dips(
    markets: List[Dict],
    charts: Dict[str, Dict],
    *,
    count: int = 2,
    min_7d_drop: float = -0.35,   # <= -35 %
    max_atr_pct: float = 0.20,    # <= 20 %
    min_vol24: float = 5_000_000, # >= 5M USD
) -> List[Dict]:
    """
    Vyberie 'count' coinov po veľkom prepade s náznakom odrazu.
    markets: výstup z /coins/markets
    charts: {id: {"prices": [[ts, close], ...]}} za ~10 dní (hodinové)
    """
    # 0) preselect – vezmeme ~40 najhorších za 24h z TOP200 (aby sme nemuseli ťahať grafy pre všetkých)
    losers = []
    for m in markets:
        if _is_stable(m):
            continue
        cid = m.get("id"); sym = (m.get("symbol") or "").upper(); nm = m.get("name") or sym
        pc24 = float(m.get("price_change_percentage_24h_in_currency") or 0.0)
        vol24 = float(m.get("total_volume") or 0.0)
        if not cid or vol24 < min_vol24:
            continue
        losers.append((pc24, cid, sym, nm, vol24))
    losers.sort(key=lambda x: x[0])  # najväčší prepady najprv
    losers = losers[:40]

    # 1) spočítaj metriky z grafov a urob filtráciu
    out: List[Dict] = []
    for _, cid, sym, nm, vol24 in losers:
        series = charts.get(cid, {}).get("prices", [])
        closes = [float(p[1]) for p in series]
        met = _metrics_from_prices(closes)
        if not met:
            continue

        # základné filtre
        if met["mom_7d"] > min_7d_drop:   # chceme napr. <= -35 %
            continue
        if met["atr_pct"] > max_atr_pct:
            continue

        # potvrdenie odrazu: 3h momentum pozitívne ALEBO close nad EMA10 ALEBO RSI 14 > 35
        bounce = (met["mom_3h"] > 0.02) or (met["price"] > met["ema10"]) or (met["rsi"] > 35.0)
        if not bounce:
            continue

        # jednoduchý scoring: preferujeme väčší prepad + čerstvý odraz, penalizuj vysokú ATR
        score = (abs(min(met["mom_7d"], -0.01)) * 0.6) + (max(met["mom_3h"], 0.0) * 0.5) - (met["atr_pct"] * 0.3)

        # návrh SL / TP a horizont
        atrp = met["atr_pct"]
        price = met["price"]
        sl     = price * (1.0 - max(atrp, 0.10))     # min. ~10% SL
        tp1    = price * (1.0 + max(1.5*atrp, 0.08)) # min. 8% TP1
        tp2    = price * (1.0 + max(2.5*atrp, 0.15)) # min. 15% TP2
        horiz  = 0.5 if atrp >= 0.10 else 2.0        # ~12h alebo ~2 dni

        out.append({
            "id": cid, "symbol": sym, "name": nm,
            "price": price,
            "vol24": vol24,
            "mom_3h": met["mom_3h"], "mom_24h": met["mom_24h"], "mom_7d": met["mom_7d"],
            "atr_pct": atrp, "ema10": met["ema10"], "rsi": met["rsi"],
            "score": score,
            "sl_usd": sl, "tp1_usd": tp1, "tp2_usd": tp2,
            "horizon_days": horiz,
        })

    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:count]
