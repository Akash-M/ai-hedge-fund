"""Generate a self-contained static progress report (runs/index.html) from the
cycle log. No backend, no JS dependencies — just an inline-SVG equity curve plus
a per-run trade table. Regenerated each cycle and committed alongside the logs.

The authoritative portfolio view lives in the eToro app; this adds the equity
trend over time and the agent's reasoning per trade in one glanceable page.
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def load_cycles(state_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(state_dir, "cycles.jsonl")
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _equity(c: Dict[str, Any]) -> Optional[float]:
    acct = c.get("account", {}) or {}
    for key in ("equity", "cash"):
        v = acct.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts


def _svg_equity_curve(series: List[Tuple[str, float]], w: int = 820, h: int = 260) -> str:
    if len(series) < 2:
        return '<p class="muted">Not enough data yet — the equity curve appears after a few cycles.</p>'
    pad = 36
    values = [v for _, v in series]
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        vmax = vmin + 1.0
    n = len(series)

    def x(i: int) -> float:
        return pad + (w - 2 * pad) * (i / (n - 1))

    def y(v: float) -> float:
        return pad + (h - 2 * pad) * (1 - (v - vmin) / (vmax - vmin))

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(series))
    up = series[-1][1] >= series[0][1]
    color = "#16a34a" if up else "#dc2626"
    area = f"{pad},{h - pad} " + pts + f" {x(n - 1):.1f},{h - pad}"
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet" role="img">
  <polygon points="{area}" fill="{color}" opacity="0.08"/>
  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"/>
  <line x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}" stroke="#e5e7eb"/>
  <text x="{pad}" y="{pad-14}" fill="#6b7280" font-size="12">{vmax:,.2f}</text>
  <text x="{pad}" y="{h-pad+18}" fill="#6b7280" font-size="12">{vmin:,.2f}</text>
  <text x="{pad}" y="{h-8}" fill="#9ca3af" font-size="11">{html.escape(_fmt_ts(series[0][0]))}</text>
  <text x="{w-pad}" y="{h-8}" fill="#9ca3af" font-size="11" text-anchor="end">{html.escape(_fmt_ts(series[-1][0]))}</text>
  <circle cx="{x(n-1):.1f}" cy="{y(series[-1][1]):.1f}" r="3.5" fill="{color}"/>
</svg>"""


def _rows(cycles: List[Dict[str, Any]], limit: int = 40) -> str:
    rows = []
    for c in reversed(cycles[-limit:]):
        results = c.get("results", []) or []
        buys = [r for r in results if r.get("side") == "buy" and r.get("status") in ("placed", "dry_run")]
        closes = [r for r in results if r.get("side") == "close" and r.get("status") in ("closed", "dry_run")]
        errs = [r for r in results if r.get("status") == "error"]
        deployed = sum(float(r.get("amount_usd", 0) or 0) for r in buys)
        eq = _equity(c)
        trades = []
        for r in buys:
            trades.append(f"<b>BUY</b> {html.escape(str(r.get('symbol','')))} "
                          f"${float(r.get('amount_usd',0)):,.0f} "
                          f"<span class='muted'>({html.escape(str(r.get('reasoning',''))[:60])})</span>")
        for r in closes:
            trades.append(f"<b>CLOSE</b> {html.escape(str(r.get('symbol','')))} "
                          f"<span class='muted'>({html.escape(str(r.get('reasoning',''))[:60])})</span>")
        badge = "DRY" if c.get("dry_run") else "LIVE"
        rows.append(
            f"<tr><td>{html.escape(_fmt_ts(c.get('timestamp','')))}</td>"
            f"<td><span class='badge'>{html.escape(str(c.get('environment','')))}/{badge}</span></td>"
            f"<td class='num'>{(f'${eq:,.2f}') if eq else '—'}</td>"
            f"<td class='num'>${deployed:,.0f}</td>"
            f"<td class='num'>{len(buys)}/{len(closes)}/{len(errs)}</td>"
            f"<td>{'<br>'.join(trades) if trades else '<span class=muted>no trades</span>'}</td></tr>"
        )
    return "\n".join(rows)


def render_html(cycles: List[Dict[str, Any]], title: str = "AI Hedge Fund — Progress") -> str:
    series = [(c.get("timestamp", ""), e) for c in cycles if (e := _equity(c)) is not None]
    first_eq = series[0][1] if series else None
    last_eq = series[-1][1] if series else None
    pct = ((last_eq - first_eq) / first_eq * 100) if (first_eq and last_eq) else None
    total_trades = sum(
        len([r for r in (c.get("results", []) or [])
             if r.get("side") in ("buy", "close") and r.get("status") in ("placed", "closed", "dry_run")])
        for c in cycles
    )
    pct_str = f"{pct:+.2f}%" if pct is not None else "—"
    pct_color = "#16a34a" if (pct or 0) >= 0 else "#dc2626"
    updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>
  :root {{ --ink:#111827; --muted:#6b7280; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; color:var(--ink);
         margin:0; background:#f9fafb; padding:0 5%; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:32px 0 64px; }}
  h1 {{ font-size:1.5rem; margin:0 0 4px; }}
  .muted {{ color:var(--muted); font-size:0.85em; }}
  .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin:20px 0; }}
  .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:16px 20px; flex:1; min-width:150px; }}
  .card .label {{ color:var(--muted); font-size:0.8rem; text-transform:uppercase; letter-spacing:.04em; }}
  .card .value {{ font-size:1.5rem; font-weight:600; margin-top:4px; }}
  .panel {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:20px; margin:16px 0; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.88rem; }}
  th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #f0f0f0; vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; font-size:0.78rem; text-transform:uppercase; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .badge {{ background:#eef2ff; color:#4338ca; border-radius:6px; padding:2px 8px; font-size:0.75rem; }}
  .disc {{ font-size:0.78rem; color:var(--muted); margin-top:24px; }}
</style></head>
<body><div class="wrap">
  <h1>{html.escape(title)}</h1>
  <div class="muted">Updated {updated} · {len(cycles)} cycles logged · authoritative portfolio view: your eToro app</div>

  <div class="cards">
    <div class="card"><div class="label">Latest equity</div>
      <div class="value">{(f"${last_eq:,.2f}") if last_eq else '—'}</div></div>
    <div class="card"><div class="label">Change since start</div>
      <div class="value" style="color:{pct_color}">{pct_str}</div></div>
    <div class="card"><div class="label">Cycles</div><div class="value">{len(cycles)}</div></div>
    <div class="card"><div class="label">Trades placed</div><div class="value">{total_trades}</div></div>
  </div>

  <div class="panel">
    <div class="muted" style="margin-bottom:8px">Account equity over time</div>
    {_svg_equity_curve(series)}
  </div>

  <div class="panel">
    <table>
      <thead><tr><th>Time (UTC)</th><th>Mode</th><th>Equity</th><th>Deployed</th>
        <th>B/C/E</th><th>Trades &amp; reasoning</th></tr></thead>
      <tbody>{_rows(cycles)}</tbody>
    </table>
  </div>

  <div class="disc">Educational software. Not investment advice. Equity/PnL shown here is derived from the
    bot's cycle logs; your eToro account is the source of truth. B/C/E = buys / closes / errors.</div>
</div></body></html>"""


def write_report(state_dir: str, filename: str = "index.html") -> Optional[str]:
    cycles = load_cycles(state_dir)
    if not cycles:
        return None
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, filename)
    with open(path, "w") as f:
        f.write(render_html(cycles))
    return path


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Generate static progress report from cycle logs")
    p.add_argument("--state-dir", default=os.getenv("AIHF_STATE_DIR", "runs"))
    args = p.parse_args(argv)
    path = write_report(args.state_dir)
    print(f"Wrote {path}" if path else "No cycles to report yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
