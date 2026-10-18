"""Small, token-independent 1h/24h engagement rechecks for X-first posts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Settings
from .providers import TwitterApiIoClient
from .storage import Store

MAX_LOOKUP_CREDITS = 20 * 15


class XFollowupWorker:
    def __init__(self, settings: Settings, store: Store, client: TwitterApiIoClient | None = None):
        self.settings = settings
        self.store = store
        self.client = client or TwitterApiIoClient(settings.twitterapi_io_key, 20)

    def process(self) -> dict[str, int | str]:
        result: dict[str, int | str] = {"status": "ok", "lookups": 0, "observations": 0,
                                         "missed_windows": 0, "credits": 0}
        if not self.settings.x_first_run_id:
            raise ValueError("X_FIRST_RUN_ID is required")
        if not self.settings.twitterapi_io_key:
            raise ValueError("TWITTERAPI_IO_KEY is empty")
        if not 0 <= self.settings.x_first_followup_sample_percent <= 100:
            raise ValueError("X_FIRST_FOLLOWUP_SAMPLE_PERCENT must be in 0..100")
        if self.settings.x_first_followup_sample_percent == 0:
            return dict(result, status="disabled")
        now = datetime.now(timezone.utc)
        jobs = self.store.due_x_followups(self.settings.x_first_run_id, now.isoformat(), 20)
        due = []
        for job in jobs:
            first = datetime.fromisoformat(job["first_observed_at"])
            window = job["window_minutes"]
            target = first + timedelta(minutes=window)
            tolerance = timedelta(minutes=30 if window == 60 else 120)
            if now > target + tolerance:
                self.store.save_x_followup(job["post_id"], window, now.isoformat(), "missed_window", None)
                result["missed_windows"] += 1
            else:
                due.append(job)
        if not due:
            return result

        ids = list(dict.fromkeys(job["post_id"] for job in due))
        week = (now.date() - timedelta(days=now.weekday())).isoformat()
        run_used = self.store.weekly_usage("twitterapi_xfirst", self.settings.x_first_run_id)
        week_used = self.store.weekly_usage("twitterapi", week)
        if ((self.settings.x_first_credit_cap > 0 and
             run_used + MAX_LOOKUP_CREDITS > self.settings.x_first_credit_cap) or
                (self.settings.social_weekly_credit_cap > 0 and
                 week_used + MAX_LOOKUP_CREDITS > self.settings.social_weekly_credit_cap)):
            return dict(result, status="budget_exhausted")
        tweets = self.client.tweets_by_ids(ids)
        by_id = {str(tweet.get("id") or tweet.get("id_str")): tweet for tweet in tweets}
        charged = max(15, len(tweets) * 15)
        captured = datetime.now(timezone.utc).isoformat()
        for job in due:
            tweet = by_id.get(job["post_id"])
            self.store.save_x_followup(job["post_id"], job["window_minutes"], captured,
                                       "ok" if tweet else "missing_tweet", tweet)
            result["observations"] += 1
        self.store.add_usage("twitterapi", week, charged)
        self.store.add_usage("twitterapi_xfirst", self.settings.x_first_run_id, charged)
        result["lookups"] = 1
        result["credits"] = charged
        return result
