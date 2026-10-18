from __future__ import annotations

import statistics
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .scoring import candidate_decision
from .storage import Store


def write_report(store: Store, settings: Settings, output_dir: Path) -> Path:
    """Create an auditable v0 filter report from completed 5m -> 24h observations."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = store.evaluation_rows()
    coverage = store.report_coverage()
    records: list[dict] = []
    for row in rows:
        early_price, late_price = row["price_5m"], row["price_24h"]
        if row["status_5m"] != "ok" or not early_price:
            continue
        if row["status_24h"] == "not_found":
            multiple = 0.0
        elif late_price:
            multiple = late_price / early_price
        else:
            continue
        eligible = candidate_decision(
            dict(row), settings.social_candidate_min_volume_usd, settings.social_candidate_min_buys
        ).eligible_for_social
        records.append({**dict(row), "multiple_24h": multiple, "winner": multiple >= 2, "eligible": eligible})

    winners = [item for item in records if item["winner"]]
    gated = [item for item in records if item["eligible"]]
    gated_winners = [item for item in gated if item["winner"]]
    lines = [
        "# Meme narrative collector — weekly filter report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Scope",
        "",
        f"- All migrations observed: **{coverage['migrations']}**",
        f"- Migrations linked to a launch: **{coverage['launch_linked']}**",
        f"- Tokens with a 5m snapshot: **{coverage['snapshot_5m']}**",
        f"- Tokens with a 24h snapshot: **{coverage['snapshot_24h']}**",
        f"- Explicit 24h dead/not-found observations: **{coverage['dead_or_missing_24h']}**",
        f"- Tokens with social data: **{coverage['social_tokens']}**",
        f"- Tokens with forensic wallet features: **{coverage['forensic_tokens']}**",
        f"- Partial forensic histories: **{coverage['forensic_partial']}**",
        f"- Tokens containing a tracked-wallet DEX hit: **{coverage['smart_wallet_tokens']}**",
        "",
        f"- Migrations with completed 5m and 24h observations: **{len(records)}**",
        f"- Defined winners (24h price / 5m price ≥ 2.0): **{len(winners)}**",
        f"- Baseline win rate: **{_percent(len(winners), len(records))}**",
        f"- Tokens passing the early on-chain gate: **{len(gated)}**",
        f"- Gate win rate: **{_percent(len(gated_winners), len(gated))}**",
        "",
        "## Current v0 gate",
        "",
        f"- 5m volume ≥ ${settings.social_candidate_min_volume_usd:,.0f}",
        f"- 5m buys ≥ {settings.social_candidate_min_buys}",
        "- at least two of: volume, buys, buy/sell balance (buys ≥ 1.2 × sells)",
        "",
        "This is a measured hypothesis, not a trading recommendation. Change one threshold at a time, then run the next week's out-of-sample validation.",
        "",
        "## Strongest completed outcomes",
        "",
        "| Token | Time to migration | 24h multiple | 5m volume | 5m buys | Avg $/buy | Social tweets | Authors | Flow risk |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in sorted(records, key=lambda value: value["multiple_24h"], reverse=True)[:25]:
        token = item.get("symbol") or item.get("name") or item["mint"][:8]
        migration_time = item.get("minutes_to_migration")
        formatted_time = _migration_time(migration_time)
        social = f"{item.get('social_tweets') or 0}{'+' if item.get('social_capped') else ''}"
        average_buy = (item.get("volume_m5_usd") or 0) / max(item.get("buys_m5") or 0, 1)
        lines.append(
            f"| {token} | {formatted_time} | {item['multiple_24h']:.2f}x | ${item.get('volume_m5_usd') or 0:,.0f} | "
            f"{item.get('buys_m5') or 0} | ${average_buy:,.2f} | {social} | {item.get('social_authors') or 0} | "
            f"{item.get('risk_level') or 'unknown'} |"
        )
    lines.extend([
        "", "## Weakest completed outcomes", "",
        "| Token | Time to migration | 24h multiple | 5m volume | 5m buys | Outcome |",
        "|---|---:|---:|---:|---:|---|",
    ])
    for item in sorted(records, key=lambda value: value["multiple_24h"])[:25]:
        token = item.get("symbol") or item.get("name") or item["mint"][:8]
        lines.append(
            f"| {token} | {_migration_time(item.get('minutes_to_migration'))} | {item['multiple_24h']:.2f}x | "
            f"${item.get('volume_m5_usd') or 0:,.0f} | {item.get('buys_m5') or 0} | {item.get('outcome') or 'failed'} |"
        )
    if records:
        median = statistics.median(item["multiple_24h"] for item in records)
        lines.extend(["", "## Distribution", "", f"- Median 24h / 5m multiple: **{median:.2f}x**"])
    path = output_dir / f"filter-report-{datetime.now(timezone.utc).date().isoformat()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _percent(part: int, whole: int) -> str:
    return "n/a" if not whole else f"{part / whole * 100:.1f}%"


def _migration_time(value: float | None) -> str:
    if value is None or value < 0:
        return "n/a"
    if value < 0.1:
        return "<0.1m"
    return f"{value:.1f}m"


def write_smart_wallet_report(store: Store, output_dir: Path) -> Path:
    """Observed DEX participation by tracked public addresses; no motive claims."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [dict(row) for row in store.smart_wallet_rows()]
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["label"], []).append(row)
    lines = [
        "# Smart-wallet observation report", "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
        "This report shows only observed DEX-tape participation. It does not identify a person, prove a purchase from balance movement alone, or establish why an address entered a token.",
        "", "## Aggregate by tracked address", "",
        "| Label | Migrated tokens in available tape | Winners with 24h label | Buy USD | Sell USD |", "|---|---:|---:|---:|---:|",
    ]
    for label, items in sorted(grouped.items()):
        winners = sum(1 for item in items if item.get("outcome") == "winner")
        lines.append(f"| {label} | {len(items)} | {winners} | ${sum(float(x['buy_volume_usd'] or 0) for x in items):,.0f} | ${sum(float(x['sell_volume_usd'] or 0) for x in items):,.0f} |")
    lines.extend(["", "## Token observations", "", "| Wallet | Token | First DEX-tape observation | Buy / sell USD | 24h outcome | Risk |", "|---|---|---|---:|---|---|"])
    for item in rows[:200]:
        token = item.get("symbol") or item.get("name") or item["mint"][:8]
        flow = f"${float(item['buy_volume_usd'] or 0):,.0f} / ${float(item['sell_volume_usd'] or 0):,.0f}"
        outcome = item.get("outcome") or "pending"
        if item.get("multiple_24h") is not None:
            outcome += f" ({float(item['multiple_24h']):.2f}x)"
        lines.append(f"| {item['label']} | {token} | {item.get('first_seen') or 'n/a'} | {flow} | {outcome} | {item.get('risk_level') or 'unknown'} |")
    path = output_dir / f"smart-wallet-report-{datetime.now(timezone.utc).date().isoformat()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
