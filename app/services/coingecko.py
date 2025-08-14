import os
import time
import random
import asyncio
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx

PLAN: str = os.getenv("COINGECKO_PLAN", "public").lower().strip()  # "public" | "demo" | "pro"
KEY: str = os.getenv("COINGECKO_KEY", "").strip()

BASE: str = "https://api.coingecko.com/api/v3"
if PLAN == "pro":
    BASE = "https://pro-api.coingecko.com/api/v3"

_HEADERS: Dict[str, str] = {
    "User-Agent": "crypto-broker/1.0",
    "Accept": "application/json",
}
if KEY:
    if PLAN == "demo":
        _HEADERS["x-cg-demo-api-key"] = KEY
    elif PLAN == "pro":
        _HEADERS["x-cg-pro-api-key"] = KEY

DEFAULT_CONCURRENCY: int = int(os.getenv("CG_CONCURRENCY", "1"))
DEFAULT_SLEEP: float = float(os.getenv("CG_SLEEP", "2.2"))

_markets_cache: Dict[str, Optional[float] | Optional[List[Dict]]] = {"ts": 0.0, "data": None}

def _with_key(url: str) -> str:
    if not KEY:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    if PLAN == "demo":
        q["x_cg_demo_api_key"] = KEY
    elif PLAN == "pro":
        q["x_cg_pro_api_key"] = KEY
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q), u.fragment))

async def _get_json(url: str, tries: int = 7, base_sleep: float = 2.0) -> Dict:
    last_exc: Optional[Exception] = None
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=60, headers=_HEADERS) as c:
                u = _with_key(url)
                r = await c.get(u)
                if r.status_code in (429,) or 500 <= r.status_code < 600:
                    raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            sleep = base_sleep * (2 ** attempt) + random.uniform(0, 0.6)
            await asyncio.sleep(sleep)
    raise last_exc  # type: ignore[misc]

def _dedupe_keep_order(items: List[Dict], key: str = "id") -> List[Dict]:
    seen = set(); out: List[Dict] = []
    for x in items:
        k = x.get(key)
        if k not in seen:
            seen.add(k); out.append(x)
    return out

def _clamp_days(days: int) -> int:
    if PLAN in ("public", "demo"):
        return min(days, 365)
    return days

# ---------- Markets (TOP200) ----------
async def _get_markets(per_page: int, page: int = 1, vs: str = "usd") -> List[Dict]:
    url = (
        f"{BASE}/coins/markets?vs_currency={vs}&order=market_cap_desc"
        f"&per_page={per_page}&page={page}&price_change_percentage=24h"
    )
    return await _get_json(url)  # type: ignore[return-value]

async def get_markets_top200_slow(vs: str = "usd") -> List[Dict]:
    try:
        return await _get_markets(200, 1, vs)
    except Exception:
        pass
    out: List[Dict] = []
    ok = True
    for p in (1, 2):
        try:
            out.extend(await _get_markets(100, p, vs))
            await asyncio.sleep(1.2)
        except Exception:
            ok = False; break
    if ok and out:
        return _dedupe_keep_order(out)[:200]
    out = []
    for p in (1, 2, 3, 4):
        try:
            out.extend(await _get_markets(50, p, vs))
        except Exception:
            pass
        await asyncio.sleep(1.5)
    out = _dedupe_keep_order(out)
    if not out:
        raise httpx.HTTPStatusError("status 429", request=None, response=None)  # type: ignore[arg-type]
    return out[:200]

async def get_markets_top200_cached(vs: str = "usd", ttl_minutes: int = 720) -> List[Dict]:
    now = time.time()
    if _markets_cache["data"] and now - float(_markets_cache["ts"] or 0) < ttl_minutes * 60:
        return _markets_cache["data"] or []  # type: ignore[return-value]
    try:
        data = await get_markets_top200_slow(vs)
        _markets_cache["data"] = data
        _markets_cache["ts"] = now
        return data
    except Exception:
        return _markets_cache["data"] or []  # type: ignore[return-value]

# ---------- Historické ceny ----------
async def get_market_chart(coin_id: str, days: int = 10) -> Dict:
    d = _clamp_days(days)
    url = f"{BASE}/coins/{coin_id}/market_chart?vs_currency=usd&days={d}"
    return await _get_json(url)  # type: ignore[return-value]

async def get_btc_daily(days: int = 365) -> Dict:
    d = _clamp_days(days)
    url = f"{BASE}/coins/bitcoin/market_chart?vs_currency=usd&days={d}"
    return await _get_json(url)  # type: ignore[return-value]

async def fetch_many_hourly(
    ids: List[str],
    days: int = 10,
    concurrency: Optional[int] = None,
    sleep_between: Optional[float] = None,
) -> Dict[str, Dict]:
    if concurrency is None:
        concurrency = DEFAULT_CONCURRENCY
    if sleep_between is None:
        sleep_between = DEFAULT_SLEEP
    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, Dict] = {}
    async def _one(cid: str) -> None:
        async with sem:
            try:
                results[cid] = await get_market_chart(cid, days=days)
            except Exception:
                results[cid] = {"prices": []}
            await asyncio.sleep(float(sleep_between))
    await asyncio.gather(*[_one(cid) for cid in ids])
    return results

# ---------- Jednoduché ceny pre watchlist ----------
async def get_simple_prices(ids: List[str], vs: str = "usd") -> Dict[str, float]:
    """
    Vráti dict {coin_id: price_usd}. BATCHuje po ~100 ids.
    """
    out: Dict[str, float] = {}
    if not ids:
        return out
    step = 100
    for i in range(0, len(ids), step):
        chunk = ids[i:i+step]
        ids_param = ",".join(chunk)
        url = f"{BASE}/simple/price?ids={ids_param}&vs_currencies={vs}"
        try:
            data = await _get_json(url)
            for k, v in data.items():
                price = v.get(vs)
                if price is not None:
                    out[k] = float(price)
        except Exception:
            # preskoč chunk
            pass
        await asyncio.sleep(0.3)
    return out

# ---------- Diagnostika ----------
async def ping() -> Dict:
    url = f"{BASE}/ping"
    return await _get_json(url)  # type: ignore[return-value]
