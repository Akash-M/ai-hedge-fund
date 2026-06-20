"""Translate a live eToro portfolio into the dict shape run_hedge_fund() expects,
scaled to OUR managed budget (not the demo account's full buying power).

Also produces a `held_map` (symbol -> aggregated position info) that the guardrail
layer and executor use for reconciliation.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from live.config import Settings


def aggregate_holdings(
    account: Dict[str, Any],
    iid_to_symbol: Dict[int, str],
) -> Dict[str, Dict[str, Any]]:
    """Aggregate eToro positions by symbol, restricted to instruments we recognize.

    Returns symbol -> {amount, units, is_buy, open_rate, position_ids, position_id}
    """
    held: Dict[str, Dict[str, Any]] = {}
    for p in account.get("positions", []) or []:
        iid = p.get("instrument_id")
        symbol = iid_to_symbol.get(int(iid)) if iid is not None else None
        if not symbol:
            continue  # ignore instruments outside our managed universe
        entry = held.setdefault(symbol, {
            "amount": 0.0, "units": 0.0, "is_buy": True,
            "open_rate": 0.0, "position_ids": [],
        })
        amt = float(p.get("amount", 0.0) or 0.0)
        units = float(p.get("units", 0.0) or 0.0)
        entry["amount"] += amt
        entry["units"] += units
        entry["is_buy"] = bool(p.get("is_buy", True))
        # weighted-ish open rate
        if units > 0 and p.get("open_rate"):
            prev_units = entry["units"] - units
            entry["open_rate"] = (
                (entry["open_rate"] * prev_units + float(p["open_rate"]) * units) / entry["units"]
                if entry["units"] > 0 else float(p["open_rate"])
            )
        if p.get("position_id") is not None:
            entry["position_ids"].append(p["position_id"])
    for sym, e in held.items():
        e["position_id"] = e["position_ids"][0] if e["position_ids"] else None
    return held


def resolve_budget(
    settings: Settings,
    account: Dict[str, Any],
    held: Dict[str, Dict[str, Any]],
) -> Tuple[float, float]:
    """Return (budget, cash_available_for_new_buys).

    - If AIHF_BUDGET_USD is set (>0): the bot manages exactly that much. Cash for
      new buys = budget minus what's already invested in our universe, further
      bounded by the account's real buying power.
    - If 0: manage the whole account — budget = cash + invested.
    """
    invested = sum(float(v.get("amount", 0.0)) for v in held.values())
    account_cash = float(account.get("cash", 0.0) or 0.0)

    if settings.budget_usd and settings.budget_usd > 0:
        budget = float(settings.budget_usd)
        cash_for_buys = max(0.0, min(account_cash, budget - invested))
    else:
        budget = account_cash + invested
        cash_for_buys = account_cash
    return budget, cash_for_buys


def build_hedge_fund_portfolio(
    settings: Settings,
    universe: List[str],
    held: Dict[str, Dict[str, Any]],
    hedge_fund_cash: float,
) -> Dict[str, Any]:
    """Construct the portfolio dict consumed by run_hedge_fund().

    `hedge_fund_cash` should be the budget-bounded investable cash so the agents
    size positions against OUR budget, not the demo account's full balance.
    """
    positions: Dict[str, Any] = {}
    realized: Dict[str, Any] = {}
    for ticker in universe:
        h = held.get(ticker.upper(), {})
        long_units = int(round(float(h.get("units", 0.0)))) if h.get("is_buy", True) else 0
        short_units = int(round(float(h.get("units", 0.0)))) if not h.get("is_buy", True) else 0
        positions[ticker] = {
            "long": long_units,
            "short": short_units,
            "long_cost_basis": float(h.get("open_rate", 0.0)),
            "short_cost_basis": 0.0,
            "short_margin_used": 0.0,
        }
        realized[ticker] = {"long": 0.0, "short": 0.0}

    return {
        "cash": float(hedge_fund_cash),
        "margin_requirement": 0.0,
        "margin_used": 0.0,
        "positions": positions,
        "realized_gains": realized,
    }


def held_for_guardrails(held: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Slim view the guardrail layer needs."""
    return {
        sym: {
            "amount": float(v.get("amount", 0.0)),
            "is_buy": bool(v.get("is_buy", True)),
            "position_id": v.get("position_id"),
        }
        for sym, v in held.items()
    }
