from typing import List, Dict

def _rank01(vals: List[float]) -> List[float]:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo == 0:
        return [0.5 for _ in vals]
    return [(v - lo) / (hi - lo) for v in vals]

def compute_scores(rows: List[Dict], w: Dict[str, float]) -> List[Dict]:
    """
    rows očakáva kľúče:
      price, vol24, mom_3h, mom_24h, mom_7d, atr_pct, trend_flag, (voliteľne rsi, ema_above)
    """
    if not rows:
        return []
    a_m3 = _rank01([r.get("mom_3h", 0.0) for r in rows])
    a_m24 = _rank01([r.get("mom_24h", 0.0) for r in rows])
    a_m7 = _rank01([r.get("mom_7d", 0.0) for r in rows])
    a_vol = _rank01([r.get("vol24", 0.0) for r in rows])
    a_atr = _rank01([r.get("atr_pct", 0.0) for r in rows])

    for i, r in enumerate(rows):
        score = (
            w.get("w1",0.20)*a_m3[i] +
            w.get("w2",0.25)*a_m24[i] +
            w.get("w3",0.15)*a_m7[i] +
            w.get("w4",0.20)*(1.0 if r.get("trend_flag",0)==1 else 0.0) +
            w.get("w5",0.10)*a_vol[i] -
            w.get("w6",0.10)*a_atr[i]
        )
        r["score"] = float(score)
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows
