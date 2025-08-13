from typing import List, Dict
import numpy as np
from .indicators import rank_pct

def compute_scores(rows: List[Dict], weights: Dict[str, float]) -> List[Dict]:
    """
    Očakáva zoznam dictov s kľúčmi:
    id, symbol, name, price, vol24, mom_3h, mom_24h, mom_7d, trend_flag, atr_pct
    a vráti dict s 'score' + zoradený zoznam.
    """
    if not rows:
        return []

    mom3 = np.array([r["mom_3h"] for r in rows])
    mom24 = np.array([r["mom_24h"] for r in rows])
    mom7 = np.array([r["mom_7d"] for r in rows])
    vol = np.array([r["vol24"] for r in rows])
    atrp = np.array([r["atr_pct"] for r in rows])
    trend = np.array([r["trend_flag"] for r in rows])

    r_m3 = rank_pct(mom3)
    r_m24 = rank_pct(mom24)
    r_m7 = rank_pct(mom7)
    r_vol = rank_pct(vol)
    r_atr = rank_pct(atrp)  # penalizujeme vyššie ATR

    # skóre
    for i, r in enumerate(rows):
        score = (
            weights["w1"] * r_m3[i] +
            weights["w2"] * r_m24[i] +
            weights["w3"] * r_m7[i] +
            weights["w4"] * float(trend[i]) +
            weights["w5"] * r_vol[i] -
            weights["w6"] * r_atr[i]
        )
        r["score"] = float(score)

    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows
