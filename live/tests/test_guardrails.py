"""Deterministic tests for the risk guardrail layer (no network/LLM)."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from live.config import Settings
from live.risk_guardrails import build_execution_plan


def aggressive_settings(**over):
    s = Settings(
        etoro_api_key="x", etoro_user_key="y", environment="demo",
        risk_profile="aggressive", max_position_pct=0.25, max_positions=6,
        max_invested_pct=1.00, min_order_usd=50.0, stop_loss_pct=0.30,
        min_confidence=45, dry_run=True,
    )
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_per_position_cap_and_stop_loss():
    s = aggressive_settings()
    decisions = {"NVDA": {"action": "buy", "quantity": 100, "confidence": 80, "reasoning": "strong"}}
    prices = {"NVDA": 100.0}
    plan = build_execution_plan(decisions, prices, held={}, cash_available=1000.0, budget=1000.0, settings=s)
    assert len(plan.orders) == 1, plan
    o = plan.orders[0]
    assert o.side == "buy"
    # desired $10,000 capped to 25% of $1000 = $250
    assert abs(o.amount_usd - 250.0) < 1e-6, o.amount_usd
    # stop loss = 100 * (1 - 0.30) = 70
    assert abs(o.stop_loss_rate - 70.0) < 1e-6, o.stop_loss_rate
    print("OK per-position cap + stop loss -> $%.2f, SL %.2f" % (o.amount_usd, o.stop_loss_rate))


def test_confidence_floor_and_short_skip():
    s = aggressive_settings()
    decisions = {
        "TSLA": {"action": "buy", "quantity": 5, "confidence": 30, "reasoning": "meh"},
        "AMZN": {"action": "short", "quantity": 5, "confidence": 90, "reasoning": "bearish"},
    }
    prices = {"TSLA": 100.0, "AMZN": 100.0}
    plan = build_execution_plan(decisions, prices, held={}, cash_available=1000.0, budget=1000.0, settings=s)
    assert len(plan.orders) == 0, plan.orders
    reasons = {sk.symbol: sk.reason for sk in plan.skipped}
    assert "confidence" in reasons["TSLA"], reasons
    assert "long-only" in reasons["AMZN"], reasons
    print("OK confidence floor skip + short skip:", reasons)


def test_max_invested_and_cash_caps():
    s = aggressive_settings(max_position_pct=1.0, max_positions=10)  # let total caps bind
    decisions = {
        "A": {"action": "buy", "quantity": 100, "confidence": 90, "reasoning": ""},
        "B": {"action": "buy", "quantity": 100, "confidence": 80, "reasoning": ""},
        "C": {"action": "buy", "quantity": 100, "confidence": 70, "reasoning": ""},
    }
    prices = {"A": 100.0, "B": 100.0, "C": 100.0}
    # budget 1000, fully investable, but only $600 cash on hand
    plan = build_execution_plan(decisions, prices, held={}, cash_available=600.0, budget=1000.0, settings=s)
    total = sum(o.amount_usd for o in plan.orders if o.side == "buy")
    assert total <= 600.0 + 1e-6, total
    # highest-confidence names funded first
    assert plan.orders[0].symbol == "A"
    print("OK cash cap: total deployed $%.2f across %d orders" % (total, len(plan.orders)))


def test_sell_closes_position_and_frees_slot():
    s = aggressive_settings(max_positions=1)  # full
    held = {"NVDA": {"amount": 250.0, "is_buy": True, "position_id": "p1"}}
    decisions = {
        "NVDA": {"action": "sell", "quantity": 0, "confidence": 60, "reasoning": "take profit"},
        "MSFT": {"action": "buy", "quantity": 1, "confidence": 90, "reasoning": "rotate in"},
    }
    prices = {"NVDA": 110.0, "MSFT": 100.0}
    plan = build_execution_plan(decisions, prices, held=held, cash_available=100.0, budget=1000.0, settings=s)
    sides = [(o.symbol, o.side) for o in plan.orders]
    assert ("NVDA", "close") in sides, sides
    # closing NVDA frees the single slot AND adds cash, so MSFT buy can proceed
    assert ("MSFT", "buy") in sides, sides
    print("OK sell closes + frees slot for rotation:", sides)


if __name__ == "__main__":
    test_per_position_cap_and_stop_loss()
    test_confidence_floor_and_short_skip()
    test_max_invested_and_cash_caps()
    test_sell_closes_position_and_frees_slot()
    print("\nALL GUARDRAIL TESTS PASSED")
