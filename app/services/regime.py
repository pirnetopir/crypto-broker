from .coingecko import get_btc_daily
from .indicators import ema

async def regime_flag() -> int:
    """1 = risk-on, 0 = risk-off"""
    data = await get_btc_daily(days=400)
    closes = [p[1] for p in data.get("prices", [])]
    if len(closes) < 200:
        return 1  # defaultne risk-on
    e200 = ema(closes, 200)
    under = closes[-1] < float(e200[-1])
    # jednoduchý 7d drawdown
    last7 = closes[-8:]
    dd7 = (last7[-1] - max(last7)) / max(last7) if last7 else 0.0
    return 1 if (not under and dd7 > -0.10) else 0
