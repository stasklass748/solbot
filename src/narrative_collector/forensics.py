from __future__ import annotations

import logging
from collections import defaultdict

from .providers import BitqueryClient, HeliusClient
from .storage import Store

LOGGER = logging.getLogger(__name__)


class ForensicsWorker:
    """Turn available migrated-token trades into auditable flow signals.

    It never subscribes to trades for every launch. Returned trades are stored
    unchanged; a provider page-limit hit is explicitly labelled ``partial``.
    """

    def __init__(self, store: Store, client: BitqueryClient, page_limit: int = 500, helius: HeliusClient | None = None):
        self.store = store
        self.client = client
        self.page_limit = page_limit
        self.helius = helius

    def process(self, max_jobs: int) -> tuple[int, int, int]:
        completed = partial = failed = 0
        for job in self.store.next_forensics_jobs(max_jobs):
            mint = job["mint"]
            try:
                trades = self.client.token_trade_tape(mint, self.page_limit)
                inserted = self.store.save_raw_trades(mint, trades)
                launch = self.store.launch_for_mint(mint)
                creator = launch["creator"] if launch else None
                self.store.save_wallet_features(mint, creator, analyse_wallet_flow(self.store.raw_trades_for_mint(mint), creator))
                self.store.refresh_developer_profile(creator)
                self.store.save_smart_wallet_hits(mint, self.store.raw_trades_for_mint(mint), self.store.tracked_wallet_map())
                if self.helius:
                    try:
                        self.store.save_holder_snapshot(mint, "solana_top_20", self.helius.largest_token_accounts(mint))
                    except Exception as holder_error:
                        LOGGER.warning("Holder snapshot failed for %s: %s", mint, holder_error)
                status = "partial" if len(trades) >= self.page_limit else "completed"
                self.store.finish_forensics(mint, status, f"rows={len(trades)}, inserted={inserted}")
                completed += status == "completed"
                partial += status == "partial"
                LOGGER.info("Forensics %s: rows=%d inserted=%d status=%s", mint, len(trades), inserted, status)
            except Exception as exc:
                self.store.finish_forensics(mint, "failed", str(exc)[:500])
                failed += 1
                LOGGER.warning("Forensics failed for %s: %s", mint, exc)
        return completed, partial, failed


def analyse_wallet_flow(rows: list, creator: str | None) -> dict:
    """Flow signals only: no attempt to identify the people behind addresses."""
    volumes: dict[str, float] = defaultdict(float)
    buys: set[str] = set()
    sells: set[str] = set()
    early_buyers: set[str] = set()
    buy_volume = sell_volume = 0.0
    ordered = sorted(rows, key=lambda row: (row["trade_time"] or "", row["trade_id"]))
    for index, row in enumerate(ordered):
        wallet = row["signer"] or row["fee_payer"]
        amount = float(row["amount_usd"] or 0.0)
        if wallet:
            volumes[wallet] += amount
            if row["side"] == "buy":
                buys.add(wallet)
                if index < 100:
                    early_buyers.add(wallet)
            elif row["side"] == "sell":
                sells.add(wallet)
        if row["side"] == "buy":
            buy_volume += amount
        elif row["side"] == "sell":
            sell_volume += amount
    ranked = sorted(volumes.values(), reverse=True)
    total = sum(ranked)
    top_one = ranked[0] / total if total else None
    top_five = sum(ranked[:5]) / total if total else None
    rapid_flip_ratio = len(buys & sells) / len(buys) if buys else None
    creator_trades = sum(1 for row in rows if creator and (row["signer"] == creator or row["fee_payer"] == creator))
    flags: list[str] = []
    if rows and len(buys) < 15:
        flags.append("very_few_distinct_buyers")
    if top_one is not None and top_one > .35:
        flags.append("single_wallet_volume_concentration")
    if top_five is not None and top_five > .70:
        flags.append("top_five_volume_concentration")
    if rapid_flip_ratio is not None and rapid_flip_ratio > .50:
        flags.append("high_buyer_to_seller_overlap")
    if creator_trades:
        flags.append("creator_present_in_available_trade_tape")
    return {
        "available_trade_rows": len(rows), "distinct_traders": len(volumes), "distinct_buyers": len(buys),
        "distinct_sellers": len(sells), "early_buyers": len(early_buyers), "creator_trades": creator_trades,
        "top_trader_volume_share": top_one, "top_5_volume_share": top_five,
        "buy_volume_usd": buy_volume, "sell_volume_usd": sell_volume, "net_flow_usd": buy_volume - sell_volume,
        "rapid_flip_ratio": rapid_flip_ratio, "risk_flags": flags,
    }
