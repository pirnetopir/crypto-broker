import httpx, asyncio, time, random
from typing import List, Dict

BASE = "https://api.coingecko.com/api/v3"

# jednoduchá in-memory cache pre top200
_markets_cache = {"ts": 0.0, "data": None}

async def _get_json(url: str, tries: int = 5, base_sleep: float = 1.5):
    """
    GET s exponenciálnym backoffom. Retry pri 429 a 5xx.
    """
    last_exc = None
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.get(url)
                # 429/5xx -> retry
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"status {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            # exponenciálny backoff + trocha jitteru
            sleep = base_sleep * (2 ** attempt) + random.uniform(0, 0.3)
            await asyncio.sleep(sleep)
    # po vyčerpaní pokusov
    raise last_exc

async def get_markets_top200(vs: str = "usd") -> List[Dict]:
    url = (
        f"{BASE}/coins/markets?vs_currency={vs}&order=market_cap_desc"
        f"&per_page=200&page=1&price_change_percentage=24h"
    )
    return await _get_json(url)

async def get_markets_top200_cached(vs: str = "usd", ttl_minutes: int = 720) -> List[Dict]:
    """
    Vráti top200 z cache; ak je staršia než ttl, stiahne znova.
    """
    now = time.time()
    if _markets_cache["data"] and now - _markets_cache["ts"] < ttl_minutes * 60:
        return _markets_cache["data"]
    data = await get_markets_top200(vs)
    _markets_cache["data"] = data
    _markets_cache["ts"] = now
    return data

async def get_market_chart(coin_id: str, days: int = 10, interval: str = "hourly") -> Dict:
    url = f"{BASE}/coins/{coin_id}/market_chart?vs_currency=usd&days={days}&interval={interval}"
    return await _get_json(url)

async def fetch_many_hourly(
    ids: List[str],
    days: int = 10,
    concurrency: int = 2,
    sleep_between: float = 1.2,
) -> Dict[str, Dict]:
    """
    Sťahuje hourly grafy s limitovanou paralelnosťou a pauzou medzi volaniami.
    """
    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, Dict] = {}

    async def _one(cid: str):
        async with sem:
            try:
                results[cid] = await get_market_chart(cid, days=days, interval="hourly")
            except Exception:
                results[cid] = {"prices": []}
            # po každom requeste krátka pauza kvôli rate-limitom
            await asyncio.sleep(sleep_between)

    await asyncio.gather(*[_one(cid) for cid in ids])
    return results

async def get_btc_daily(days: int = 400) -> Dict:
    url = f"{BASE}/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily"
    return await _get_json(url)
