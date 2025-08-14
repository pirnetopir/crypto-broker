from dataclasses import dataclass
from typing import List

@dataclass
class Pick:
    id: str
    symbol: str
    name: str
    price: float
    score: float
    weight: float
    mom_24h: float
    atr_pct: float
    spark: list  # posledných ~50 close (na mini graf)

@dataclass
class SignalPack:
    created_at: str
    regime: str
    picks: List[Pick]
    note: str
