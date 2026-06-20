"""Optional notifications. Always logs; if AIHF_NOTIFY_WEBHOOK is set, posts a
compact summary to that URL (Slack/Discord/generic JSON webhook compatible).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from live.config import Settings

logger = logging.getLogger("notifier")


def format_summary(cycle: Dict[str, Any]) -> str:
    env = cycle.get("environment", "?")
    dry = " (DRY-RUN)" if cycle.get("dry_run") else ""
    lines: List[str] = [f"AI Hedge Fund — {env}{dry} — {cycle.get('timestamp', '')}"]

    acct = cycle.get("account", {})
    if acct:
        lines.append(
            f"Account: cash ${acct.get('cash', 0):,.2f} | equity ${acct.get('equity', 0):,.2f} "
            f"| budget ${cycle.get('budget', 0):,.0f}"
        )

    placed = [r for r in cycle.get("results", []) if r.get("status") in ("placed", "closed", "dry_run")]
    errors = [r for r in cycle.get("results", []) if r.get("status") == "error"]
    skipped = [r for r in cycle.get("results", []) if r.get("status") == "skipped"]

    if placed:
        lines.append("Orders:")
        for r in placed:
            if r["side"] == "buy":
                lines.append(f"  BUY  {r['symbol']:<6} ${r.get('amount_usd', 0):,.2f} "
                             f"(conf {r.get('confidence', '?')})  {r.get('reasoning', '')[:60]}")
            else:
                lines.append(f"  {r['side'].upper():<5}{r['symbol']:<6} {r.get('reasoning', '')[:60]}")
    else:
        lines.append("Orders: none")

    if errors:
        lines.append(f"Errors: {len(errors)}")
        for r in errors[:5]:
            lines.append(f"  ! {r.get('symbol', '?')}: {str(r.get('detail'))[:80]}")
    if skipped:
        lines.append(f"Skipped: {len(skipped)} (" +
                     ", ".join(f"{r['symbol']}:{str(r['detail'])[:24]}" for r in skipped[:6]) + ")")
    return "\n".join(lines)


def notify(settings: Settings, cycle: Dict[str, Any]) -> None:
    text = format_summary(cycle)
    logger.info("\n%s", text)
    if not settings.notify_webhook:
        return
    try:
        # Slack/Discord both accept a JSON body with a "text"/"content" field.
        payload = {"text": text, "content": text}
        with httpx.Client(timeout=15.0, trust_env=True) as c:
            c.post(settings.notify_webhook, json=payload)
    except Exception as e:
        logger.warning("Notification webhook failed: %s", e)
