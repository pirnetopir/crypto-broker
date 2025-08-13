import httpx
import asyncio
from typing import List, Dict

BASE = "https://api.coingecko.com/api/v3"

async def get_markets_top200(vs: str = "usd") -> List[Dict]:
    url = f"{BASE}/coins/markets?vs_currency={vs}&order=market_cap_desc&per_page=200&page=1&price_change_percentage=24h"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json()

async def get_market_chart(coin_id: str, days: int = 10, interval: str = "hourly") -> Dict:
    """Hourly ceny (10 dní stačí na 7d momentum a ATR)."""
    url = f"{BASE}/coins/{coin_id}/market_chart?vs_currency=usd&days={days}&interval={interval}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json()

async def get_btc_daily(days: int = 400) -> Dict:
    url = f"{BASE}/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json()

async def fetch_many_hourly(ids: List[str], days: int = 10, concurrency: int = 4) -> Dict[str, Dict]:
    """Stiahne hourly grafy pre viac coinov so základným rate-limitom."""
    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, Dict] = {}

    async def _one(cid: str):
        async with sem:
            try:
                data = await get_market_chart(cid, days=days, interval="hourly")
                results[cid] = data
            except Exception:
                # preskoč, keď coin zlyhá
                results[cid] = {"prices": []}

    await asyncio.gather(*[_one(cid) for cid in ids])
    return results
