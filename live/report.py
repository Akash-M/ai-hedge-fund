"""Quick review tool: summarize recent trading cycles from the state log.

Usage:
  python -m live.report                 # summarize last 10 cycles
  python -m live.report --n 30          # last 30
  python -m live.report --state-dir DIR
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List


def load_cycles(state_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(state_dir, "cycles.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def summarize(cycles: List[Dict[str, Any]], n: int) -> str:
    if not cycles:
        return "No cycles recorded yet."
    lines: List[str] = []
    for c in cycles[-n:]:
        ts = c.get("timestamp", "?")
        env = c.get("environment", "?")
        dry = "DRY" if c.get("dry_run") else "LIVE"
        results = c.get("results", [])
        buys = [r for r in results if r.get("side") == "buy" and r.get("status") in ("placed", "dry_run")]
        closes = [r for r in results if r.get("side") == "close" and r.get("status") in ("closed", "dry_run")]
        errs = [r for r in results if r.get("status") == "error"]
        deployed = sum(float(r.get("amount_usd", 0) or 0) for r in buys)
        acct = c.get("account", {})
        lines.append(
            f"{ts} [{env}/{dry}] equity=${acct.get('equity', 0):,.0f} "
            f"cash=${acct.get('cash', 0):,.0f} | buys={len(buys)} (${deployed:,.0f}) "
            f"closes={len(closes)} errors={len(errs)}"
        )
        for r in buys:
            lines.append(f"      + {r['symbol']:<6} ${float(r.get('amount_usd', 0)):>8,.2f} "
                         f"conf={r.get('confidence', '?')}  {str(r.get('reasoning', ''))[:50]}")
        for r in closes:
            lines.append(f"      - {r['symbol']:<6} close            {str(r.get('reasoning', ''))[:50]}")
        for r in errs[:5]:
            lines.append(f"      ! {r.get('symbol', '?'):<6} ERROR  {str(r.get('detail'))[:60]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Summarize AI hedge fund trading cycles")
    p.add_argument("--state-dir", default=os.getenv("AIHF_STATE_DIR", "./live_state"))
    p.add_argument("--n", type=int, default=10)
    args = p.parse_args(argv)
    cycles = load_cycles(args.state_dir)
    print(f"Loaded {len(cycles)} cycle(s) from {args.state_dir}\n")
    print(summarize(cycles, args.n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
