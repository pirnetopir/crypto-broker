import time
import math
from typing import Dict, List, Tuple
from datetime import datetime, timezone
import feedparser
import re

# Verejné RSS (bez kľúčov)
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://blog.coinbase.com/feed",
    "https://www.binance.com/en/blog/feed",
    "https://okx.com/learn/feeds/rss",
    "https://medium.com/feed/tag/crypto",
]

_word = re.compile(r"[A-Za-z0-9\-_.]+")

def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()

def _age_hours(published_ts: float) -> float:
    return max(0.0, (_now_ts() - published_ts) / 3600.0)

def _text_of(entry) -> str:
    parts = []
    if getattr(entry, "title", None): parts.append(entry.title)
    if getattr(entry, "summary", None): parts.append(entry.summary)
    return " ".join(parts).lower()

def _published(entry) -> float:
    # feedparser normalizuje published_parsed
    try:
        return time.mktime(entry.published_parsed)
    except Exception:
        return _now_ts()

def _tokenize(s: str) -> List[str]:
    return [w.lower() for w in _word.findall(s)]

def fetch_candidates_from_rss(
    markets: List[Dict],
    hours_back: int = 36,
    max_candidates: int = 12,
) -> List[Dict]:
    """
    markets: výstup z CoinGecko /coins/markets (TOP200), slúži na mapovanie názvov/symbolov.
    Výsledok: zoznam kandidátov: {id, symbol, name, news_hits, news_score}
    """
    # mapy na rýchle párovanie
    by_symbol: Dict[str, Tuple[str, str]] = {}   # SYMBOL -> (id, name)
    by_name: Dict[str, Tuple[str, str]] = {}     # "solana" -> (id, name)
    for m in markets:
        cid = m.get("id")
        symbol = (m.get("symbol") or "").upper()
        name = (m.get("name") or "").lower()
        if cid and symbol:
            by_symbol[symbol] = (cid, m.get("name") or symbol)
        if cid and name:
            by_name[name] = (cid, m.get("name") or symbol)

    # agregácia zásahov
    hits: Dict[str, Dict] = {}  # id -> {id, symbol, name, news_hits, news_score}
    cutoff_h = float(hours_back)

    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                ts = _published(e)
                age_h = _age_hours(ts)
                if age_h > cutoff_h:
                    continue
                txt = _text_of(e)
                toks = set(_tokenize(txt))

                # silnejšie overenie na názvy a symboly
                matched: List[Tuple[str, str, str]] = []  # (id, symbol, name)
                # podľa symbolu (musí byť celé slovo v UPPER v titulku)
                for sym, (cid, nm) in by_symbol.items():
                    if sym.lower() in toks:
                        matched.append((cid, sym, nm))
                # podľa názvu (celé slovo, dĺžka>3 aby nechytilo "near" bežne)
                for nm_txt, (cid, nm) in by_name.items():
                    if len(nm_txt) > 3 and nm_txt in toks:
                        matched.append((cid, by_symbol.get(nm.upper(), (None, ""))[0] or (nm[:6]).upper(), nm))

                # skóre: čerstvosť (exponenciálne), 1.0 pre najnovšie; + menší bonus za zdroj
                freshness = math.exp(-age_h / 12.0)  # ~ polčas 8–12h
                base = 1.0 * freshness
                for cid, sym, nm in matched:
                    if cid not in hits:
                        hits[cid] = {"id": cid, "symbol": sym, "name": nm, "news_hits": 0, "news_score": 0.0}
                    hits[cid]["news_hits"] += 1
                    hits[cid]["news_score"] += base
        except Exception:
            continue

    # prenes len coiny s aspoň 1 zásahom
    out = [v for v in hits.values() if v["news_hits"] >= 1]
    out.sort(key=lambda x: (x["news_score"], x["news_hits"]), reverse=True)
    return out[:max_candidates]
