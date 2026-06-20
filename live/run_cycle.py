"""Run ONE trading cycle, then exit. Designed to be invoked by a scheduler
(cron / PaaS cron / Railway / Render) once per trading day.

Flow:
  1. Read live eToro portfolio (also the credential health check).
  2. Resolve our universe to eToro instrumentIds.
  3. Build the hedge fund's portfolio dict, scaled to our managed budget.
  4. Run the AI hedge fund decision engine (or load a decisions file for testing).
  5. Apply deterministic risk guardrails -> capped, cash-denominated orders.
  6. Execute on eToro (or DRY-RUN), log every action, optionally notify.

Safety:
  * Defaults to DRY-RUN. Set AIHF_DRY_RUN=false to actually place orders.
  * Live (real-money) trading additionally requires AIHF_CONFIRM_LIVE=I_UNDERSTAND.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

from live.config import Settings
from live.etoro_client import EToroClient, EToroAPIError
from live import portfolio_sync as psync
from live.risk_guardrails import build_execution_plan
from live.executor import execute_plan
from live.state_store import record_cycle
from live.notifier import notify, format_summary
from live import llm_failover

logger = logging.getLogger("run_cycle")


def _date_window(lookback_months: int) -> Tuple[str, str]:
    end = datetime.utcnow().date()
    try:
        from dateutil.relativedelta import relativedelta
        start = end - relativedelta(months=lookback_months)
    except Exception:
        start = end - timedelta(days=int(lookback_months * 31))
    return start.isoformat(), end.isoformat()


def get_decisions_and_prices(
    settings: Settings,
    client: EToroClient,
    universe: List[str],
    hf_portfolio: Dict[str, Any],
    symbol_to_iid: Dict[str, int],
) -> Tuple[Dict[str, Any], Dict[str, float], Dict[str, Any]]:
    """Return (decisions, prices, analyst_signals)."""
    # --- Test/inspection mode: skip the LLM, load decisions from a file. ---
    decisions_file = os.getenv("AIHF_DECISIONS_FILE", "").strip()
    if decisions_file:
        logger.info("Loading decisions from %s (LLM bypassed)", decisions_file)
        with open(decisions_file, "r") as f:
            decisions = json.load(f)
        decisions = decisions.get("decisions", decisions)
        prices: Dict[str, float] = {}
        for sym in universe:
            iid = symbol_to_iid.get(sym.upper())
            if iid is not None:
                try:
                    rate = client.get_instrument_rate(iid)
                    if rate:
                        prices[sym.upper()] = rate
                except EToroAPIError:
                    pass
        return decisions, prices, {}

    # --- Normal mode: run the AI hedge fund. ---
    start_date, end_date = _date_window(settings.lookback_months)
    logger.info("Running hedge fund engine %s..%s on %d tickers (model=%s/%s)",
                start_date, end_date, len(universe), settings.llm_provider, settings.llm_model)

    # Install multi-provider LLM failover BEFORE importing the agents — they bind
    # call_llm at import time, so the patch must precede `import src.main`.
    from live.llm_failover import install_llm_failover
    chain = install_llm_failover(settings)
    if chain:
        logger.info("LLM failover chain (in order): %s", chain)

    from src.main import run_hedge_fund  # heavy import (langchain/langgraph)

    result = run_hedge_fund(
        tickers=[s.upper() for s in universe],
        start_date=start_date,
        end_date=end_date,
        portfolio=hf_portfolio,
        show_reasoning=False,
        selected_analysts=settings.selected_analysts or [],
        model_name=settings.llm_model,
        model_provider=settings.llm_provider,
    )
    decisions = result.get("decisions") or {}
    signals = result.get("analyst_signals") or {}

    # Prices come from the risk manager's output; fall back to eToro rates.
    prices = {}
    risk_signals = signals.get("risk_management_agent", {})
    for sym in universe:
        sym = sym.upper()
        cp = float((risk_signals.get(sym) or {}).get("current_price", 0.0) or 0.0)
        if cp <= 0:
            iid = symbol_to_iid.get(sym)
            if iid is not None:
                try:
                    cp = client.get_instrument_rate(iid) or 0.0
                except EToroAPIError:
                    cp = 0.0
        if cp > 0:
            prices[sym] = cp
    return decisions, prices, signals


def run_once(settings: Settings) -> Dict[str, Any]:
    problems = settings.validate()
    if problems:
        raise SystemExit("Config errors:\n  - " + "\n  - ".join(problems))

    # Hard gate on real-money trading.
    if settings.is_real and not settings.dry_run:
        if os.getenv("AIHF_CONFIRM_LIVE", "").strip() != "I_UNDERSTAND":
            raise SystemExit(
                "Refusing to trade REAL money without AIHF_CONFIRM_LIVE=I_UNDERSTAND. "
                "Validate on demo first."
            )

    universe = [s.upper() for s in settings.universe]
    client = EToroClient(settings)
    try:
        logger.info("Fetching eToro portfolio (%s)...", settings.environment)
        account = client.get_portfolio()
        logger.info("Account: cash=%.2f equity=%.2f positions=%d",
                    account.get("cash", 0), account.get("equity", 0),
                    len(account.get("positions", [])))

        symbol_to_iid = client.resolve_symbols(universe)
        missing = [s for s in universe if s not in symbol_to_iid]
        if missing:
            logger.warning("Could not resolve instrumentIds for: %s", missing)
        iid_to_symbol = {v: k for k, v in symbol_to_iid.items()}

        held = psync.aggregate_holdings(account, iid_to_symbol)
        budget, cash_for_buys = psync.resolve_budget(settings, account, held)
        hf_portfolio = psync.build_hedge_fund_portfolio(settings, universe, held, cash_for_buys)

        decisions, prices, signals = get_decisions_and_prices(
            settings, client, universe, hf_portfolio, symbol_to_iid)

        gholds = psync.held_for_guardrails(held)
        plan = build_execution_plan(decisions, prices, gholds, cash_for_buys, budget, settings)

        results = execute_plan(client, plan, held, symbol_to_iid, settings)

        cycle: Dict[str, Any] = {
            "environment": settings.environment,
            "dry_run": settings.dry_run,
            "budget": budget,
            "cash_for_buys": cash_for_buys,
            "account": {"cash": account.get("cash"), "equity": account.get("equity"),
                        "credit": account.get("credit"), "unrealized_pnl": account.get("unrealized_pnl"),
                        "currency": account.get("currency"),
                        "open_positions": len(account.get("positions", []))},
            "universe": universe,
            "resolved_instruments": symbol_to_iid,
            "llm_chain": llm_failover.build_chain(
                (settings.llm_provider, settings.llm_model),
                os.getenv("AIHF_LLM_FALLBACKS", "")),
            "decisions": decisions,
            "plan_notes": plan.notes,
            "results": results,
        }
        record_cycle(settings, cycle)
        notify(settings, cycle)
        try:
            from live.report_html import write_report
            report_path = write_report(settings.state_dir)
            if report_path:
                logger.info("Progress report written to %s", report_path)
        except Exception as e:  # a report failure must never break trading
            logger.warning("Report generation failed: %s", e)
        return cycle
    finally:
        client.close()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one AI-hedge-fund -> eToro trading cycle")
    parser.add_argument("--live", action="store_true", help="Place real orders (overrides AIHF_DRY_RUN)")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run (no orders sent)")
    parser.add_argument("--print", dest="do_print", action="store_true", help="Print summary to stdout")
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(
        level=os.getenv("AIHF_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    if args.live:
        settings.dry_run = False
    if args.dry_run:
        settings.dry_run = True

    logger.info("Settings: %s", json.dumps(settings.redacted(), default=str))
    try:
        cycle = run_once(settings)
    except Exception as e:
        # Leave a committed trace for genuine failures (won't capture hard timeouts,
        # which SIGTERM the process, but does capture bad keys / API errors / bugs).
        logger.exception("Trading cycle failed")
        try:
            import traceback
            record_cycle(settings, {"error": str(e),
                                    "traceback": traceback.format_exc()[:4000],
                                    "results": []})
        except Exception:
            pass
        raise
    if args.do_print:
        print("\n" + format_summary(cycle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
