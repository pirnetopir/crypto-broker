import time
import httpx
from typing import Set, Dict, Any, Optional

# Jednoduchá in-memory cache na 24h
_cache: Dict[str, Any] = {"ts": 0.0, "symbols": None}

async def get_coinbase_usd_symbols_cached(ttl_minutes: int = 1440) -> Set[str]:
    """
    Vráti množinu symbolov (BASE) obchodovateľných voči USD/USDC na Coinbase Exchange.
    Žiadny API kľúč netreba. Príklad symbolov: {"BTC","ETH","SOL",...}.
    """
    now = time.time()
    if _cache["symbols"] is not None and now - _cache["ts"] < ttl_minutes * 60:
        return _cache["symbols"]

    url = "https://api.exchange.coinbase.com/products"
    symbols: Set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
            for prod in data:
                base = (prod.get("base_currency") or "").upper()
                quote = (prod.get("quote_currency") or "").upper()
                if base and quote in {"USD", "USDC"}:
                    symbols.add(base)
    except Exception:
        # pri chybe necháme prázdny set -> žiadny filter
        symbols = set()

    _cache["ts"] = now
    _cache["symbols"] = symbols
    return symbols
