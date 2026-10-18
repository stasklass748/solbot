from narrative_collector.scoring import candidate_decision


def test_candidate_gate_requires_two_signals() -> None:
    decision = candidate_decision(
        {"volume_m5_usd": 10_000, "buys_m5": 25, "sells_m5": 10},
        min_volume_usd=5_000,
        min_buys=20,
    )
    assert decision.eligible_for_social


def test_candidate_gate_blocks_thin_flow() -> None:
    decision = candidate_decision(
        {"volume_m5_usd": 100, "buys_m5": 1, "sells_m5": 0},
        min_volume_usd=5_000,
        min_buys=20,
    )
    assert not decision.eligible_for_social
