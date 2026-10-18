from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Small dependency-free .env loader; existing environment always wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_path: Path
    pumpportal_api_key: str
    twitterapi_io_key: str
    bitquery_access_token: str
    helius_rpc_url: str
    social_weekly_credit_cap: int
    social_max_results_per_query: int
    social_candidate_min_volume_usd: float
    social_candidate_min_buys: int
    pumpportal_max_trade_events: int
    forensics_page_limit: int
    forensics_max_jobs_per_run: int
    social_collect_migration_baseline: bool
    social_collect_candidates: bool
    x_first_enabled: bool
    x_first_end_at: str
    x_first_run_id: str
    x_first_credit_cap: int
    x_first_queries_path: Path
    x_first_followup_sample_percent: int
    source_metadata_since: str
    source_metadata_end_at: str
    source_metadata_max_jobs_per_run: int
    enrich_max_jobs_per_run: int
    wallet_watch_signature_limit: int
    wallet_watch_detail_limit: int
    snapshot_windows_minutes: tuple[int, ...]
    log_level: str

    @classmethod
    def from_environment(cls, root: Path | None = None) -> "Settings":
        root = root or Path.cwd()
        load_dotenv(root / ".env")
        database_path = Path(os.getenv("DATABASE_PATH", "./data/collector.sqlite3"))
        if not database_path.is_absolute():
            database_path = root / database_path
        windows = tuple(
            int(value.strip())
            for value in os.getenv("SNAPSHOT_WINDOWS_MINUTES", "5,60,360,1440").split(",")
            if value.strip()
        )
        return cls(
            database_path=database_path,
            pumpportal_api_key=os.getenv("PUMPPORTAL_API_KEY", ""),
            twitterapi_io_key=os.getenv("TWITTERAPI_IO_KEY", ""),
            bitquery_access_token=os.getenv("BITQUERY_ACCESS_TOKEN", ""),
            helius_rpc_url=os.getenv("HELIUS_RPC_URL", ""),
            social_weekly_credit_cap=int(os.getenv("SOCIAL_WEEKLY_CREDIT_CAP", "0")),
            social_max_results_per_query=int(os.getenv("SOCIAL_MAX_RESULTS_PER_QUERY", "25")),
            social_candidate_min_volume_usd=float(os.getenv("SOCIAL_CANDIDATE_MIN_VOLUME_USD", "5000")),
            social_candidate_min_buys=int(os.getenv("SOCIAL_CANDIDATE_MIN_BUYS", "20")),
            pumpportal_max_trade_events=int(os.getenv("PUMPPORTAL_MAX_TRADE_EVENTS", "50000")),
            forensics_page_limit=int(os.getenv("FORENSICS_PAGE_LIMIT", "500")),
            forensics_max_jobs_per_run=int(os.getenv("FORENSICS_MAX_JOBS_PER_RUN", "1")),
            social_collect_migration_baseline=os.getenv("SOCIAL_COLLECT_MIGRATION_BASELINE", "true").lower()
            in {"1", "true", "yes", "on"},
            social_collect_candidates=os.getenv("SOCIAL_COLLECT_CANDIDATES", "true").lower()
            in {"1", "true", "yes", "on"},
            x_first_enabled=os.getenv("X_FIRST_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            x_first_end_at=os.getenv("X_FIRST_END_AT", ""),
            x_first_run_id=os.getenv("X_FIRST_RUN_ID", ""),
            x_first_credit_cap=int(os.getenv("X_FIRST_CREDIT_CAP", "0")),
            x_first_queries_path=root / os.getenv("X_FIRST_QUERIES_PATH", "x_first_queries.json"),
            x_first_followup_sample_percent=int(os.getenv("X_FIRST_FOLLOWUP_SAMPLE_PERCENT", "0")),
            source_metadata_since=os.getenv("SOURCE_METADATA_SINCE", ""),
            source_metadata_end_at=os.getenv("SOURCE_METADATA_END_AT", ""),
            source_metadata_max_jobs_per_run=int(os.getenv("SOURCE_METADATA_MAX_JOBS_PER_RUN", "10")),
            enrich_max_jobs_per_run=int(os.getenv("ENRICH_MAX_JOBS_PER_RUN", "200")),
            wallet_watch_signature_limit=int(os.getenv("WALLET_WATCH_SIGNATURE_LIMIT", "25")),
            wallet_watch_detail_limit=int(os.getenv("WALLET_WATCH_DETAIL_LIMIT", "10")),
            snapshot_windows_minutes=windows,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def require_live_keys(self) -> list[str]:
        missing: list[str] = []
        if not self.pumpportal_api_key:
            missing.append("PUMPPORTAL_API_KEY")
        if not self.twitterapi_io_key:
            missing.append("TWITTERAPI_IO_KEY")
        return missing
