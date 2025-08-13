import numpy as np
import pandas as pd

def ema(arr, n: int):
    arr = np.asarray(arr, dtype=float)
    if len(arr) == 0:
        return arr
    k = 2/(n+1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def atr_from_closes(closes, period: int = 14):
    """Simplifikovaná ATR z absolútnych zmien close (bez H/L)."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < 2:
        return np.zeros_like(closes)
    changes = np.abs(np.diff(closes))
    tr = np.concatenate([[changes[0]], changes])
    atr = np.empty_like(tr)
    atr[0] = tr[0]
    for i in range(1, len(tr)):
        atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
    return atr

def rank_pct(values):
    """Percentilový rank v rozsahu 0..1 (vyššie=lepšie)."""
    import numpy as np
    v = np.asarray(values, dtype=float)
    order = np.argsort(v)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(len(v))
    denom = max(1, len(v)-1)
    return ranks / denom

def pct_change(a, b):
    if b == 0 or b is None:
        return 0.0
    return (a - b) / b

def last_close(series):
    return float(series[-1]) if len(series) else 0.0
