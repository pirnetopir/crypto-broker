import os
from typing import List, Dict

# OpenAI je voliteľné; ak nie je kľúč, použijeme FREE pravidlá
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

def _free_rule_eval(item: Dict, regime: str) -> Dict:
    """FREE fallback: approve/veto + odhad horizontu."""
    # jednoduché pravidlá
    mom7 = float(item.get("mom_7d", 0.0))
    atrp = float(item.get("atr_pct", 0.0))
    vol = float(item.get("vol24", 0.0))
    hits = int(item.get("news_hits", 0))
    approve = (mom7 > 0 and atrp < 0.12 and vol > 2_000_000 and hits >= 1)
    # horizont
    if atrp >= 0.09 or float(item.get("mom_3h", 0.0)) > float(item.get("mom_24h", 0.0)) * 1.5:
        horiz = 0.5  # ~12 hodín
    elif atrp >= 0.06:
        horiz = 2.0  # ~2 dni
    else:
        horiz = 5.0 if regime == "risk-on" else 2.0
    rationale = f"momentum {mom7:+.1%}, ATR {atrp:.1%}, news_hits {hits}"
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
                "Rules of thumb: prefer positive 7d momentum, reasonable ATR (<0.12), decent 24h volume; "
                "if ATR high and momentum short-lived -> horizon under 1 day; otherwise 2–7 days. "
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
        # ak čokoľvek zlyhá, prepneme na free
        return [_free_rule_eval(it, regime) for it in items]

def evaluate_wildcards(items: List[Dict], regime: str) -> List[Dict]:
    """
    items: zoznam kandidátov s metrikami (price, vol24, mom_*, atr_pct, news_hits, news_score).
    Vráti doplnené polia ai_approve, ai_rationale, ai_horizon_days.
    """
    if not items:
        return []
    if not OPENAI_KEY or os.getenv("AI_WILDCARDS", "1") != "1":
        evals = [_free_rule_eval(it, regime) for it in items]
    else:
        evals = _with_openai(items, regime)

    out: List[Dict] = []
    for it, ev in zip(items, evals):
        z = dict(it)
        z["ai_approve"] = bool(ev.get("approve", False))
        z["ai_rationale"] = str(ev.get("rationale", ""))
        z["ai_horizon_days"] = float(ev.get("horizon_days", 2.0))
        out.append(z)
    return out
