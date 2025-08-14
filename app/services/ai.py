import os
from typing import List, Dict

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ---- nastaviteľné prahy pre FREE fallback (ENV s defaultmi) ----
def _envf(name: str, default: float) -> float:
    try: return float(os.getenv(name, str(default)))
    except: return float(default)

FREE_MIN_MOM7   = _envf("AI_FREE_MIN_MOM7",   -0.02)   # min 7d momentum (napr. -2 % povolené)
FREE_MAX_ATR    = _envf("AI_FREE_MAX_ATR",     0.15)   # max ATR% (0.15 = 15 %)
FREE_MIN_VOL    = _envf("AI_FREE_MIN_VOL",  1_000_000) # min 24h volume USD
FREE_MIN_HITS   = int(_envf("AI_FREE_MIN_HITS", 1.0))  # min počet news zásahov

def _free_rule_eval(item: Dict, regime: str) -> Dict:
    """FREE fallback: approve/veto + odhad horizontu."""
    mom7 = float(item.get("mom_7d", 0.0))
    atrp = float(item.get("atr_pct", 0.0))
    vol  = float(item.get("vol24", 0.0))
    hits = int(item.get("news_hits", 0))

    approve = (mom7 >= FREE_MIN_MOM7 and atrp <= FREE_MAX_ATR and vol >= FREE_MIN_VOL and hits >= FREE_MIN_HITS)

    # horizont: kratší pri vysokej volatilite, inak dlhší v risk-on
    if atrp >= 0.10:
        horiz = 0.5  # ~12 h
    elif atrp >= 0.07:
        horiz = 2.0  # ~2 dni
    else:
        horiz = 5.0 if regime == "risk-on" else 2.0

    rationale = f"7d momentum {mom7:+.1%}, ATR {atrp:.1%}, vol24 ${vol:,.0f}, news_hits {hits}"
    return {"approve": approve, "horizon_days": horiz, "rationale": rationale}

def _with_openai(items: List[Dict], regime: str) -> List[Dict]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)
        out: List[Dict] = []
        for it in items:
            content = (
                "You are a cautious crypto trading assistant. Decide approve or veto based only on the metrics.\n"
                "Return strict JSON: {\"approve\":true|false, \"horizon_days\": number, \"rationale\":\"<=40 words\"}.\n"
                f"Market regime: {regime}.\n"
                f"Metrics for {it['symbol']} ({it['name']}): "
                f"price={it['price']:.6f} USD, vol24={it['vol24']:.0f}, mom_3h={it.get('mom_3h',0):+.3f}, "
                f"mom_24h={it.get('mom_24h',0):+.3f}, mom_7d={it.get('mom_7d',0):+.3f}, "
                f"atr_pct={it.get('atr_pct',0):.3f}, news_hits={it.get('news_hits',0)}, news_score={it.get('news_score',0):.2f}. "
                "Rules: prefer positive 7d momentum, reasonable ATR (<0.15), decent volume; "
                "shorter horizon if volatility is high."
            )
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": content}],
                temperature=0.2,
                max_tokens=120,
                response_format={"type": "json_object"},
            )
            try:
                j = resp.choices[0].message.content
                import json
                parsed = json.loads(j)
                out.append({
                    "approve": bool(parsed.get("approve", False)),
                    "horizon_days": float(parsed.get("horizon_days", 2.0)),
                    "rationale": str(parsed.get("rationale", ""))[:180],
                })
            except Exception:
                out.append(_free_rule_eval(it, regime))
        return out
    except Exception:
        return [_free_rule_eval(it, regime) for it in items]

def evaluate_wildcards(items: List[Dict], regime: str) -> List[Dict]:
    if not items:
        return []
    use_llm = bool(OPENAI_KEY) and os.getenv("AI_WILDCARDS", "1") == "1"
    evals = _with_openai(items, regime) if use_llm else [_free_rule_eval(it, regime) for it in items]

    out: List[Dict] = []
    for it, ev in zip(items, evals):
        z = dict(it)
        z["ai_approve"] = bool(ev.get("approve", False))
        z["ai_rationale"] = str(ev.get("rationale", ""))
        z["ai_horizon_days"] = float(ev.get("horizon_days", 2.0))
        return out + [z]
    return out
