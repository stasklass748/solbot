from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateDecision:
    eligible_for_social: bool
    reasons: tuple[str, ...]


def candidate_decision(metrics: dict, min_volume_usd: float, min_buys: int) -> CandidateDecision:
    """A conservative gate: social API is not called for every migration."""
    volume = metrics.get("volume_m5_usd") or 0.0
    buys = metrics.get("buys_m5") or 0
    sells = metrics.get("sells_m5") or 0
    reasons: list[str] = []
    if volume >= min_volume_usd:
        reasons.append("m5_volume")
    if buys >= min_buys:
        reasons.append("m5_buyers")
    if sells and buys >= sells * 1.2:
        reasons.append("buy_sell_balance")
    return CandidateDecision(eligible_for_social=len(reasons) >= 2, reasons=tuple(reasons))
