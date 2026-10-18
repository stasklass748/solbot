from __future__ import annotations

import json
import hashlib
import sqlite3
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Migration:
    mint: str
    observed_at: str
    signature: str | None = None
    name: str | None = None
    symbol: str | None = None
    raw: dict | None = None


@dataclass(frozen=True)
class Launch:
    mint: str
    launched_at: str
    creator: str | None = None
    name: str | None = None
    symbol: str | None = None
    raw: dict | None = None


class Store:
    """Raw data and reproducible derived features; no wallet identity inference."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS migrations (
                  mint TEXT PRIMARY KEY, observed_at TEXT NOT NULL, signature TEXT,
                  name TEXT, symbol TEXT, raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS launches (
                  mint TEXT PRIMARY KEY, launched_at TEXT NOT NULL, creator TEXT,
                  name TEXT, symbol TEXT, raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, window_minutes INTEGER NOT NULL,
                  captured_at TEXT NOT NULL, price_usd REAL, liquidity_usd REAL, volume_m5_usd REAL,
                  buys_m5 INTEGER, sells_m5 INTEGER, market_cap REAL, status TEXT NOT NULL DEFAULT 'ok',
                  volume_h1_usd REAL, volume_h6_usd REAL, volume_h24_usd REAL,
                  buys_h1 INTEGER, sells_h1 INTEGER, buys_h6 INTEGER, sells_h6 INTEGER,
                  buys_h24 INTEGER, sells_h24 INTEGER, raw_json TEXT NOT NULL,
                  UNIQUE(mint, window_minutes)
                );
                CREATE TABLE IF NOT EXISTS snapshot_attempts (
                  mint TEXT NOT NULL, window_minutes INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT, last_attempt_at TEXT NOT NULL, PRIMARY KEY(mint, window_minutes)
                );
                /* Kept so the original deployment is fully compatible. */
                CREATE TABLE IF NOT EXISTS social_observations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, query TEXT NOT NULL,
                  observed_at TEXT NOT NULL, returned_tweets INTEGER NOT NULL,
                  estimated_credits INTEGER NOT NULL, raw_json TEXT NOT NULL, UNIQUE(mint, query)
                );
                CREATE TABLE IF NOT EXISTS social_searches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, stage TEXT NOT NULL,
                  query TEXT NOT NULL, observed_at TEXT NOT NULL, returned_tweets INTEGER NOT NULL,
                  estimated_credits INTEGER NOT NULL, pre_launch_tweets INTEGER NOT NULL DEFAULT 0,
                  pre_migration_tweets INTEGER NOT NULL DEFAULT 0, post_migration_tweets INTEGER NOT NULL DEFAULT 0,
                  unique_authors INTEGER NOT NULL DEFAULT 0, suspicious_authors INTEGER NOT NULL DEFAULT 0,
                  is_capped INTEGER NOT NULL DEFAULT 0,
                  raw_json TEXT NOT NULL, UNIQUE(mint, stage, query)
                );
                CREATE TABLE IF NOT EXISTS api_usage (
                  period_start TEXT NOT NULL, provider TEXT NOT NULL, units INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL, PRIMARY KEY(period_start, provider)
                );
                CREATE TABLE IF NOT EXISTS forensics_jobs (
                  mint TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT, updated_at TEXT NOT NULL, sample_group TEXT NOT NULL DEFAULT 'candidate'
                );
                CREATE TABLE IF NOT EXISTS raw_trades (
                  trade_id TEXT PRIMARY KEY, mint TEXT NOT NULL, trade_time TEXT, signature TEXT,
                  signer TEXT, fee_payer TEXT, side TEXT, price_usd REAL, amount_usd REAL,
                  dex_protocol TEXT, raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS raw_trades_mint_time ON raw_trades(mint, trade_time);
                CREATE INDEX IF NOT EXISTS raw_trades_signer ON raw_trades(signer);
                CREATE TABLE IF NOT EXISTS wallet_features (
                  mint TEXT PRIMARY KEY, analysed_at TEXT NOT NULL, creator TEXT,
                  distinct_traders INTEGER NOT NULL, distinct_buyers INTEGER NOT NULL,
                  distinct_sellers INTEGER NOT NULL, early_buyers INTEGER NOT NULL, creator_trades INTEGER NOT NULL,
                  top_trader_volume_share REAL, top_5_volume_share REAL, buy_volume_usd REAL NOT NULL,
                  sell_volume_usd REAL NOT NULL, net_flow_usd REAL NOT NULL, rapid_flip_ratio REAL,
                  risk_flags_json TEXT NOT NULL, raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS token_labels (
                  mint TEXT PRIMARY KEY, labelled_at TEXT NOT NULL, liquidity_retained_1h INTEGER,
                  liquidity_retained_24h INTEGER, multiple_1h REAL, multiple_24h REAL,
                  outcome TEXT, risk_level TEXT, raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS developer_profiles (
                  creator TEXT PRIMARY KEY, analysed_at TEXT NOT NULL, launches_seen INTEGER NOT NULL,
                  migrations_seen INTEGER NOT NULL, winners_seen INTEGER NOT NULL, failures_seen INTEGER NOT NULL,
                  high_risk_tokens_seen INTEGER NOT NULL, raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holder_snapshots (
                  mint TEXT NOT NULL, captured_at TEXT NOT NULL, source TEXT NOT NULL,
                  account_count INTEGER NOT NULL, raw_json TEXT NOT NULL, PRIMARY KEY(mint, source)
                );
                CREATE TABLE IF NOT EXISTS tracked_wallets (
                  address TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                  added_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wallet_signatures (
                  address TEXT NOT NULL, signature TEXT NOT NULL, slot INTEGER, block_time INTEGER,
                  err_json TEXT, observed_at TEXT NOT NULL, detailed_at TEXT, raw_json TEXT NOT NULL,
                  PRIMARY KEY(address, signature)
                );
                CREATE TABLE IF NOT EXISTS wallet_token_movements (
                  address TEXT NOT NULL, signature TEXT NOT NULL, mint TEXT NOT NULL, delta_raw TEXT NOT NULL,
                  decimals INTEGER, block_time INTEGER, raw_json TEXT NOT NULL,
                  PRIMARY KEY(address, signature, mint)
                );
                CREATE INDEX IF NOT EXISTS wallet_movements_mint ON wallet_token_movements(mint);
                CREATE TABLE IF NOT EXISTS smart_wallet_hits (
                  mint TEXT NOT NULL, address TEXT NOT NULL, label TEXT NOT NULL, first_seen TEXT,
                  buy_trades INTEGER NOT NULL, sell_trades INTEGER NOT NULL, buy_volume_usd REAL NOT NULL,
                  sell_volume_usd REAL NOT NULL, PRIMARY KEY(mint, address)
                );
                CREATE TABLE IF NOT EXISTS token_metadata (
                  mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, image_url TEXT,
                  websites_json TEXT NOT NULL, socials_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL, raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS launch_source_metadata (
                  mint TEXT PRIMARY KEY, launch_uri TEXT NOT NULL, cid TEXT,
                  fetched_at TEXT NOT NULL, status TEXT NOT NULL,
                  description TEXT, source_post_id TEXT, source_post_url TEXT,
                  raw_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS migrations_observed_at ON migrations(observed_at);
                CREATE TABLE IF NOT EXISTS x_first_searches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                  query_label TEXT NOT NULL, query TEXT NOT NULL,
                  observed_at TEXT NOT NULL, returned_tweets INTEGER NOT NULL,
                  estimated_credits INTEGER NOT NULL, is_capped INTEGER NOT NULL,
                  oldest_post_at TEXT, newest_post_at TEXT, response_meta_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS x_first_searches_run_time ON x_first_searches(run_id, observed_at);
                CREATE TABLE IF NOT EXISTS x_first_posts (
                  post_id TEXT PRIMARY KEY, created_at TEXT, first_observed_at TEXT NOT NULL,
                  last_observed_at TEXT NOT NULL, text TEXT, author_id TEXT,
                  author_username TEXT, lang TEXT, raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS x_first_posts_created ON x_first_posts(created_at);
                CREATE TABLE IF NOT EXISTS x_watchlist_hits (
                  post_id TEXT PRIMARY KEY, handle TEXT NOT NULL, observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS x_first_post_observations (
                  search_id INTEGER NOT NULL, post_id TEXT NOT NULL,
                  observed_at TEXT NOT NULL, likes INTEGER, retweets INTEGER,
                  replies INTEGER, quotes INTEGER, views INTEGER,
                  PRIMARY KEY(search_id, post_id)
                );
                CREATE TABLE IF NOT EXISTS x_followup_cohort (
                  post_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sampled_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS x_followup_cohort_run ON x_followup_cohort(run_id);
                CREATE TABLE IF NOT EXISTS x_followup_observations (
                  post_id TEXT NOT NULL, window_minutes INTEGER NOT NULL,
                  captured_at TEXT NOT NULL, status TEXT NOT NULL,
                  likes INTEGER, retweets INTEGER, replies INTEGER, quotes INTEGER, views INTEGER,
                  raw_json TEXT NOT NULL, PRIMARY KEY(post_id, window_minutes)
                );
                """
            )
            self._add_column_if_missing(conn, "raw_trades", "fee_payer TEXT")
            self._add_column_if_missing(conn, "raw_trades", "dex_protocol TEXT")
            self._add_column_if_missing(conn, "wallet_signatures", "detailed_at TEXT")
            for definition in (
                "status TEXT NOT NULL DEFAULT 'ok'", "volume_h1_usd REAL", "volume_h6_usd REAL",
                "volume_h24_usd REAL", "buys_h1 INTEGER", "sells_h1 INTEGER", "buys_h6 INTEGER",
                "sells_h6 INTEGER", "buys_h24 INTEGER", "sells_h24 INTEGER",
            ):
                self._add_column_if_missing(conn, "snapshots", definition)
            self._add_column_if_missing(conn, "social_searches", "is_capped INTEGER NOT NULL DEFAULT 0")
            if self._add_column_if_missing(conn, "forensics_jobs", "sample_group TEXT NOT NULL DEFAULT 'candidate'"):
                # Only backfill when the column is first introduced. Rewriting
                # thousands of jobs on every CLI invocation overwhelmed the VPS.
                for row in conn.execute("SELECT mint FROM forensics_jobs WHERE attempts=0").fetchall():
                    conn.execute("UPDATE forensics_jobs SET sample_group=? WHERE mint=?",
                                 (_sample_group(row["mint"]), row["mint"]))

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, definition: str) -> bool:
        column = definition.split()[0]
        if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            return True
        return False

    def save_launch(self, launch: Launch) -> bool:
        with self.connection() as conn:
            result = conn.execute(
                "INSERT OR IGNORE INTO launches VALUES (?, ?, ?, ?, ?, ?)",
                (launch.mint, launch.launched_at, launch.creator, launch.name, launch.symbol,
                 json.dumps(launch.raw or {}, ensure_ascii=False)),
            )
            return result.rowcount == 1

    def launch_for_mint(self, mint: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM launches WHERE mint=?", (mint,)).fetchone()

    def pending_launch_sources(self, since: str, limit: int) -> list[sqlite3.Row]:
        """Fetch only migrated launches in the current bounded research run."""
        retry_before = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        with self.connection() as conn:
            return conn.execute("""SELECT l.mint,l.raw_json FROM migrations m
                JOIN launches l ON l.mint=m.mint
                LEFT JOIN launch_source_metadata x ON x.mint=l.mint
                WHERE m.observed_at>=? AND (x.mint IS NULL OR
                  (x.status='fetch_error' AND x.attempts<3 AND x.fetched_at<=?))
                ORDER BY (x.mint IS NOT NULL), m.observed_at LIMIT ?""",
                (since, retry_before, max(0, limit))).fetchall()

    def save_launch_source(self, mint: str, uri: str, cid: str | None, status: str,
                           description: str | None, source_post_id: str | None,
                           source_post_url: str | None, raw: dict) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT INTO launch_source_metadata
                (mint,launch_uri,cid,fetched_at,status,description,source_post_id,source_post_url,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mint) DO UPDATE SET fetched_at=excluded.fetched_at,
                  status=excluded.status,description=excluded.description,
                  source_post_id=excluded.source_post_id,source_post_url=excluded.source_post_url,
                  raw_json=excluded.raw_json,attempts=launch_source_metadata.attempts+1""",
                (mint, uri, cid, utc_now(), status, description, source_post_id,
                 source_post_url, json.dumps(raw, ensure_ascii=False)))

    def save_migration(self, migration: Migration) -> bool:
        with self.connection() as conn:
            result = conn.execute(
                "INSERT OR IGNORE INTO migrations VALUES (?, ?, ?, ?, ?, ?)",
                (migration.mint, migration.observed_at, migration.signature, migration.name, migration.symbol,
                 json.dumps(migration.raw or {}, ensure_ascii=False)),
            )
            return result.rowcount == 1

    def migration_for_mint(self, mint: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM migrations WHERE mint=?", (mint,)).fetchone()

    def queue_forensics(self, mint: str) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT OR IGNORE INTO forensics_jobs(mint,status,updated_at,sample_group)
                         VALUES (?, 'queued', ?, ?)""", (mint, utc_now(), _sample_group(mint)))

    def next_forensics_jobs(self, limit: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            processed = conn.execute(
                "SELECT COUNT(*) FROM forensics_jobs WHERE status IN ('completed','partial')"
            ).fetchone()[0]
            preferred = "control" if processed % 5 == 4 else "candidate"
            return conn.execute("""SELECT f.* FROM forensics_jobs f
                LEFT JOIN snapshots s ON s.mint=f.mint AND s.window_minutes=5
                WHERE f.status='queued'
                ORDER BY (f.sample_group=?) DESC,
                         EXISTS(SELECT 1 FROM smart_wallet_hits h WHERE h.mint=f.mint) DESC,
                         (s.status='ok') DESC, COALESCE(s.volume_m5_usd,0) DESC,
                         COALESCE(s.buys_m5,0) DESC, f.updated_at ASC LIMIT ?""",
                (preferred, limit)).fetchall()

    def save_raw_trades(self, mint: str, trades: list[dict]) -> int:
        inserted = 0
        with self.connection() as conn:
            for index, item in enumerate(trades):
                transaction, trade = item.get("Transaction") or {}, item.get("Trade") or {}
                signature = transaction.get("Signature") or f"unknown-{index}"
                buy, sell = trade.get("Buy") or {}, trade.get("Sell") or {}
                is_buy = (buy.get("Currency") or {}).get("MintAddress") == mint
                leg = buy if is_buy else sell
                result = conn.execute(
                    """INSERT OR IGNORE INTO raw_trades
                    (trade_id,mint,trade_time,signature,signer,fee_payer,side,price_usd,amount_usd,dex_protocol,raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (f"{signature}:{trade.get('Index', index)}", mint, (item.get("Block") or {}).get("Time"),
                     signature, transaction.get("Signer"), transaction.get("FeePayer"), "buy" if is_buy else "sell",
                     _float(leg.get("PriceInUSD")), _float(leg.get("AmountInUSD")),
                     (trade.get("Dex") or {}).get("ProtocolName"), json.dumps(item, ensure_ascii=False)),
                )
                inserted += result.rowcount
        return inserted

    def raw_trades_for_mint(self, mint: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM raw_trades WHERE mint=? ORDER BY trade_time, trade_id", (mint,)).fetchall()

    def finish_forensics(self, mint: str, status: str, detail: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute("""UPDATE forensics_jobs SET status=?, attempts=attempts+1, last_error=?, updated_at=?
                         WHERE mint=?""", (status, detail, utc_now(), mint))

    def save_wallet_features(self, mint: str, creator: str | None, features: dict) -> None:
        with self.connection() as conn:
            columns = ("mint,analysed_at,creator,distinct_traders,distinct_buyers,distinct_sellers,early_buyers,"
                       "creator_trades,top_trader_volume_share,top_5_volume_share,buy_volume_usd,sell_volume_usd,"
                       "net_flow_usd,rapid_flip_ratio,risk_flags_json,raw_json")
            values = (mint, utc_now(), creator, features["distinct_traders"], features["distinct_buyers"],
                      features["distinct_sellers"], features["early_buyers"], features["creator_trades"],
                      features["top_trader_volume_share"], features["top_5_volume_share"], features["buy_volume_usd"],
                      features["sell_volume_usd"], features["net_flow_usd"], features["rapid_flip_ratio"],
                      json.dumps(features["risk_flags"], ensure_ascii=False), json.dumps(features, ensure_ascii=False))
            conn.execute(f"INSERT OR REPLACE INTO wallet_features ({columns}) VALUES ({','.join('?' for _ in values)})", values)

    def refresh_developer_profile(self, creator: str | None) -> None:
        """Profile only behaviour visible inside our own collected dataset."""
        if not creator:
            return
        with self.connection() as conn:
            row = conn.execute("""SELECT COUNT(*) launches_seen, SUM(m.mint IS NOT NULL) migrations_seen,
                SUM(t.outcome='winner') winners_seen, SUM(t.outcome='failed') failures_seen,
                SUM(t.risk_level='high') high_risk_tokens_seen
                FROM launches l LEFT JOIN migrations m ON m.mint=l.mint
                LEFT JOIN token_labels t ON t.mint=l.mint WHERE l.creator=?""", (creator,)).fetchone()
            payload = {key: int(row[key] or 0) for key in row.keys()}
            conn.execute("INSERT OR REPLACE INTO developer_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (creator, utc_now(), payload["launches_seen"], payload["migrations_seen"],
                          payload["winners_seen"], payload["failures_seen"], payload["high_risk_tokens_seen"],
                          json.dumps(payload, ensure_ascii=False)))

    def save_holder_snapshot(self, mint: str, source: str, accounts: list[dict]) -> None:
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO holder_snapshots VALUES (?, ?, ?, ?, ?)",
                         (mint, utc_now(), source, len(accounts), json.dumps(accounts, ensure_ascii=False)))

    def import_watchlist(self, entries: list[dict]) -> int:
        imported = 0
        with self.connection() as conn:
            for item in entries:
                address = item.get("address") if isinstance(item, dict) else None
                label = item.get("label") if isinstance(item, dict) else None
                if not isinstance(address, str) or not isinstance(label, str) or not address or not label:
                    continue
                cursor = conn.execute("""INSERT INTO tracked_wallets(address,label,enabled,added_at) VALUES (?, ?, 1, ?)
                    ON CONFLICT(address) DO UPDATE SET label=excluded.label,enabled=1""", (address, label, utc_now()))
                imported += cursor.rowcount
        return imported

    def tracked_wallet_map(self) -> dict[str, str]:
        with self.connection() as conn:
            return {row["address"]: row["label"] for row in conn.execute(
                "SELECT address,label FROM tracked_wallets WHERE enabled=1"
            ).fetchall()}

    def save_wallet_signature(self, address: str, item: dict) -> bool:
        signature = item.get("signature")
        if not isinstance(signature, str) or not signature:
            return False
        with self.connection() as conn:
            cursor = conn.execute("""INSERT OR IGNORE INTO wallet_signatures
                (address,signature,slot,block_time,err_json,observed_at,raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (address, signature, item.get("slot"), item.get("blockTime"), json.dumps(item.get("err")),
                 utc_now(), json.dumps(item, ensure_ascii=False)))
            return cursor.rowcount == 1

    def wallet_signature_needs_detail(self, address: str, signature: str) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT detailed_at FROM wallet_signatures WHERE address=? AND signature=?",
                               (address, signature)).fetchone()
            return bool(row) and row["detailed_at"] is None

    def mark_wallet_signature_detailed(self, address: str, signature: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE wallet_signatures SET detailed_at=? WHERE address=? AND signature=?",
                         (utc_now(), address, signature))

    def save_wallet_movements(self, address: str, signature: str, movements: list[dict]) -> None:
        with self.connection() as conn:
            for movement in movements:
                conn.execute("""INSERT OR REPLACE INTO wallet_token_movements
                    (address,signature,mint,delta_raw,decimals,block_time,raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (address, signature, movement["mint"], str(movement["delta_raw"]), movement.get("decimals"),
                     movement.get("block_time"), json.dumps(movement, ensure_ascii=False)))

    def save_smart_wallet_hits(self, mint: str, rows: list, watchlist: dict[str, str]) -> None:
        hits: dict[str, dict] = {}
        for row in rows:
            address = row["signer"] or row["fee_payer"]
            if not address or address not in watchlist:
                continue
            hit = hits.setdefault(address, {"buy_trades": 0, "sell_trades": 0, "buy_volume_usd": 0.0,
                                             "sell_volume_usd": 0.0, "first_seen": row["trade_time"]})
            amount = float(row["amount_usd"] or 0.0)
            if row["side"] == "buy":
                hit["buy_trades"] += 1
                hit["buy_volume_usd"] += amount
            else:
                hit["sell_trades"] += 1
                hit["sell_volume_usd"] += amount
        with self.connection() as conn:
            for address, hit in hits.items():
                conn.execute("INSERT OR REPLACE INTO smart_wallet_hits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (mint, address, watchlist[address], hit["first_seen"], hit["buy_trades"], hit["sell_trades"],
                              hit["buy_volume_usd"], hit["sell_volume_usd"]))

    def pending_snapshots(self, now: datetime, windows: tuple[int, ...], limit: int = 200) -> list[dict]:
        with self.connection() as conn:
            migrations = conn.execute("SELECT * FROM migrations ORDER BY observed_at").fetchall()
            present = {(item["mint"], item["window_minutes"])
                       for item in conn.execute("SELECT mint,window_minutes FROM snapshots").fetchall()}
        pending: list[dict] = []
        for migration in migrations:
            observed = datetime.fromisoformat(migration["observed_at"])
            for minutes in windows:
                if (migration["mint"], minutes) not in present and now >= observed + timedelta(minutes=minutes):
                    pending.append(dict(migration) | {"window_minutes": minutes,
                                                       "due_at": observed + timedelta(minutes=minutes)})
        # Process snapshots closest to their intended time first. Old backlog remains
        # eligible, but cannot delay all fresh 5m observations on a small VPS.
        pending.sort(key=lambda item: item["due_at"], reverse=True)
        return pending[:max(0, limit)]

    def save_x_first_search(self, run_id: str, label: str, query: str, payload: dict,
                            credits: int, observed_at: str, followup_sample_percent: int = 0) -> tuple[int, int]:
        tweets = [item for item in payload.get("tweets", []) if isinstance(item, dict)]
        dated = [value for item in tweets if (value := _parse_time(item.get("createdAt") or item.get("created_at")))]
        meta = {key: value for key, value in payload.items() if key not in {"tweets", "data", "results"}}
        with self.connection() as conn:
            result = conn.execute("""INSERT INTO x_first_searches
                (run_id,query_label,query,observed_at,returned_tweets,estimated_credits,is_capped,
                 oldest_post_at,newest_post_at,response_meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, label, query, observed_at, len(tweets), credits,
                 int(bool(payload.get("has_next_page") or payload.get("next_cursor"))),
                 min(dated).isoformat() if dated else None,
                 max(dated).isoformat() if dated else None,
                 json.dumps(meta, ensure_ascii=False)))
            search_id = result.lastrowid
            inserted = 0
            for item in tweets:
                post_id = str(item.get("id") or item.get("id_str") or "")
                if not post_id:
                    continue
                author = item.get("author") or item.get("user") or {}
                if not isinstance(author, dict):
                    author = {}
                created = _parse_time(item.get("createdAt") or item.get("created_at"))
                result = conn.execute("""INSERT OR IGNORE INTO x_first_posts
                    (post_id,created_at,first_observed_at,last_observed_at,text,author_id,
                     author_username,lang,raw_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (post_id, created.isoformat() if created else None, observed_at, observed_at,
                     item.get("text") or item.get("full_text"),
                     str(author.get("id") or author.get("userId") or item.get("authorId") or ""),
                     author.get("userName") or author.get("screen_name"), item.get("lang"),
                     json.dumps(item, ensure_ascii=False)))
                inserted += result.rowcount
                source_handle = item.get("watchlist_source")
                if isinstance(source_handle, str) and source_handle:
                    conn.execute("INSERT OR IGNORE INTO x_watchlist_hits VALUES (?,?,?)",
                                 (post_id, source_handle, observed_at))
                if (result.rowcount == 1 and followup_sample_percent > 0 and
                        int.from_bytes(hashlib.sha256(post_id.encode("utf-8")).digest()[:4], "big") % 100
                        < followup_sample_percent):
                    conn.execute("""INSERT OR IGNORE INTO x_followup_cohort(post_id,run_id,sampled_at)
                        VALUES (?,?,?)""", (post_id, run_id, observed_at))
                conn.execute("UPDATE x_first_posts SET last_observed_at=? WHERE post_id=?", (observed_at, post_id))
                conn.execute("""INSERT OR IGNORE INTO x_first_post_observations
                    (search_id,post_id,observed_at,likes,retweets,replies,quotes,views)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (search_id, post_id, observed_at,
                     _int(item.get("likeCount")), _int(item.get("retweetCount")),
                     _int(item.get("replyCount")), _int(item.get("quoteCount")),
                     _int(item.get("viewCount"))))
            return len(tweets), inserted

    def last_x_first_search_at(self, run_id: str, label: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("""SELECT MAX(observed_at) FROM x_first_searches
                WHERE run_id=? AND query_label=?""", (run_id, label)).fetchone()
            return row[0] if row else None

    def due_x_followups(self, run_id: str, now: str, limit: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("""SELECT c.post_id,p.first_observed_at,w.window_minutes
                FROM x_followup_cohort c JOIN x_first_posts p ON p.post_id=c.post_id
                CROSS JOIN (SELECT 60 AS window_minutes UNION ALL SELECT 1440) w
                LEFT JOIN x_followup_observations f
                  ON f.post_id=c.post_id AND f.window_minutes=w.window_minutes
                WHERE c.run_id=? AND f.post_id IS NULL
                  AND julianday(p.first_observed_at)+w.window_minutes/1440.0<=julianday(?)
                ORDER BY julianday(p.first_observed_at)+w.window_minutes/1440.0 DESC
                LIMIT ?""", (run_id, now, max(0, limit))).fetchall()

    def save_x_followup(self, post_id: str, window_minutes: int, captured_at: str,
                        status: str, tweet: dict | None) -> None:
        item = tweet or {}
        with self.connection() as conn:
            conn.execute("""INSERT OR IGNORE INTO x_followup_observations
                (post_id,window_minutes,captured_at,status,likes,retweets,replies,quotes,views,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (post_id, window_minutes, captured_at, status, _int(item.get("likeCount")),
                 _int(item.get("retweetCount")), _int(item.get("replyCount")),
                 _int(item.get("quoteCount")), _int(item.get("viewCount")),
                 json.dumps(item, ensure_ascii=False)))

    def save_snapshot(self, mint: str, window_minutes: int, metrics: dict) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT OR REPLACE INTO snapshots
              (mint,window_minutes,captured_at,price_usd,liquidity_usd,volume_m5_usd,buys_m5,sells_m5,market_cap,
               status,volume_h1_usd,volume_h6_usd,volume_h24_usd,buys_h1,sells_h1,buys_h6,sells_h6,
               buys_h24,sells_h24,raw_json)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (mint, window_minutes, utc_now(), metrics.get("price_usd"), metrics.get("liquidity_usd"),
               metrics.get("volume_m5_usd"), metrics.get("buys_m5"), metrics.get("sells_m5"),
               metrics.get("market_cap"), metrics.get("volume_h1_usd"), metrics.get("volume_h6_usd"),
               metrics.get("volume_h24_usd"), metrics.get("buys_h1"), metrics.get("sells_h1"),
               metrics.get("buys_h6"), metrics.get("sells_h6"), metrics.get("buys_h24"),
               metrics.get("sells_h24"), json.dumps(metrics.get("raw", {}), ensure_ascii=False)))

    def save_snapshot_failure(self, mint: str, window_minutes: int, error: str, max_attempts: int = 3) -> bool:
        """After repeated provider misses, preserve the missing/dead outcome instead of dropping it."""
        with self.connection() as conn:
            conn.execute("""INSERT INTO snapshot_attempts(mint,window_minutes,attempts,last_error,last_attempt_at)
                VALUES (?, ?, 1, ?, ?) ON CONFLICT(mint,window_minutes) DO UPDATE SET
                attempts=attempts+1,last_error=excluded.last_error,last_attempt_at=excluded.last_attempt_at""",
                (mint, window_minutes, error, utc_now()))
            attempts = conn.execute("SELECT attempts FROM snapshot_attempts WHERE mint=? AND window_minutes=?",
                                    (mint, window_minutes)).fetchone()[0]
            if attempts < max_attempts:
                return False
            conn.execute("""INSERT OR REPLACE INTO snapshots
                (mint,window_minutes,captured_at,status,raw_json) VALUES (?, ?, ?, 'not_found', ?)""",
                (mint, window_minutes, utc_now(), json.dumps({"error": error, "attempts": attempts})))
            return True

    def clear_snapshot_failures(self, mint: str, window_minutes: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM snapshot_attempts WHERE mint=? AND window_minutes=?", (mint, window_minutes))

    def save_token_metadata(self, mint: str, pair: dict) -> None:
        base = pair.get("baseToken") or {}
        info = pair.get("info") or {}
        with self.connection() as conn:
            conn.execute("""INSERT OR REPLACE INTO token_metadata
              (mint,name,symbol,image_url,websites_json,socials_json,updated_at,raw_json)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (mint, base.get("name"), base.get("symbol"), info.get("imageUrl"),
               json.dumps(info.get("websites") or [], ensure_ascii=False),
               json.dumps(info.get("socials") or [], ensure_ascii=False), utc_now(),
               json.dumps({"baseToken": base, "info": info}, ensure_ascii=False)))
            conn.execute("""UPDATE migrations SET name=COALESCE(name,?),symbol=COALESCE(symbol,?) WHERE mint=?""",
                         (base.get("name"), base.get("symbol"), mint))

    def social_search_seen(self, mint: str, stage: str, query: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM social_searches WHERE mint=? AND stage=? AND query=?",
                                (mint, stage, query)).fetchone() is not None

    def save_social_search(self, mint: str, stage: str, query: str, tweets: list[dict], credits: int,
                           launch_time: str | None, migration_time: str | None, capped: bool = False) -> None:
        stats = _social_stats(tweets, launch_time, migration_time)
        with self.connection() as conn:
            conn.execute("""INSERT OR IGNORE INTO social_searches
              (mint,stage,query,observed_at,returned_tweets,estimated_credits,pre_launch_tweets,pre_migration_tweets,
               post_migration_tweets,unique_authors,suspicious_authors,is_capped,raw_json)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (mint, stage, query, utc_now(), len(tweets), credits, stats["pre_launch_tweets"],
               stats["pre_migration_tweets"], stats["post_migration_tweets"], stats["unique_authors"],
               stats["suspicious_authors"], int(capped), json.dumps(tweets, ensure_ascii=False)))

    def weekly_usage(self, provider: str, week_start: str) -> int:
        with self.connection() as conn:
            item = conn.execute("SELECT units FROM api_usage WHERE period_start=? AND provider=?", (week_start, provider)).fetchone()
            return int(item["units"]) if item else 0

    def add_usage(self, provider: str, week_start: str, units: int) -> None:
        with self.connection() as conn:
            conn.execute("""INSERT INTO api_usage(period_start,provider,units,updated_at) VALUES (?, ?, ?, ?)
              ON CONFLICT(period_start,provider) DO UPDATE SET units=units+excluded.units,updated_at=excluded.updated_at""",
              (week_start, provider, units, utc_now()))

    def update_labels(self) -> int:
        labels = 0
        creators: set[str] = set()
        with self.connection() as conn:
            for row in conn.execute("SELECT mint FROM migrations").fetchall():
                mint = row["mint"]
                snapshots = {s["window_minutes"]: s for s in conn.execute("SELECT * FROM snapshots WHERE mint=?", (mint,))}
                s5, s60, s1440 = snapshots.get(5), snapshots.get(60), snapshots.get(1440)
                if not s5:
                    continue
                m1h = _ratio(_float(s60["price_usd"]) if s60 else None, _float(s5["price_usd"]))
                m24h = _ratio(_float(s1440["price_usd"]) if s1440 else None, _float(s5["price_usd"]))
                l1h = _retained(_float(s5["liquidity_usd"]), _float(s60["liquidity_usd"]) if s60 else None)
                l24h = _retained(_float(s5["liquidity_usd"]), _float(s1440["liquidity_usd"]) if s1440 else None)
                wallet = conn.execute("SELECT risk_flags_json FROM wallet_features WHERE mint=?", (mint,)).fetchone()
                flags = json.loads(wallet["risk_flags_json"]) if wallet else []
                late_missing = bool(s1440 and s1440["status"] == "not_found")
                outcome = "failed" if late_missing else "pending" if m24h is None else "winner" if m24h >= 2 else "survived" if m24h >= .7 and l24h else "failed"
                risk = "high" if len(flags) >= 2 else "medium" if flags else "unknown"
                conn.execute("""INSERT OR REPLACE INTO token_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (mint, utc_now(), l1h, l24h, m1h, m24h, outcome, risk,
                   json.dumps({"flags": flags, "snapshot_windows": sorted(snapshots)}, ensure_ascii=False)))
                labels += 1
                creator = conn.execute("SELECT creator FROM launches WHERE mint=?", (mint,)).fetchone()
                if creator and creator["creator"]:
                    creators.add(creator["creator"])
        for creator in creators:
            self.refresh_developer_profile(creator)
        return labels

    def summary(self) -> dict[str, int]:
        with self.connection() as conn:
            def count(table: str, where: str = "") -> int:
                return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
            return {"launches": count("launches"), "migrations": count("migrations"), "snapshots": count("snapshots"),
                    "missing_snapshots": count("snapshots", "WHERE status='not_found'"),
                    "social_searches": count("social_searches"), "forensics_queued": count("forensics_jobs", "WHERE status='queued'"),
                    "raw_trades": count("raw_trades"), "wallet_profiles": count("wallet_features"),
                    "holder_snapshots": count("holder_snapshots"), "developer_profiles": count("developer_profiles"),
                    "tracked_wallets": count("tracked_wallets", "WHERE enabled=1"),
                    "wallet_events": count("wallet_signatures"), "smart_wallet_hits": count("smart_wallet_hits"),
                    "labels": count("token_labels"), "x_first_searches": count("x_first_searches"),
                    "x_first_posts": count("x_first_posts"),
                    "x_first_post_observations": count("x_first_post_observations"),
                    "x_followup_cohort": count("x_followup_cohort"),
                    "x_followup_observations": count("x_followup_observations"),
                    "launch_source_metadata": count("launch_source_metadata"),
                    "launch_source_post_links": count("launch_source_metadata", "WHERE source_post_id IS NOT NULL")}

    def evaluation_rows(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("""SELECT m.mint,COALESCE(m.name,l.name,tm.name) name,
              COALESCE(m.symbol,l.symbol,tm.symbol) symbol,m.observed_at,
              l.creator,l.launched_at,(julianday(m.observed_at)-julianday(l.launched_at))*1440 minutes_to_migration,
              e.price_usd price_5m,z.price_usd price_24h,e.liquidity_usd liquidity_5m,z.liquidity_usd liquidity_24h,
              e.status status_5m,z.status status_24h,e.volume_m5_usd,e.buys_m5,e.sells_m5,
              COALESCE((SELECT SUM(returned_tweets) FROM social_searches s WHERE s.mint=m.mint),0) social_tweets,
              COALESCE((SELECT MAX(is_capped) FROM social_searches s WHERE s.mint=m.mint),0) social_capped,
              COALESCE((SELECT SUM(unique_authors) FROM social_searches s WHERE s.mint=m.mint),0) social_authors,
              w.top_trader_volume_share,w.rapid_flip_ratio,w.risk_flags_json,t.outcome,t.risk_level
              FROM migrations m LEFT JOIN launches l ON l.mint=m.mint LEFT JOIN token_metadata tm ON tm.mint=m.mint
              JOIN snapshots e ON e.mint=m.mint AND e.window_minutes=5
              JOIN snapshots z ON z.mint=m.mint AND z.window_minutes=1440 LEFT JOIN wallet_features w ON w.mint=m.mint
              LEFT JOIN token_labels t ON t.mint=m.mint ORDER BY m.observed_at DESC""").fetchall()

    def report_coverage(self) -> dict[str, int]:
        with self.connection() as conn:
            scalar = lambda sql: int(conn.execute(sql).fetchone()[0] or 0)
            return {
                "migrations": scalar("SELECT COUNT(*) FROM migrations"),
                "launch_linked": scalar("SELECT COUNT(*) FROM migrations m JOIN launches l ON l.mint=m.mint"),
                "snapshot_5m": scalar("SELECT COUNT(*) FROM snapshots WHERE window_minutes=5"),
                "snapshot_24h": scalar("SELECT COUNT(*) FROM snapshots WHERE window_minutes=1440"),
                "dead_or_missing_24h": scalar("SELECT COUNT(*) FROM snapshots WHERE window_minutes=1440 AND status='not_found'"),
                "social_tokens": scalar("SELECT COUNT(DISTINCT mint) FROM social_searches"),
                "forensic_tokens": scalar("SELECT COUNT(*) FROM wallet_features"),
                "forensic_partial": scalar("SELECT COUNT(*) FROM forensics_jobs WHERE status='partial'"),
                "smart_wallet_tokens": scalar("SELECT COUNT(DISTINCT mint) FROM smart_wallet_hits"),
            }

    def smart_wallet_rows(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("""SELECT h.*,COALESCE(m.name,l.name) name,COALESCE(m.symbol,l.symbol) symbol,
              m.observed_at migration_time,l.launched_at,t.multiple_24h,t.outcome,t.risk_level
              FROM smart_wallet_hits h JOIN migrations m ON m.mint=h.mint
              LEFT JOIN launches l ON l.mint=h.mint LEFT JOIN token_labels t ON t.mint=h.mint
              ORDER BY h.first_seen DESC""").fetchall()


def _float(value: object) -> float | None:
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None

def _int(value: object) -> int | None:
    try: return int(value) if value is not None else None
    except (TypeError, ValueError): return None

def _sample_group(mint: str) -> str:
    """Stable 20% random control sample, independent of future token outcome."""
    return "control" if hashlib.sha256(mint.encode("utf-8")).digest()[0] < 51 else "candidate"

def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    return numerator / denominator if numerator is not None and denominator and denominator > 0 else None

def _retained(initial: float | None, later: float | None) -> int | None:
    return None if initial is None or later is None else int(later >= initial * .5)

def _social_stats(tweets: list[dict], launch_time: str | None, migration_time: str | None) -> dict[str, int]:
    launch, migration = _parse_time(launch_time), _parse_time(migration_time)
    result = {"pre_launch_tweets": 0, "pre_migration_tweets": 0, "post_migration_tweets": 0,
              "unique_authors": 0, "suspicious_authors": 0}
    authors: set[str] = set()
    for tweet in tweets:
        author = tweet.get("author") or tweet.get("user") or {}
        identifier = str(author.get("id") or author.get("userId") or tweet.get("authorId") or "")
        if identifier: authors.add(identifier)
        followers, statuses = _float(author.get("followers") or author.get("followersCount")), _float(author.get("statusesCount") or author.get("tweetCount"))
        if (followers is not None and followers < 20) or (statuses is not None and statuses < 5): result["suspicious_authors"] += 1
        posted = _parse_time(tweet.get("createdAt") or tweet.get("created_at"))
        if posted and launch and posted < launch: result["pre_launch_tweets"] += 1
        elif posted and migration and posted < migration: result["pre_migration_tweets"] += 1
        elif posted and migration: result["post_migration_tweets"] += 1
    result["unique_authors"] = len(authors)
    return result

def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value: return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try: parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError): return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
