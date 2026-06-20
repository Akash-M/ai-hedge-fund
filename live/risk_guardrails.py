"""Deterministic risk guardrails.

The LLM/agent layer proposes trades; THIS layer has the final say. It is pure
(no network, no LLM) and unit-testable, so the hard safety limits are auditable
and cannot be talked around by a model. Even the 'aggressive' profile is bounded
here.

It converts the hedge fund's share-based decisions into capped, cash-denominated
orders for the eToro execution bridge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from live.config import Settings


@dataclass
class PlannedOrder:
    symbol: str
    side: str                       # "buy" = open/increase long; "close" = exit a position
    amount_usd: float               # cash to deploy (for buy); informational for close
    price: float
    confidence: int
    source_action: str              # original hedge-fund action
    reasoning: str
    stop_loss_rate: Optional[float] = None
    position_id: Optional[Any] = None  # required for "close"


@dataclass
class SkippedDecision:
    symbol: str
    action: str
    reason: str


@dataclass
class ExecutionPlan:
    orders: List[PlannedOrder] = field(default_factory=list)
    skipped: List[SkippedDecision] = field(default_factory=list)
    budget: float = 0.0
    invested_before: float = 0.0
    projected_invested_after: float = 0.0
    notes: List[str] = field(default_factory=list)


def build_execution_plan(
    decisions: Dict[str, Dict[str, Any]],
    prices: Dict[str, float],
    held: Dict[str, Dict[str, Any]],
    cash_available: float,
    budget: float,
    settings: Settings,
) -> ExecutionPlan:
    """Translate decisions -> capped orders.

    held: symbol -> {"amount": market_value_usd, "is_buy": bool, "position_id": id}
    """
    plan = ExecutionPlan(budget=budget)

    held = {k.upper(): v for k, v in (held or {}).items()}
    invested_before = sum(float(v.get("amount", 0.0)) for v in held.values())
    plan.invested_before = invested_before

    # Running, mutable budget trackers updated as we approve orders this cycle.
    running_cash = max(0.0, float(cash_available))
    running_invested = invested_before
    open_symbols = {s for s, v in held.items() if float(v.get("amount", 0.0)) > 0}

    max_invested_cap = settings.max_invested_pct * budget
    per_position_cap = settings.max_position_pct * budget

    # ---- 1) Process exits first (sell / cover on a held long) — frees slots & cash.
    buy_candidates: List[Tuple[str, Dict[str, Any], float]] = []
    for symbol, dec in decisions.items():
        symbol = symbol.upper()
        action = str(dec.get("action", "hold")).lower()
        conf = int(dec.get("confidence", 0) or 0)
        reasoning = str(dec.get("reasoning", ""))[:300]
        price = float(prices.get(symbol, 0.0) or 0.0)

        if action == "hold":
            continue

        if action in ("short", "cover") and not settings.allow_short:
            plan.skipped.append(SkippedDecision(symbol, action,
                "short/cover skipped (long-only mode; enable AIHF_ALLOW_SHORT to use)"))
            continue

        if action in ("sell", "cover"):
            pos = held.get(symbol)
            if not pos or float(pos.get("amount", 0.0)) <= 0:
                plan.skipped.append(SkippedDecision(symbol, action, "no open position to close"))
                continue
            plan.orders.append(PlannedOrder(
                symbol=symbol, side="close", amount_usd=float(pos.get("amount", 0.0)),
                price=price, confidence=conf, source_action=action,
                reasoning=reasoning, position_id=pos.get("position_id"),
            ))
            # Closing returns cash and a slot.
            running_cash += float(pos.get("amount", 0.0))
            running_invested -= float(pos.get("amount", 0.0))
            open_symbols.discard(symbol)
            continue

        if action == "buy":
            if price <= 0:
                plan.skipped.append(SkippedDecision(symbol, action, "no valid price"))
                continue
            if conf < settings.min_confidence:
                plan.skipped.append(SkippedDecision(symbol, action,
                    f"confidence {conf} < floor {settings.min_confidence}"))
                continue
            # desired add = hedge fund's share quantity * price
            qty = float(dec.get("quantity", 0) or 0)
            desired = qty * price
            if desired <= 0:
                # fall back to a per-position target if quantity is 0 but signal is a buy
                desired = per_position_cap
            buy_candidates.append((symbol, dec, desired))
            continue

        plan.skipped.append(SkippedDecision(symbol, action, "unrecognized action"))

    # ---- 2) Allocate buys, highest conviction first, respecting all caps.
    buy_candidates.sort(key=lambda t: int(t[1].get("confidence", 0) or 0), reverse=True)

    for symbol, dec, desired in buy_candidates:
        conf = int(dec.get("confidence", 0) or 0)
        reasoning = str(dec.get("reasoning", ""))[:300]
        price = float(prices.get(symbol, 0.0))
        already = float(held.get(symbol, {}).get("amount", 0.0))
        is_new = symbol not in open_symbols

        # Max positions cap applies only to *new* names.
        if is_new and len(open_symbols) >= settings.max_positions:
            plan.skipped.append(SkippedDecision(symbol, "buy",
                f"max_positions {settings.max_positions} reached"))
            continue

        # Cap by remaining room in this single name.
        room_position = max(0.0, per_position_cap - already)
        # Cap by remaining room in total invested.
        room_invested = max(0.0, max_invested_cap - running_invested)
        # Cap by cash on hand.
        room_cash = max(0.0, running_cash)

        amount = min(desired, room_position, room_invested, room_cash)

        if amount < settings.min_order_usd:
            reason = "below min order size"
            if room_position < settings.min_order_usd:
                reason = f"per-position cap ({settings.max_position_pct:.0%}) leaves no room"
            elif room_invested < settings.min_order_usd:
                reason = f"max invested ({settings.max_invested_pct:.0%}) reached"
            elif room_cash < settings.min_order_usd:
                reason = "insufficient cash"
            plan.skipped.append(SkippedDecision(symbol, "buy", reason))
            continue

        stop_loss_rate = None
        if settings.stop_loss_pct and settings.stop_loss_pct > 0 and price > 0:
            stop_loss_rate = round(price * (1.0 - settings.stop_loss_pct), 4)

        plan.orders.append(PlannedOrder(
            symbol=symbol, side="buy", amount_usd=round(amount, 2), price=price,
            confidence=conf, source_action="buy", reasoning=reasoning,
            stop_loss_rate=stop_loss_rate,
        ))
        running_cash -= amount
        running_invested += amount
        open_symbols.add(symbol)

    plan.projected_invested_after = running_invested
    plan.notes.append(
        f"budget=${budget:,.0f} per_position_cap=${per_position_cap:,.0f} "
        f"max_invested=${max_invested_cap:,.0f} cash_avail=${cash_available:,.0f}"
    )
    return plan
