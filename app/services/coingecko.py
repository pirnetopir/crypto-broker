import httpx, asyncio, time, random, os
from typing import List, Dict

PLAN = os.getenv("COINGECKO_PLAN", "public").lower().strip()
KEY  = os.getenv("COINGECKO_KEY", "").strip()

BASE = "https://api.coingecko.com/api/v3"
if PLAN == "pro":
    BASE = "https://pro-api.coingecko.com/api/v3"

_HEADERS = {
    "User-Agent": "crypto-broker/1.0 (contact: email in EMAIL_TO env)",
    "Accept": "application/json",
}

# demo/pro kľúč do hlavičky – podľa oficiálneho návodu
# demo: x-cg-demo-api-key, pro: x-cg-pro-api-key
if KEY:
    if PLAN == "demo":
        _HEADERS["x-cg-demo-api-key"] = KEY
    elif PLAN == "pro":
        _HEADERS["x-cg-pro-api-key"] = KEY

# jednoduchá in-memory cache pre top200
_markets_cache = {"ts": 0.0, "data": None}

async def _get_json(url: str, tries: int = 7, base_sleep: float = 2.5):
    last_exc = None
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=60, headers=_HEADERS) as c:
                r = await c.get(url)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"status {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            sleep = base_sleep * (2 ** attempt) + random.uniform(0, 0.6)
            await asyncio.sleep(sleep)
    raise last_exc

async def _get_markets(per_page: int, page: int = 1, vs: str = "usd"):
    url = (
        f"{BASE}/coins/markets?vs_currency={vs}&order=market_cap_desc"
        f"&per_page={per_page}&page={page}&price_change_percentage=24h"
    )
    return await _get_json(url)

async def get_markets_top200_slow(vs: str = "usd") -> List[Dict]:
    # 1) 200 na 1 request
    try:
        return await _get_markets(200, 1, vs)
    except Exception:
        pass
    # 2) 2×100
    out = []
    ok = True
    for p in (1, 2):
        try:
            out.extend(await _get_markets(100, p, vs))
            await asyncio.sleep(1.5)
        except Exception:
            ok = False
            break
    if ok and out:
        seen, uniq = set(), []
        for x in out:
            i = x.get("id")
            if i not in seen:
                seen.add(i); uniq.append(x)
        return uniq
    # 3) 4×50
    out = []
    for p in (1, 2, 3, 4):
        try:
            out.extend(await _get_markets(50, p, vs))
        except Exception:
            pass
        await asyncio.sleep(2.0)
    seen, uniq = set(), []
    for x in out:
        i = x.get("id")
        if i not in seen:
            seen.add(i); uniq.append(x)
        if len(uniq) >= 200: break
    if not uniq:
        raise httpx.HTTPStatusError("status 429", request=None, response=None)
    return uniq

async def get_markets_top200_cached(vs: str = "usd", ttl_minutes: int = 720) -> List[Dict]:
    now = time.time()
    if _markets_cache["data"] and now - _markets_cache["ts"] < ttl_minutes * 60:
        return _markets_cache["data"]
    try:
        data = await get_markets_top200_slow(vs)
        _markets_cache["data"] = data
        _markets_cache["ts"] = now
        return data
    except Exception:
        return _markets_cache["data"] or []

async def get_market_chart(coin_id: str, days: int = 10, interval: str = "hourly") -> Dict:
    url = f"{BASE}/coins/{coin_id}/market_chart?vs_currency=usd&days={days}&interval={interval}"
    return await _get_json(url)

async def fetch_many_hourly(
    ids: List[str],
    days: int = 10,
    concurrency: int | None = None,
    sleep_between: float | None = None,
) -> Dict[str, Dict]:
    # ber z ENV (bezpecne pre Demo 30rpm)
    if concurrency is None:
        concurrency = int(os.getenv("CG_CONCURRENCY", "1"))
    if sleep_between is None:
        sleep_between = float(os.getenv("CG_SLEEP", "2.2"))

    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, Dict] = {}

    async def _one(cid: str):
        async with sem:
            try:
                results[cid] = await get_market_chart(cid, days=days, interval="hourly")
            except Exception:
                results[cid] = {"prices": []}
            await asyncio.sleep(sleep_between)

    await asyncio.gather(*[_one(cid) for cid in ids])
    return results

async def get_btc_daily(days: int = 400) -> Dict:
    url = f"{BASE}/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily"
    return await _get_json(url)
