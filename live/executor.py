"""Executes an ExecutionPlan against eToro. One order failing never aborts the
whole cycle — each result is captured so the bot keeps a complete audit trail.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from live.config import Settings
from live.etoro_client import EToroClient, EToroAPIError
from live.risk_guardrails import ExecutionPlan

logger = logging.getLogger("executor")


def execute_plan(
    client: EToroClient,
    plan: ExecutionPlan,
    held_full: Dict[str, Dict[str, Any]],
    symbol_to_iid: Dict[str, int],
    settings: Settings,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    for order in plan.orders:
        symbol = order.symbol.upper()
        iid = symbol_to_iid.get(symbol)
        base = {
            "symbol": symbol,
            "side": order.side,
            "amount_usd": round(order.amount_usd, 2),
            "price": order.price,
            "confidence": order.confidence,
            "source_action": order.source_action,
            "reasoning": order.reasoning,
        }

        if iid is None:
            results.append({**base, "status": "error", "detail": "no instrumentId resolved"})
            continue

        try:
            if order.side == "buy":
                resp = client.open_market_position(
                    instrument_id=iid,
                    is_buy=True,
                    amount=order.amount_usd,
                    stop_loss_rate=order.stop_loss_rate,
                    leverage=settings.leverage,
                )
                status = "dry_run" if resp.get("dry_run") else "placed"
                results.append({**base, "status": status,
                                "stop_loss_rate": order.stop_loss_rate, "detail": resp})

            elif order.side == "close":
                pids = (held_full.get(symbol, {}) or {}).get("position_ids") or []
                if not pids and order.position_id is not None:
                    pids = [order.position_id]
                if not pids:
                    results.append({**base, "status": "error", "detail": "no position_id(s) to close"})
                    continue
                closed = []
                for pid in pids:
                    resp = client.close_position(pid, instrument_id=iid)
                    closed.append({"position_id": pid, "resp": resp})
                status = "dry_run" if settings.dry_run else "closed"
                results.append({**base, "status": status, "detail": closed})

            else:
                results.append({**base, "status": "error", "detail": f"unknown side {order.side}"})

        except EToroAPIError as e:
            logger.error("Order failed for %s (%s): %s", symbol, order.side, e)
            results.append({**base, "status": "error", "detail": str(e),
                            "http_status": getattr(e, "status", None)})
        except Exception as e:  # never let one order kill the cycle
            logger.exception("Unexpected error executing %s", symbol)
            results.append({**base, "status": "error", "detail": f"unexpected: {e}"})

    # Record everything we deliberately skipped, too.
    for sk in plan.skipped:
        results.append({"symbol": sk.symbol, "side": sk.action,
                        "status": "skipped", "detail": sk.reason})

    return results
