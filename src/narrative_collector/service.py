from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import websockets

from .config import Settings
from .providers import DexScreenerClient, TwitterApiIoClient
from .scoring import candidate_decision
from .storage import Launch, Migration, Store

LOGGER = logging.getLogger(__name__)


class Collector:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.dex = DexScreenerClient()
        self.social = TwitterApiIoClient(settings.twitterapi_io_key, settings.social_max_results_per_query)

    async def run(self) -> None:
        await asyncio.gather(
            self._run_stream("subscribeNewToken", "launch", self._handle_launch),
            self._run_stream("subscribeMigration", "migration", self._handle_migration),
        )

    async def _run_stream(self, method: str, label: str, handler) -> None:
        """Keep subscriptions separate so a PumpPortal `create` tx cannot be
        mistaken for both a token launch and a migration pool creation.
        """
        url = f"wss://pumpportal.fun/api/data?api-key={self.settings.pumpportal_api_key}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
                    await ws.send(json.dumps({"method": method}))
                    LOGGER.info("Connected to PumpPortal %s stream", label)
                    async for message in ws:
                        event = json.loads(message)
                        if isinstance(event, dict):
                            await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Connection recovery is intentional for a week-long process.
                LOGGER.warning("PumpPortal %s stream failed: %s; retrying in 10 seconds", label, exc)
                await asyncio.sleep(10)

    async def _handle_launch(self, event: dict) -> None:
        mint = _first_string(event, "mint", "mintAddress", "tokenAddress", "token")
        if not mint:
            LOGGER.debug("Ignored launch event with no mint: %s", event)
            return
        launch = Launch(
            mint=mint,
            launched_at=_event_time(event),
            creator=_first_string(event, "creator", "creatorPublicKey", "traderPublicKey", "user"),
            name=_first_string(event, "name"),
            symbol=_first_string(event, "symbol", "ticker"),
            raw=event,
        )
        if self.store.save_launch(launch):
            LOGGER.info("Launch stored: %s (%s)", mint, launch.symbol or "no-symbol")

    async def _handle_migration(self, event: dict) -> None:
        mint = _first_string(event, "mint", "mintAddress", "tokenAddress", "token")
        if not mint:
            LOGGER.debug("Ignored migration event with no mint: %s", event)
            return
        launch = self.store.launch_for_mint(mint)
        migration = Migration(
            mint=mint,
            observed_at=_event_time(event),
            signature=_first_string(event, "signature", "txSignature"),
            name=_first_string(event, "name") or (launch["name"] if launch else None),
            symbol=_first_string(event, "symbol", "ticker") or (launch["symbol"] if launch else None),
            raw=event,
        )
        if self.store.save_migration(migration):
            self.store.queue_forensics(mint)
            LOGGER.info("Migration stored: %s (%s)", mint, migration.symbol or "no-symbol")

    async def enrich_due_snapshots(self) -> int:
        pending = self.store.pending_snapshots(datetime.now(timezone.utc), self.settings.snapshot_windows_minutes,
                                               self.settings.enrich_max_jobs_per_run)
        processed = 0
        for job in pending:
            try:
                metrics = await asyncio.to_thread(self.dex.token_metrics, job["mint"])
                if not metrics:
                    finalized = self.store.save_snapshot_failure(
                        job["mint"], job["window_minutes"], "dexscreener_not_found", max_attempts=3
                    )
                    if finalized:
                        processed += 1
                    continue
                self.store.clear_snapshot_failures(job["mint"], job["window_minutes"])
                self.store.save_snapshot(job["mint"], job["window_minutes"], metrics)
                self.store.save_token_metadata(job["mint"], metrics.get("raw") or {})
                processed += 1
                if job["window_minutes"] == 5 and self.settings.social_collect_migration_baseline:
                    await self._collect_social_baseline(job)
                if job["window_minutes"] == 5 and self.settings.social_collect_candidates:
                    await self._maybe_collect_candidate_social(job, metrics, "candidate_5m")
                elif job["window_minutes"] == 60 and self.settings.social_collect_candidates:
                    # A second, precise CA query measures social momentum after migration.
                    await self._maybe_collect_candidate_social(job, metrics, "candidate_60m", mint_only=True)
            except Exception as exc:
                LOGGER.warning("Snapshot failed for %s: %s", job["mint"], exc)
        return processed

    async def _collect_social_baseline(self, migration: dict) -> None:
        """One exact CA search for every migrated token, bounded by the credit cap.

        Tweet timestamps are stored and classified relative to the launch and
        migration time, so a later report never treats post-migration hype as
        evidence that existed before migration.
        """
        if not self.settings.twitterapi_io_key or not self.settings.social_collect_migration_baseline:
            return
        await self._collect_social(migration, "migration_baseline", [migration["mint"]])

    async def _maybe_collect_candidate_social(self, migration: dict, metrics: dict, stage: str,
                                              mint_only: bool = False) -> None:
        if not self.settings.twitterapi_io_key:
            return
        decision = candidate_decision(
            metrics,
            self.settings.social_candidate_min_volume_usd,
            self.settings.social_candidate_min_buys,
        )
        if not decision.eligible_for_social:
            return
        queries = [migration["mint"]] if mint_only else _build_queries(migration.get("name"), migration.get("symbol"))
        await self._collect_social(migration, stage, queries)

    async def _collect_social(self, migration: dict, stage: str, queries: list[str]) -> None:
        if not queries:
            return
        today = datetime.now(timezone.utc).date()
        week = (today - timedelta(days=today.weekday())).isoformat()
        launch = self.store.launch_for_mint(migration["mint"])
        for query in queries:
            if self.store.social_search_seen(migration["mint"], stage, query):
                continue
            used = self.store.weekly_usage("twitterapi", week)
            reserve = self.settings.social_max_results_per_query * 15
            if (self.settings.social_weekly_credit_cap > 0 and
                    used + reserve > self.settings.social_weekly_credit_cap):
                LOGGER.warning("Social budget cap reached; skipping %s", migration["mint"])
                return
            tweets = await asyncio.to_thread(self.social.search, query)
            credits = max(15, len(tweets) * 15)
            self.store.save_social_search(
                migration["mint"], stage, query, tweets, credits,
                launch["launched_at"] if launch else None, migration["observed_at"],
                capped=len(tweets) >= min(self.settings.social_max_results_per_query, 20),
            )
            self.store.add_usage("twitterapi", week, credits)
            LOGGER.info("Social %s for %s: %d tweets (%d credits)", stage, migration["mint"], len(tweets), credits)


def _first_string(payload: dict, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _build_queries(name: str | None, symbol: str | None) -> list[str]:
    queries: list[str] = []
    if symbol and 2 <= len(symbol) <= 12:
        queries.append(f"${symbol} lang:en")
    if name and 2 <= len(name) <= 80:
        queries.append(f'"{name}" lang:en')
    return queries[:2]


def _event_time(payload: dict) -> str:
    for key in ("timestamp", "blockTime", "createdAt", "time"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    return datetime.now(timezone.utc).isoformat()
