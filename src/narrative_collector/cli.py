from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .providers import BitqueryClient, HeliusClient
from .forensics import ForensicsWorker
from .reporting import write_report, write_smart_wallet_report
from .service import Collector
from .source_metadata import SourceMetadataWorker
from .storage import Store
from .wallet_watch import WalletWatchWorker
from .x_first import XFirstWorker
from .x_followup import XFollowupWorker


def parser() -> argparse.ArgumentParser:
    program = argparse.ArgumentParser(description="Pump.fun narrative research collector")
    program.add_argument("--env-dir", default=".", help="directory holding .env (default: current directory)")
    actions = program.add_subparsers(dest="command", required=True)
    actions.add_parser("init-db", help="create SQLite tables")
    actions.add_parser("status", help="show local collection counts")
    actions.add_parser("run", help="listen for PumpPortal migrations continuously")
    actions.add_parser("enrich", help="capture every due 5m/1h/6h/24h snapshot once")
    actions.add_parser("bitquery-probe", help="test the optional Bitquery token with a tiny query")
    forensics = actions.add_parser("forensics", help="fetch pre/post raw tape for queued migrated tokens")
    forensics.add_argument("--max-jobs", type=int, default=None, help="maximum migrated tokens per run")
    actions.add_parser("label", help="refresh outcome and risk labels from stored observations")
    watchlist = actions.add_parser("watchlist-import", help="import public wallet labels from a JSON array")
    watchlist.add_argument("--path", default="wallet_watchlist.json")
    actions.add_parser("watch-wallets", help="record new public-wallet signatures and a bounded set of parsed token deltas")
    actions.add_parser("x-first", help="collect a bounded independent sample of X posts before token selection")
    actions.add_parser("x-followup", help="recheck sampled X posts at 1h and 24h")
    sources = actions.add_parser("source-metadata", help="fetch IPFS launch descriptions for migrated tokens")
    sources.add_argument("--max-jobs", type=int, default=None)
    report = actions.add_parser("report", help="write a measured 5m-to-24h filter report")
    report.add_argument("--output-dir", default="./reports")
    wallet_report = actions.add_parser("smart-wallet-report", help="write observed tracked-wallet DEX participation report")
    wallet_report.add_argument("--output-dir", default="./reports")
    return program


def main() -> None:
    args = parser().parse_args()
    settings = Settings.from_environment(Path(args.env_dir))
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(settings.database_path)
    store.initialize()
    if args.command == "init-db":
        print(f"Database ready: {settings.database_path}")
        return
    if args.command == "status":
        print(json.dumps(store.summary(), indent=2))
        return
    if args.command == "bitquery-probe":
        if not settings.bitquery_access_token:
            sys.exit("BITQUERY_ACCESS_TOKEN is empty in .env")
        print(json.dumps(BitqueryClient(settings.bitquery_access_token).probe(), indent=2))
        return
    if args.command == "forensics":
        if not settings.bitquery_access_token:
            sys.exit("BITQUERY_ACCESS_TOKEN is empty in .env")
        helius = HeliusClient(settings.helius_rpc_url) if settings.helius_rpc_url else None
        completed, partial, failed = ForensicsWorker(
            store, BitqueryClient(settings.bitquery_access_token), settings.forensics_page_limit, helius
        ).process(args.max_jobs or settings.forensics_max_jobs_per_run)
        print(f"Forensics completed={completed}, partial={partial}, failed={failed}")
        return
    if args.command == "label":
        print(f"Labels refreshed: {store.update_labels()}")
        return
    if args.command == "watchlist-import":
        path = Path(args.path)
        if not path.is_absolute():
            path = Path(args.env_dir) / path
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit(f"Cannot read watchlist {path}: {exc}")
        if not isinstance(entries, list):
            sys.exit("Watchlist must be a JSON array")
        print(f"Watchlist imported: {store.import_watchlist(entries)}")
        return
    if args.command == "watch-wallets":
        if not settings.helius_rpc_url:
            sys.exit("HELIUS_RPC_URL is empty in .env")
        signatures, details = WalletWatchWorker(store, HeliusClient(settings.helius_rpc_url)).process(
            settings.wallet_watch_signature_limit, settings.wallet_watch_detail_limit
        )
        print(f"Wallet watch: new_signatures={signatures}, detailed_transactions={details}")
        return
    if args.command == "x-first":
        print(json.dumps(XFirstWorker(settings, store).process(), indent=2))
        return
    if args.command == "x-followup":
        print(json.dumps(XFollowupWorker(settings, store).process(), indent=2))
        return
    if args.command == "source-metadata":
        if not settings.source_metadata_since or not settings.source_metadata_end_at:
            sys.exit("SOURCE_METADATA_SINCE and SOURCE_METADATA_END_AT are required")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(
            settings.source_metadata_end_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc):
            print(json.dumps({"status": "expired", "processed": 0}))
            return
        print(json.dumps(SourceMetadataWorker(store).process(
            settings.source_metadata_since, args.max_jobs or settings.source_metadata_max_jobs_per_run
        ), indent=2))
        return
    if args.command == "report":
        path = write_report(store, settings, Path(args.output_dir))
        print(f"Report written: {path}")
        return
    if args.command == "smart-wallet-report":
        path = write_smart_wallet_report(store, Path(args.output_dir))
        print(f"Smart-wallet report written: {path}")
        return
    if args.command == "enrich":
        count = asyncio.run(Collector(settings, store).enrich_due_snapshots())
        print(f"Captured {count} snapshot(s)")
        return
    if args.command == "run":
        missing = ["PUMPPORTAL_API_KEY"] if not settings.pumpportal_api_key else []
        if missing:
            sys.exit("Missing required variables: " + ", ".join(missing))
        asyncio.run(Collector(settings, store).run())
