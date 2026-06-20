"""Configuration for the live eToro trading bridge.

All settings are read from environment variables so secrets never live in code.
A risk *profile* provides sensible defaults; any explicit env var overrides it.

This module has no third-party dependencies (stdlib only) so it can be imported
and unit-tested in isolation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _get_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Risk profiles
# ---------------------------------------------------------------------------
# Each profile defines DEFAULTS only. Explicit env vars always win.
# "aggressive" = larger single-name bets, more concurrent positions, growth
# universe. Even aggressive keeps HARD caps so a bad LLM call cannot blow up
# the account.
RISK_PROFILES = {
    "conservative": dict(
        max_position_pct=0.08,   # <=8% of budget in any one name
        max_positions=12,        # diversify widely
        max_invested_pct=0.80,   # keep >=20% cash buffer
        stop_loss_pct=0.15,
        min_confidence=60,       # ignore weak signals
    ),
    "balanced": dict(
        max_position_pct=0.15,
        max_positions=8,
        max_invested_pct=0.90,
        stop_loss_pct=0.20,
        min_confidence=55,
    ),
    "aggressive": dict(
        max_position_pct=0.25,   # concentrate up to 25% in a high-conviction name
        max_positions=6,         # fewer, bigger bets
        max_invested_pct=1.00,   # fully invest the budget when signals are strong
        stop_loss_pct=0.30,      # wider stop so volatile growth names aren't shaken out
        min_confidence=45,       # act on more signals
    ),
}

# A default growth-oriented universe (US large/mega-cap growth + a couple of ETFs).
# Restricted to names that have fundamental data coverage so every agent can reason.
DEFAULT_UNIVERSE = [
    "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA", "AMD",
    "AVGO", "CRM", "NFLX", "QQQ",
]


@dataclass
class Settings:
    # --- eToro credentials & environment ---
    etoro_api_key: str = ""
    etoro_user_key: str = ""
    environment: str = "demo"          # "demo" or "real"

    # --- capital & universe ---
    budget_usd: float = 0.0            # 0 => use the account's available buying power
    universe: List[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    base_currency: str = "usd"

    # --- risk guardrails (filled from profile, overridable) ---
    risk_profile: str = "aggressive"
    max_position_pct: float = 0.25
    max_positions: int = 6
    max_invested_pct: float = 1.00
    min_order_usd: float = 50.0        # eToro minimum order is ~$50 for stocks
    stop_loss_pct: float = 0.30        # 0 => no stop loss attached
    leverage: int = 1                  # 1 = no leverage (cash equity). Keep at 1.
    allow_short: bool = False          # long-only by default (shorting => CFD/leverage)
    min_confidence: int = 45           # skip decisions below this confidence

    # --- decision engine ---
    llm_model: str = "gpt-4.1-mini"
    llm_provider: str = "OpenAI"
    # Ordered "Provider:model" fallbacks tried on rate-limit/quota errors.
    # Empty => auto-build from providers whose API keys are present.
    llm_fallbacks: str = ""
    llm_cooldown_s: int = 90           # bench a provider this long after it 429s
    lookback_months: int = 6           # price history window for vol/correlation
    selected_analysts: List[str] = field(default_factory=list)  # [] => all analysts

    # --- operations ---
    dry_run: bool = True               # compute + log orders but DO NOT send them
    state_dir: str = "./live_state"
    notify_webhook: str = ""           # optional Slack/Discord/generic webhook
    http_timeout: float = 30.0
    max_retries: int = 4

    # ---- derived ----
    @property
    def is_real(self) -> bool:
        return self.environment.strip().lower() == "real"

    @property
    def env_segment(self) -> str:
        """Path segment inserted for demo; empty for real."""
        return "" if self.is_real else "demo"

    def validate(self) -> List[str]:
        """Return a list of human-readable problems; empty list means OK."""
        problems: List[str] = []
        if not self.etoro_api_key:
            problems.append("ETORO_API_KEY is not set.")
        if not self.etoro_user_key:
            problems.append("ETORO_USER_KEY is not set.")
        if self.environment.lower() not in {"demo", "real"}:
            problems.append("ETORO_ENVIRONMENT must be 'demo' or 'real'.")
        if not (0 < self.max_position_pct <= 1):
            problems.append("max_position_pct must be in (0, 1].")
        if self.max_positions < 1:
            problems.append("max_positions must be >= 1.")
        if self.leverage != 1 and not self.allow_short:
            problems.append("leverage must be 1 unless you explicitly enable margin/short.")
        if not self.universe:
            problems.append("Trading universe is empty.")
        return problems

    def redacted(self) -> dict:
        d = asdict(self)
        if d.get("etoro_api_key"):
            d["etoro_api_key"] = d["etoro_api_key"][:3] + "***"
        if d.get("etoro_user_key"):
            d["etoro_user_key"] = d["etoro_user_key"][:3] + "***"
        if d.get("notify_webhook"):
            d["notify_webhook"] = "***set***"
        return d

    @classmethod
    def from_env(cls) -> "Settings":
        profile = (os.getenv("AIHF_RISK_PROFILE", "aggressive") or "aggressive").strip().lower()
        preset = RISK_PROFILES.get(profile, RISK_PROFILES["aggressive"])

        return cls(
            etoro_api_key=os.getenv("ETORO_API_KEY", "").strip(),
            etoro_user_key=os.getenv("ETORO_USER_KEY", "").strip(),
            environment=(os.getenv("ETORO_ENVIRONMENT", "demo") or "demo").strip().lower(),

            budget_usd=_get_float("AIHF_BUDGET_USD", 0.0),
            universe=_get_list("AIHF_TICKERS", DEFAULT_UNIVERSE),
            base_currency=(os.getenv("AIHF_BASE_CURRENCY", "usd") or "usd").strip().lower(),

            risk_profile=profile,
            max_position_pct=_get_float("AIHF_MAX_POSITION_PCT", preset["max_position_pct"]),
            max_positions=_get_int("AIHF_MAX_POSITIONS", preset["max_positions"]),
            max_invested_pct=_get_float("AIHF_MAX_INVESTED_PCT", preset["max_invested_pct"]),
            min_order_usd=_get_float("AIHF_MIN_ORDER_USD", 50.0),
            stop_loss_pct=_get_float("AIHF_STOP_LOSS_PCT", preset["stop_loss_pct"]),
            leverage=_get_int("AIHF_LEVERAGE", 1),
            allow_short=_get_bool("AIHF_ALLOW_SHORT", False),
            min_confidence=_get_int("AIHF_MIN_CONFIDENCE", preset["min_confidence"]),

            llm_model=os.getenv("AIHF_LLM_MODEL", "gpt-4.1-mini").strip(),
            llm_provider=os.getenv("AIHF_LLM_PROVIDER", "OpenAI").strip(),
            llm_fallbacks=os.getenv("AIHF_LLM_FALLBACKS", "").strip(),
            llm_cooldown_s=_get_int("AIHF_LLM_COOLDOWN_S", 90),
            lookback_months=_get_int("AIHF_LOOKBACK_MONTHS", 6),
            selected_analysts=_get_list("AIHF_ANALYSTS", []),

            dry_run=_get_bool("AIHF_DRY_RUN", True),
            state_dir=os.getenv("AIHF_STATE_DIR", "./live_state").strip(),
            notify_webhook=os.getenv("AIHF_NOTIFY_WEBHOOK", "").strip(),
            http_timeout=_get_float("AIHF_HTTP_TIMEOUT", 30.0),
            max_retries=_get_int("AIHF_MAX_RETRIES", 4),
        )


if __name__ == "__main__":
    import json
    s = Settings.from_env()
    print(json.dumps(s.redacted(), indent=2, default=str))
    problems = s.validate()
    print("\nVALIDATION:", "OK" if not problems else problems)
