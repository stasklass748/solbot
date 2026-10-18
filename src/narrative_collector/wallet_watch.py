from __future__ import annotations

import logging

from .providers import HeliusClient
from .storage import Store

LOGGER = logging.getLogger(__name__)


class WalletWatchWorker:
    """Tracks public-address activity and records only observable token deltas.

    A positive delta is not automatically a purchase, and a negative delta is
    not automatically a sale; transfers and airdrops exist. The more precise
    buy/sell classification comes later when a delta is matched to DEX tape.
    """

    def __init__(self, store: Store, helius: HeliusClient):
        self.store = store
        self.helius = helius

    def process(self, signatures_per_wallet: int = 25, max_transaction_details: int = 10) -> tuple[int, int]:
        new_signatures = detailed = 0
        remaining = max_transaction_details
        for address, label in self.store.tracked_wallet_map().items():
            try:
                for item in self.helius.recent_signatures(address, signatures_per_wallet):
                    is_new = self.store.save_wallet_signature(address, item)
                    new_signatures += is_new
                    signature = item.get("signature")
                    if not isinstance(signature, str) or remaining <= 0:
                        continue
                    if not self.store.wallet_signature_needs_detail(address, signature):
                        continue
                    transaction = self.helius.transaction(signature)
                    if transaction:
                        self.store.save_wallet_movements(address, signature, extract_token_movements(address, transaction))
                    self.store.mark_wallet_signature_detailed(address, signature)
                    detailed += 1
                    remaining -= 1
                LOGGER.info("Wallet watch updated %s (%s)", label, address)
            except Exception as exc:
                LOGGER.warning("Wallet watch failed for %s: %s", label, exc)
        return new_signatures, detailed


def extract_token_movements(address: str, transaction: dict) -> list[dict]:
    meta = transaction.get("meta") or {}
    before = _balances_by_account(meta.get("preTokenBalances") or [])
    after = _balances_by_account(meta.get("postTokenBalances") or [])
    movements: list[dict] = []
    for key in set(before) | set(after):
        pre, post = before.get(key), after.get(key)
        entry = post or pre
        if not entry or entry.get("owner") != address:
            continue
        delta = _raw_amount(post) - _raw_amount(pre)
        if not delta:
            continue
        movements.append({
            "mint": entry.get("mint"), "delta_raw": delta,
            "decimals": (entry.get("uiTokenAmount") or {}).get("decimals"),
            "block_time": transaction.get("blockTime"),
        })
    return [item for item in movements if isinstance(item["mint"], str)]


def _balances_by_account(entries: list[dict]) -> dict[tuple[object, object], dict]:
    return {(entry.get("accountIndex"), entry.get("mint")): entry for entry in entries}


def _raw_amount(entry: dict | None) -> int:
    if not entry:
        return 0
    try:
        return int((entry.get("uiTokenAmount") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0
