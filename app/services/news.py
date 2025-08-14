import time
import math
from typing import Dict, List, Tuple
from datetime import datetime, timezone
import feedparser
import re

# Viac RSS zdrojov (bez kľúčov)
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://blog.coinbase.com/feed",
    "https://www.binance.com/en/blog/feed",
    "https://okx.com/learn/feeds/rss",
    "https://medium.com/feed/tag/crypto",
    # agregátory / novinky
    "https://cryptonews.com/news/feed",          # CryptoNews
    "https://cryptopotato.com/feed/",            # CryptoPotato
    "https://cryptoslate.com/feed/",             # CryptoSlate
    "https://www.theblock.co/rss",               # The Block
]

_word = re.compile(r"[A-Za-z0-9\-_.]+")
_cashtag = re.compile(r"\$[A-Za-z]{2,10}")  # napr. $SOL, $BTC

def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()

def _age_hours(published_ts: float) -> float:
    return max(0.0, (_now_ts() - published_ts) / 3600.0)

def _text_of(entry) -> str:
    parts = []
    if getattr(entry, "title", None): parts.append(entry.title)
    if getattr(entry, "summary", None): parts.append(entry.summary)
    return " ".join(parts)

def _published(entry) -> float:
    try:
        return time.mktime(entry.published_parsed)
    except Exception:
        return _now_ts()

def _tokenize_lower(s: str) -> List[str]:
    return [w.lower() for w in _word.findall(s.lower())]

def fetch_candidates_from_rss(
    markets: List[Dict],
    hours_back: int = 36,
    max_candidates: int = 12,
) -> List[Dict]:
    """
    markets: výstup z CoinGecko /coins/markets (TOP200), slúži na mapovanie názvov/symbolov.
    Výstup: [{id, symbol, name, news_hits, news_score}]
    """
    # mapy na rýchle párovanie
    by_symbol: Dict[str, Tuple[str, str]] = {}   # SYMBOL -> (id, name)
    by_name: Dict[str, Tuple[str, str]] = {}     # "solana" -> (id, "Solana")
    for m in markets:
        cid = m.get("id")
        sym = (m.get("symbol") or "").upper()
        name = (m.get("name") or "").strip()
        if not cid or not sym or not name:
            continue
        by_symbol[sym] = (cid, name)
        by_name[name.lower()] = (cid, name)

    hits: Dict[str, Dict] = {}  # id -> agg
    cutoff_h = float(hours_back)

    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                ts = _published(e)
                age_h = _age_hours(ts)
                if age_h > cutoff_h:
                    continue

                text = _text_of(e)
                toks = set(_tokenize_lower(text))
                tags = {t[1:].upper() for t in _cashtag.findall(text)}  # {$SOL, $BTC} -> {"SOL", "BTC"}

                matched: List[Tuple[str, str, str]] = []

                # 1) match cashtagov
                for sym in tags:
                    if sym in by_symbol:
                        cid, nm = by_symbol[sym]
                        matched.append((cid, sym, nm))

                # 2) match podľa symbolu ako celého slova (lowercase v toks)
                for sym, (cid, nm) in by_symbol.items():
                    if sym.lower() in toks:
                        matched.append((cid, sym, nm))

                # 3) match podľa mena (celé slovo; dĺžka > 3 kvôli bežným slovám)
                for nm_key, (cid, nm) in by_name.items():
                    if len(nm_key) > 3 and nm_key in toks:
                        matched.append((cid, by_symbol.get(nm.upper(), (sym,))[0] if nm.upper() in by_symbol else nm[:6].upper(), nm))

                # skóre: čerstvosť (exponenciálne), bonus za priamy cashtag
                freshness = math.exp(-age_h / 12.0)  # ~ polčas 8–12h
                base = 1.0 * freshness
                for cid, sym, nm in matched:
                    if cid not in hits:
                        hits[cid] = {"id": cid, "symbol": sym, "name": nm, "news_hits": 0, "news_score": 0.0}
                    hits[cid]["news_hits"] += 1
                    # +0.5 bonus ak bol cashtag (silnejšia relevancia)
                    bonus = 0.5 if sym in tags else 0.0
                    hits[cid]["news_score"] += base + bonus
        except Exception:
            # RSS niekedy zlyhá — ticho ignorujeme a ideme ďalej
            continue

    out = [v for v in hits.values() if v["news_hits"] >= 1]
    out.sort(key=lambda x: (x["news_score"], x["news_hits"]), reverse=True)
    return out[:max_candidates]
