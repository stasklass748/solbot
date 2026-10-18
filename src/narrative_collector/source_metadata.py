"""Bounded retrieval of creator-supplied IPFS metadata for migrated tokens.

The launch URI is evidence of what the creator submitted, not evidence that a
linked X post caused the token's demand. Never request the launch URI itself:
untrusted hosts are reduced to a CID and fetched through a fixed gateway.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .storage import Store

LOGGER = logging.getLogger(__name__)
GATEWAY = "https://gateway.pinata.cloud/ipfs/"
CID_RE = re.compile(r"[A-Za-z0-9]{40,100}\Z")
POST_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/[^/\s]+/status/(\d+)", re.I)
MAX_BYTES = 65_536


def cid_from_uri(uri: str) -> str | None:
    """Accept IPFS references only; never use the supplied network host."""
    parsed = urlparse(uri)
    if parsed.scheme == "ipfs":
        candidate = parsed.netloc or parsed.path.strip("/").split("/")[0]
    elif parsed.scheme in {"http", "https"}:
        match = re.search(r"(?:^|/)ipfs/([A-Za-z0-9]{40,100})(?:/|$)", parsed.path)
        candidate = match.group(1) if match else ""
    else:
        candidate = uri if "/" not in uri else ""
    return candidate if CID_RE.fullmatch(candidate) else None


def fetch_ipfs(cid: str) -> dict:
    if not CID_RE.fullmatch(cid):
        raise ValueError("invalid CID")
    request = Request(GATEWAY + cid, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("IPFS JSON exceeds 64 KiB")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("IPFS JSON is not an object")
    return payload


def post_link(metadata: dict) -> tuple[str | None, str | None]:
    candidate = metadata.get("twitter")
    match = POST_RE.search(candidate) if isinstance(candidate, str) else None
    if not match:
        return None, None
    return match.group(1), f"https://x.com/i/status/{match.group(1)}"


class SourceMetadataWorker:
    def __init__(self, store: Store, fetcher=fetch_ipfs):
        self.store = store
        self.fetcher = fetcher

    def process(self, since: str, max_jobs: int = 10) -> dict[str, int]:
        if not since:
            raise ValueError("SOURCE_METADATA_SINCE must be set to bound this research run")
        if not 1 <= max_jobs <= 100:
            raise ValueError("max_jobs must be between 1 and 100")
        rows = self.store.pending_launch_sources(since, max_jobs)
        result = {"processed": 0, "with_post_link": 0, "fetch_errors": 0}
        for row in rows:
            try:
                event = json.loads(row["raw_json"])
                uri = event.get("uri") if isinstance(event, dict) else None
                uri = uri if isinstance(uri, str) else ""
                cid = cid_from_uri(uri)
                if not uri:
                    status, metadata = "missing_uri", {}
                elif not cid:
                    status, metadata = "non_ipfs_uri", {}
                else:
                    try:
                        metadata = self.fetcher(cid)
                        if not isinstance(metadata, dict):
                            raise ValueError("metadata is not an object")
                        status = "ok"
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        LOGGER.warning("IPFS metadata failed for %s: %s", row["mint"], exc)
                        status, metadata = "fetch_error", {"fetch_error": str(exc)[:200]}
                        result["fetch_errors"] += 1
                description = metadata.get("description")
                description = description[:3000] if isinstance(description, str) else None
                post_id, post_url = post_link(metadata)
                self.store.save_launch_source(row["mint"], uri[:1000], cid, status,
                                              description, post_id, post_url, metadata)
                result["processed"] += 1
                result["with_post_link"] += int(post_id is not None)
            except Exception:
                LOGGER.exception("Unexpected source metadata failure for %s", row["mint"])
                result["fetch_errors"] += 1
        return result
