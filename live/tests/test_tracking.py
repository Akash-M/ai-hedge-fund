"""Tests for per-run log files (state_store) and the static report (report_html)."""
from __future__ import annotations

import glob
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from live.config import Settings
from live.state_store import record_cycle
from live.report_html import write_report, render_html, load_cycles


def _settings(state_dir):
    return Settings(etoro_api_key="x", etoro_user_key="y", environment="demo",
                    dry_run=True, state_dir=state_dir)


def _cycle(equity, buys=()):
    return {
        "account": {"cash": 100.0, "equity": equity, "currency": "USD", "open_positions": len(buys)},
        "budget": 500.0,
        "results": [
            {"symbol": s, "side": "buy", "status": "dry_run", "amount_usd": amt,
             "confidence": 80, "reasoning": "growth"} for s, amt in buys
        ],
    }


def test_per_run_files_and_consolidated_log():
    tmp = tempfile.mkdtemp(prefix="aihf_track_")
    s = _settings(tmp)
    record_cycle(s, _cycle(500.0, [("NVDA", 125.0)]))
    record_cycle(s, _cycle(530.0, [("MSFT", 100.0)]))

    run_files = sorted(glob.glob(os.path.join(tmp, "run-*.json")))
    assert len(run_files) == 2, run_files
    assert os.path.exists(os.path.join(tmp, "cycles.jsonl"))
    assert os.path.exists(os.path.join(tmp, "latest.json"))

    cycles = load_cycles(tmp)
    assert len(cycles) == 2
    latest = json.load(open(os.path.join(tmp, "latest.json")))
    assert latest["account"]["equity"] == 530.0
    print("OK per-run files (%d) + consolidated log + latest snapshot" % len(run_files))


def test_report_html_renders_equity_and_trades():
    tmp = tempfile.mkdtemp(prefix="aihf_report_")
    s = _settings(tmp)
    record_cycle(s, _cycle(500.0, [("NVDA", 125.0)]))
    record_cycle(s, _cycle(515.0, [("AMD", 90.0)]))
    record_cycle(s, _cycle(540.0))

    path = write_report(tmp)
    assert path and os.path.exists(path), path
    html_text = open(path).read()
    assert "AI Hedge Fund" in html_text
    assert "<svg" in html_text                  # equity curve drew (>=2 points)
    assert "540" in html_text                    # latest equity surfaced
    assert "NVDA" in html_text and "AMD" in html_text  # trades + reasoning in table
    assert "+8.00%" in html_text                 # (540-500)/500
    print("OK report renders equity curve, change %, and trade reasoning ->", os.path.basename(path))


def test_report_handles_single_point():
    # < 2 equity points => no SVG, graceful message, still renders.
    cycles = [_cycle(500.0, [("NVDA", 125.0)])]
    out = render_html(cycles)
    assert "Not enough data yet" in out
    print("OK report degrades gracefully with one data point")


if __name__ == "__main__":
    test_per_run_files_and_consolidated_log()
    test_report_html_renders_equity_and_trades()
    test_report_handles_single_point()
    print("\nALL TRACKING TESTS PASSED")
