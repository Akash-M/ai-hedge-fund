"""End-to-end offline test of run_once() with a mocked eToro client.

Proves the full wiring (portfolio sync -> decisions file -> guardrails -> executor
-> state log) works without network, credentials, or an LLM.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from live.config import Settings
import live.run_cycle as rc


class FakeClient:
    def __init__(self, settings):
        self.s = settings

    def close(self):
        pass

    def get_portfolio(self):
        return {
            "cash": 1000.0, "equity": 1000.0, "currency": "USD",
            "positions": [
                {"instrument_id": 1001, "position_id": "p1", "is_buy": True,
                 "units": 2.0, "amount": 300.0, "open_rate": 150.0, "pnl": 0.0},
            ],
            "raw": {},
        }

    def resolve_symbols(self, symbols):
        table = {"NVDA": 2000, "AAPL": 1001, "MSFT": 3000}
        return {s.upper(): table[s.upper()] for s in symbols if s.upper() in table}

    def get_instrument_rate(self, iid):
        return {2000: 100.0, 1001: 160.0, 3000: 100.0}.get(iid)

    def open_market_position(self, **kw):
        return {"dry_run": True, "request": kw}

    def close_position(self, position_id, instrument_id=None):
        return {"dry_run": True, "request": {"positionId": position_id}}


def run():
    tmp = tempfile.mkdtemp(prefix="aihf_test_")
    decisions = {"decisions": {
        "NVDA": {"action": "buy", "quantity": 100, "confidence": 80, "reasoning": "AI leader breakout"},
        "AAPL": {"action": "sell", "quantity": 0, "confidence": 70, "reasoning": "take profit"},
        "MSFT": {"action": "buy", "quantity": 1, "confidence": 50, "reasoning": "cloud growth"},
    }}
    dfile = os.path.join(tmp, "decisions.json")
    with open(dfile, "w") as f:
        json.dump(decisions, f)

    os.environ["AIHF_DECISIONS_FILE"] = dfile

    settings = Settings(
        etoro_api_key="x", etoro_user_key="y", environment="demo",
        budget_usd=1000.0, universe=["NVDA", "AAPL", "MSFT"],
        risk_profile="aggressive", max_position_pct=0.25, max_positions=6,
        max_invested_pct=1.0, min_order_usd=50.0, stop_loss_pct=0.30,
        min_confidence=45, dry_run=True, state_dir=tmp,
    )

    # Inject the fake client.
    rc.EToroClient = FakeClient

    cycle = rc.run_once(settings)

    results = {(r["symbol"], r["side"]): r for r in cycle["results"]}
    print(json.dumps(cycle["results"], indent=2, default=str))

    # NVDA buy capped to 25% of $1000 = $250
    nvda = results[("NVDA", "buy")]
    assert nvda["status"] == "dry_run" and abs(nvda["amount_usd"] - 250.0) < 1e-6, nvda
    assert abs(nvda["stop_loss_rate"] - 70.0) < 1e-6, nvda  # 100*(1-0.30)

    # AAPL sell -> close
    aapl = results[("AAPL", "close")]
    assert aapl["status"] == "dry_run", aapl

    # MSFT buy = qty 1 * $100 = $100
    msft = results[("MSFT", "buy")]
    assert abs(msft["amount_usd"] - 100.0) < 1e-6, msft

    # State log written
    assert os.path.exists(os.path.join(tmp, "cycles.jsonl"))
    assert os.path.exists(os.path.join(tmp, "latest.json"))

    print("\nALL OFFLINE CYCLE ASSERTIONS PASSED")
    del os.environ["AIHF_DECISIONS_FILE"]


if __name__ == "__main__":
    run()
