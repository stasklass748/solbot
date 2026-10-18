from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .http import get_json, post_json


class DexScreenerClient:
    def token_metrics(self, mint: str) -> dict[str, Any] | None:
        payload = get_json(f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}")
        if not isinstance(payload, list) or not payload:
            return None
        pair = max(payload, key=lambda item: (item.get("liquidity") or {}).get("usd") or 0)
        volume = pair.get("volume") or {}
        transactions = pair.get("txns") or {}
        m5 = transactions.get("m5") or {}
        h1 = transactions.get("h1") or {}
        h6 = transactions.get("h6") or {}
        h24 = transactions.get("h24") or {}
        return {
            "price_usd": _float(pair.get("priceUsd")),
            "liquidity_usd": _float((pair.get("liquidity") or {}).get("usd")),
            "volume_m5_usd": _float(volume.get("m5")),
            "volume_h1_usd": _float(volume.get("h1")),
            "volume_h6_usd": _float(volume.get("h6")),
            "volume_h24_usd": _float(volume.get("h24")),
            "buys_m5": _int(m5.get("buys")),
            "sells_m5": _int(m5.get("sells")),
            "buys_h1": _int(h1.get("buys")),
            "sells_h1": _int(h1.get("sells")),
            "buys_h6": _int(h6.get("buys")),
            "sells_h6": _int(h6.get("sells")),
            "buys_h24": _int(h24.get("buys")),
            "sells_h24": _int(h24.get("sells")),
            "market_cap": _float(pair.get("marketCap") or pair.get("fdv")),
            "raw": pair,
        }


class TwitterApiIoClient:
    ENDPOINT = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    TWEETS_ENDPOINT = "https://api.twitterapi.io/twitter/tweets"

    def __init__(self, api_key: str, max_results: int):
        self.api_key = api_key
        self.max_results = max_results

    def search(self, query: str) -> list[dict[str, Any]]:
        payload = self.search_page(query)
        return self._tweets(payload)[: self.max_results]

    def search_page(self, query: str, cursor: str | None = None) -> dict[str, Any]:
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        payload = get_json(
            self.ENDPOINT,
            headers={"X-API-Key": self.api_key},
            params=params,
        )
        if not isinstance(payload, dict):
            raise ValueError("TwitterAPI.io search returned an unexpected response type")
        if payload.get("error") or payload.get("status") == "error":
            raise ValueError(f"TwitterAPI.io search error: {str(payload.get('error') or payload.get('message'))[:200]}")
        if not any(isinstance(payload.get(key), list) for key in ("tweets", "data", "results")):
            raise ValueError("TwitterAPI.io search returned no posts array")
        return payload

    def tweets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not 1 <= len(ids) <= 20 or any(not item.isdigit() for item in ids):
            raise ValueError("Tweet lookup requires 1-20 numeric IDs")
        payload = get_json(self.TWEETS_ENDPOINT, headers={"X-API-Key": self.api_key},
                           params={"tweet_ids": ",".join(ids)})
        if not isinstance(payload, dict) or not isinstance(payload.get("tweets"), list):
            raise ValueError("TwitterAPI.io tweet lookup returned an unexpected response")
        if payload.get("status") == "error":
            raise ValueError(f"TwitterAPI.io tweet lookup error: {str(payload.get('msg'))[:200]}")
        tweets = self._tweets(payload)
        if len(tweets) > len(ids):
            raise ValueError("TwitterAPI.io tweet lookup returned more items than requested")
        return tweets

    @staticmethod
    def _tweets(payload: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        for key in ("tweets", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        return [item for item in candidates if isinstance(item, dict)]


class BitqueryClient:
    ENDPOINT = "https://streaming.bitquery.io/graphql"

    def __init__(self, access_token: str):
        self.access_token = access_token

    def query(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        return post_json(
            self.ENDPOINT,
            {"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self.access_token}"},
        )

    def probe(self) -> dict:
        return self.query(
            """query { Solana { DEXTrades(limit: {count: 1}) {
                Trade {
                  Dex { ProtocolName }
                  Buy { Price Currency { Symbol } }
                }
            } } }"""
        )

    def token_trade_tape(self, mint: str, limit: int = 1000) -> list[dict]:
        """Recent raw Pump.fun/PumpSwap trade tape for a migrated token.

        `limit` is deliberately bounded. A response with exactly `limit` rows is
        marked partial by the forensic worker rather than being silently treated
        as the full trade history.
        """
        response = self.query(
            """query ($token: String!, $limit: Int!) {
              Solana(dataset: realtime) {
                DEXTrades(
                  limit: {count: $limit}
                  orderBy: [{ascending: Block_Time}, {ascending: Transaction_Index}, {ascending: Trade_Index}]
                  where: {
                    Transaction: {Result: {Success: true}}
                    any: [
                      {Trade: {Buy: {Currency: {MintAddress: {is: $token}}}}}
                      {Trade: {Sell: {Currency: {MintAddress: {is: $token}}}}}
                    ]
                  }
                ) {
                  Block { Time Slot }
                  Transaction { Signature Signer FeePayer }
                  Trade {
                    Index
                    Dex { ProtocolName ProgramAddress }
                    Buy {
                      Amount AmountInUSD Price PriceInUSD
                      Currency { MintAddress Symbol Name }
                    }
                    Sell {
                      Amount AmountInUSD Price PriceInUSD
                      Currency { MintAddress Symbol Name }
                    }
                  }
                }
              }
            }""",
            {"token": mint, "limit": limit},
        )
        if response.get("errors"):
            raise RuntimeError(str(response["errors"]))
        return ((response.get("data") or {}).get("Solana") or {}).get("DEXTrades") or []


class HeliusClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def call(self, method: str, params: list[Any]) -> dict:
        return post_json(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    def recent_signatures(self, address: str, limit: int = 25) -> list[dict]:
        response = self.call("getSignaturesForAddress", [address, {"limit": limit}])
        return response.get("result") or []

    def largest_token_accounts(self, mint: str) -> list[dict]:
        """Standard Solana RPC's top-20 token accounts, not a full holder list."""
        response = self.call("getTokenLargestAccounts", [mint, {"commitment": "finalized"}])
        return response.get("result", {}).get("value") or []

    def transaction(self, signature: str) -> dict | None:
        response = self.call("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        return response.get("result")


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
