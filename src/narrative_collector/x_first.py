"""Prospective, token-independent X sampling for narrative research.

This is a fixed search sample, not a complete X firehose or an automatic
post-to-token attribution system. Posts with no later token are retained.
"""

from __future__ import annotations

import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .config import Settings
from .providers import TwitterApiIoClient
from .storage import Store
from .watchlist import read_accounts

LOGGER = logging.getLogger(__name__)
MAX_PAGE_CREDITS = 20 * 15  # The documented advanced_search page has up to 20 posts.


class XFirstWorker:
    def __init__(self, settings: Settings, store: Store, client: TwitterApiIoClient | None = None):
        self.settings = settings
        self.store = store
        self.client = client or TwitterApiIoClient(settings.twitterapi_io_key, 20)

    def process(self) -> dict[str, int | str]:
        now = datetime.now(timezone.utc)
        if not self.settings.x_first_enabled:
            return {"status": "disabled", "searches": 0, "new_posts": 0, "credits": 0}
        if not self.settings.twitterapi_io_key:
            raise ValueError("TWITTERAPI_IO_KEY is empty")
        if not self.settings.x_first_run_id:
            raise ValueError("X_FIRST_RUN_ID is required to enforce the per-run X credit cap")
        if self.settings.x_first_credit_cap < 0 or self.settings.social_weekly_credit_cap < 0:
            raise ValueError("X credit caps must be nonnegative; 0 means unlimited")

        raw_queries = json.loads(self.settings.x_first_queries_path.read_text(encoding="utf-8"))
        if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= 5:
            raise ValueError("x_first_queries.json must contain 1-5 search objects")
        queries: list[tuple[str, str, int, int]] = []
        for item in raw_queries:
            if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not isinstance(item.get("query"), str):
                raise ValueError("Each X-first query needs label and query strings")
            label, query = item["label"].strip(), item["query"].strip()
            if not label or not query or len(query) > 400:
                raise ValueError("X-first query label/query empty or query too long")
            interval = item.get("interval_minutes", 10)
            pages = item.get("max_pages", 1)
            if not isinstance(interval, int) or not 0 <= interval <= 1440:
                raise ValueError("interval_minutes must be an integer in 0..1440")
            if not isinstance(pages, int) or not 1 <= pages <= 3:
                raise ValueError("max_pages must be an integer in 1..3")
            queries.append((label, query, interval, pages))

        # One rotating batch of up to five accounts per ten-minute tick keeps
        # the shared X cap meaningful even when both reviewers upload files.
        state_path = getattr(self.settings, "database_path", None)
        watched = read_accounts(state_path.parent / "idea-bot-state.sqlite3") if state_path else []
        watch_label = ""
        watch_batch: list[tuple[str, str]] = []
        watch_last: dict[str, str] = {}
        if watched:
            # Keep rotation fair across bounded runs; restarting a run must not
            # repeatedly select the first five alphabetical handles.
            with self.store.connection() as conn:
                watch_last = {str(label): str(observed_at) for label, observed_at in conn.execute(
                    "SELECT query_label,MAX(observed_at) FROM x_first_searches "
                    "WHERE query_label LIKE 'watch:%' GROUP BY query_label")}
            batches = [watched[i:i + 5] for i in range(0, len(watched), 5)]
            labels = [("watch:" + hashlib.sha256(",".join(handle for handle, _ in batch).encode()).hexdigest()[:12], batch)
                      for batch in batches]
            labels.sort(key=lambda item: (watch_last.get(item[0]) or "", item[0]))
            watch_label, watch_batch = labels[0]
            query = ("(" + " OR ".join("from:" + handle for handle, _ in watch_batch) + ") "
                     "within_time:2h -filter:retweets -filter:replies")
            # Search topics first; watchlist accounts use the remaining run budget.
            queries.append((watch_label, query, 10, 1))

        searches = new_posts = credits = 0
        for label, query, interval, max_pages in queries:
            last_text = (watch_last.get(label) if label == watch_label else
                         self.store.last_x_first_search_at(self.settings.x_first_run_id, label))
            last = datetime.fromisoformat(last_text) if last_text else None
            if last and not _query_due(now, last, interval):
                continue
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for page in range(max_pages):
                week = (now.date() - timedelta(days=now.weekday())).isoformat()
                run_used = self.store.weekly_usage("twitterapi_xfirst", self.settings.x_first_run_id)
                week_used = self.store.weekly_usage("twitterapi", week)
                if ((self.settings.x_first_credit_cap > 0 and
                     run_used + MAX_PAGE_CREDITS > self.settings.x_first_credit_cap) or
                        (self.settings.social_weekly_credit_cap > 0 and
                         week_used + MAX_PAGE_CREDITS > self.settings.social_weekly_credit_cap)):
                    LOGGER.warning("X-first credit cap reached: run=%d/%d week=%d/%d", run_used,
                                   self.settings.x_first_credit_cap, week_used, self.settings.social_weekly_credit_cap)
                    return {"status": "budget_exhausted", "searches": searches,
                            "new_posts": new_posts, "credits": credits}
                payload = self.client.search_page(query) if page == 0 else self.client.search_page(query, cursor=cursor)
                tweets = TwitterApiIoClient._tweets(payload)
                if len(tweets) > 20:
                    raise ValueError("TwitterAPI.io returned more than 20 posts; cannot safely bound credit usage")
                saved_tweets = tweets
                if label == watch_label:
                    added = {handle: datetime.fromisoformat(when) for handle, when in watch_batch}
                    saved_tweets = []
                    for item in tweets:
                        author = item.get("author") or item.get("user") or {}
                        if not isinstance(author, dict):
                            continue
                        handle = str(author.get("userName") or author.get("screen_name") or "").lower()
                        created = _post_time(item)
                        if handle in added and created and created >= added[handle]:
                            saved_tweets.append(dict(item, watchlist_source=handle))
                normalized = dict(payload, tweets=saved_tweets)
                charged = max(15, len(tweets) * 15)
                count, fresh = self.store.save_x_first_search(
                    self.settings.x_first_run_id, label, query, normalized, charged,
                    datetime.now(timezone.utc).isoformat(),
                    getattr(self.settings, "x_first_followup_sample_percent", 0),
                )
                self.store.add_usage("twitterapi", week, charged)
                self.store.add_usage("twitterapi_xfirst", self.settings.x_first_run_id, charged)
                searches += 1
                new_posts += fresh
                credits += charged
                LOGGER.info("X-first %s page %d: %d posts, %d new, %d estimated credits",
                            label, page + 1, count, fresh, charged)
                if not payload.get("has_next_page"):
                    break
                next_cursor = payload.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                # Once the page overlaps the previous poll, deeper paid pages
                # cannot add new chronological coverage to this Latest query.
                oldest = [_post_time(t) for t in tweets]
                oldest = [value for value in oldest if value]
                if last and oldest and min(oldest) <= last:
                    break
                cursor = next_cursor
        return {"status": "ok", "searches": searches, "new_posts": new_posts, "credits": credits}


def _post_time(tweet: dict) -> datetime | None:
    value = tweet.get("createdAt") or tweet.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _query_due(now: datetime, last: datetime, interval_minutes: int) -> bool:
    # A timer firing at the exact 10-minute boundary can precede the previous
    # request's completion time by a few seconds. Bounded slack avoids skipping
    # every other tick while preserving the configured sampling interval.
    slack = timedelta(seconds=min(30, interval_minutes * 3))
    return now + slack >= last + timedelta(minutes=interval_minutes)
