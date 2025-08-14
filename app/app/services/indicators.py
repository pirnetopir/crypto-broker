from typing import List
import math

def pct_change(curr: float, prev: float) -> float:
    if prev == 0 or prev is None or curr is None:
        return 0.0
    return (curr / prev) - 1.0

def ema(values: List[float], period: int) -> List[float]:
    if not values or period <= 1:
        return values[:] if values else []
    k = 2.0 / (period + 1.0)
    out: List[float] = []
    ema_val = values[0]
    out.append(ema_val)
    for i in range(1, len(values)):
        ema_val = values[i] * k + ema_val * (1.0 - k)
        out.append(ema_val)
    return out

def rsi(values: List[float], period: int = 14) -> List[float]:
    if not values or len(values) < period + 1:
        return [50.0 for _ in values]  # neutrál
    gains = [0.0]; losses = [0.0]
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    out = [50.0 for _ in range(period)]  # naplň začiatok
    for i in range(period+1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rs = math.inf
        else:
            rs = avg_gain / avg_loss
        rsi_val = 100.0 - (100.0 / (1.0 + rs))
        out.append(rsi_val)
    # dorovnaj dĺžku (prvé hodnoty neutrál)
    while len(out) < len(values):
        out.insert(0, 50.0)
    return out

def atr_from_closes(values: List[float], period: int = 14) -> List[float]:
    """
    Proxy ATR iba z close-to-close (bez H/L). Stačí pre relatívny 'atr_pct'.
    """
    if not values:
        return []
    tr = [0.0]
    for i in range(1, len(values)):
        tr.append(abs(values[i] - values[i-1]))
    # Wilder smoothing
    atr: List[float] = []
    if len(tr) < period:
        atr = [sum(tr)/max(1,len(tr)) for _ in tr]
    else:
        first = sum(tr[1:period+1]) / period
        atr = [first]
        for i in range(period+1, len(tr)):
            prev = atr[-1]
            atr.append((prev*(period-1) + tr[i]) / period)
        while len(atr) < len(values):
            atr.insert(0, atr[0])
    # zlaď dĺžku
    while len(atr) < len(values):
        atr.append(atr[-1])
    return atr
