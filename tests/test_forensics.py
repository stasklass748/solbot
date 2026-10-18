from narrative_collector.forensics import analyse_wallet_flow


def test_wallet_flow_marks_concentrated_and_recycled_flow() -> None:
    rows = [
        {"trade_time": "2026-01-01T00:00:00+00:00", "trade_id": "a", "signer": "a", "fee_payer": None, "side": "buy", "amount_usd": 100},
        {"trade_time": "2026-01-01T00:00:01+00:00", "trade_id": "b", "signer": "a", "fee_payer": None, "side": "sell", "amount_usd": 100},
        {"trade_time": "2026-01-01T00:00:02+00:00", "trade_id": "c", "signer": "b", "fee_payer": None, "side": "buy", "amount_usd": 1},
    ]
    result = analyse_wallet_flow(rows, creator="a")
    assert result["distinct_buyers"] == 2
    assert "single_wallet_volume_concentration" in result["risk_flags"]
    assert "creator_present_in_available_trade_tape" in result["risk_flags"]
