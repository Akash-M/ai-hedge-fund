"""Thin, defensive wrapper around the eToro public trading API.

Docs: https://builders.etoro.com  /  https://api-portal.etoro.com
Bases:
  v1  https://public-api.etoro.com/api/v1   (portfolio + market data)
  v2  https://public-api.etoro.com/api/v2   (unified order execution)

Auth: every request sends x-api-key, x-user-key and a unique x-request-id.

NOTE ON SCHEMA: eToro's public docs show some inconsistency in field casing
(e.g. `instrumentId` vs `InstrumentID`, `amount` vs `Amount`). This client sends
the v2 unified-order shape by default and reads responses defensively via
`_first(...)`, which tries several key spellings. The first thing the bot does on
startup is call `get_portfolio()` as a health check; if any field mapping is off,
that surfaces immediately on the DEMO account before a single real dollar moves.
"""
from __future__ import annotations

import os
import time
import uuid
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from live.config import Settings

logger = logging.getLogger("etoro")

V1 = "https://public-api.etoro.com/api/v1"
V2 = "https://public-api.etoro.com/api/v2"


class EToroAPIError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _first(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present key from a dict, trying case-insensitive too."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d:
            return d[k]
    lowered = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in lowered:
            return lowered[k.lower()]
    return default


class EToroClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = httpx.Client(timeout=settings.http_timeout, trust_env=True)
        self._instrument_cache: Dict[str, int] = {}
        self._cache_path = os.path.join(settings.state_dir, "instrument_cache.json")
        self._load_cache()

    # ----- lifecycle -----
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "EToroClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- low level -----
    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.s.etoro_api_key,
            "x-user-key": self.s.etoro_user_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, url: str, *, params: Optional[dict] = None,
                 body: Optional[dict] = None) -> Any:
        """Perform a request with retry/backoff on 429 and 5xx."""
        attempt = 0
        last_err: Optional[Exception] = None
        while attempt <= self.s.max_retries:
            attempt += 1
            try:
                resp = self._client.request(
                    method, url, params=params, json=body, headers=self._headers()
                )
            except httpx.HTTPError as e:  # network/timeout
                last_err = e
                wait = min(2 ** attempt, 30)
                logger.warning("Network error %s (attempt %s/%s); retrying in %ss",
                               e, attempt, self.s.max_retries + 1, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(2 ** attempt, 30)
                logger.warning("HTTP %s on %s (attempt %s); backing off %ss",
                               resp.status_code, url, attempt, wait)
                time.sleep(wait)
                last_err = EToroAPIError(f"HTTP {resp.status_code}", resp.status_code, _safe_json(resp))
                continue

            if resp.status_code >= 400:
                raise EToroAPIError(
                    f"HTTP {resp.status_code} on {method} {url}: {resp.text[:500]}",
                    resp.status_code, _safe_json(resp),
                )
            return _safe_json(resp)

        raise EToroAPIError(f"Exhausted retries on {method} {url}: {last_err}")

    # ----- path helpers -----
    def _info_base(self) -> str:
        seg = self.s.env_segment
        return f"{V1}/trading/info/{seg}".rstrip("/") if seg else f"{V1}/trading/info"

    def _exec_base(self) -> str:
        seg = self.s.env_segment
        return f"{V2}/trading/execution/{seg}".rstrip("/") if seg else f"{V2}/trading/execution"

    # ----- market data -----
    def search_instrument(self, symbol: str) -> Optional[int]:
        """Resolve a ticker symbol -> eToro instrumentId (cached)."""
        symbol = symbol.upper().strip()
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        data = self._request("GET", f"{V1}/market-data/search", params={"search": symbol})
        items = _first(data, "data", "instruments", "results", default=[]) or []
        if isinstance(items, dict):
            items = items.get("items", []) or [items]
        # Prefer an exact symbol/ticker match.
        chosen = None
        for it in items:
            sym = str(_first(it, "symbolFull", "symbol", "ticker", "name", default="")).upper()
            if sym == symbol:
                chosen = it
                break
        if chosen is None and items:
            chosen = items[0]
        if chosen is None:
            logger.warning("No instrument found for %s", symbol)
            return None
        iid = _first(chosen, "instrumentId", "InstrumentID", "instrumentID", "id")
        if iid is None:
            return None
        iid = int(iid)
        self._instrument_cache[symbol] = iid
        self._save_cache()
        return iid

    def resolve_symbols(self, symbols: List[str]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for sym in symbols:
            try:
                iid = self.search_instrument(sym)
                if iid is not None:
                    out[sym.upper()] = iid
            except EToroAPIError as e:
                logger.warning("Instrument lookup failed for %s: %s", sym, e)
        return out

    def get_instrument_rate(self, instrument_id: int) -> Optional[float]:
        data = self._request(
            "GET", f"{V1}/market-data/instruments/rates",
            params={"instrumentIds": instrument_id},
        )
        rows = _first(data, "data", "rates", default=[]) or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            return None
        row = rows[0]
        # try direct price fields, then last candle
        price = _first(row, "lastRate", "last", "ask", "bid", "mid", "close")
        if price is None:
            candles = _first(row, "candles", default=[]) or []
            if candles:
                price = _first(candles[-1], "close", "mid")
        return float(price) if price is not None else None

    # ----- account -----
    def get_portfolio(self) -> Dict[str, Any]:
        """Return the live portfolio. Doubles as the credential/health check.

        Normalized shape:
          {
            "cash": float,                # available buying power
            "equity": float,              # total account value if available
            "currency": str,
            "positions": [
                {"instrument_id": int, "position_id": str, "is_buy": bool,
                 "units": float, "amount": float, "open_rate": float, "pnl": float}
            ],
            "raw": <original payload>
          }
        """
        data = self._request("GET", f"{self._info_base()}/portfolio")
        payload = _first(data, "data", default=data)

        cash = _first(payload, "availableBuyingPower", "buyingPower", "cash",
                      "availableAmount", "credit", default=0.0)
        equity = _first(payload, "equity", "totalValue", "netAssetValue", default=cash)
        currency = _first(payload, "currency", "currencyCode", default=self.s.base_currency.upper())

        raw_positions = _first(payload, "positions", "openPositions", default=[]) or []
        positions: List[Dict[str, Any]] = []
        for p in raw_positions:
            positions.append({
                "instrument_id": _safe_int(_first(p, "instrumentId", "InstrumentID", "instrumentID")),
                "position_id": _first(p, "positionId", "PositionID", "id"),
                "is_buy": bool(_first(p, "isBuy", "IsBuy", default=True)),
                "units": _safe_float(_first(p, "units", "Units", "amountInUnits")),
                "amount": _safe_float(_first(p, "amount", "Amount", "investedAmount")),
                "open_rate": _safe_float(_first(p, "openRate", "OpenRate", "rate")),
                "pnl": _safe_float(_first(p, "netProfit", "profit", "pnl", "unrealizedPnl")),
            })
        return {
            "cash": _safe_float(cash),
            "equity": _safe_float(equity),
            "currency": currency,
            "positions": positions,
            "raw": payload,
        }

    # ----- execution -----
    def open_market_position(self, instrument_id: int, is_buy: bool, amount: float,
                             stop_loss_rate: Optional[float] = None,
                             take_profit_rate: Optional[float] = None,
                             leverage: int = 1) -> Dict[str, Any]:
        """Open a market position by cash AMOUNT (not units)."""
        body: Dict[str, Any] = {
            "action": "open",
            "transaction": "buy" if is_buy else "sell",
            "instrumentId": instrument_id,
            "orderType": "mkt",
            "amount": round(float(amount), 2),
            "orderCurrency": self.s.base_currency,
            "leverage": int(leverage),
        }
        if stop_loss_rate is not None:
            body["stopLossType"] = "fixed"
            body["stopLossRate"] = round(float(stop_loss_rate), 4)
        if take_profit_rate is not None:
            body["takeProfitType"] = "fixed"
            body["takeProfitRate"] = round(float(take_profit_rate), 4)
        if self.s.dry_run:
            logger.info("[DRY-RUN] would POST open order: %s", body)
            return {"dry_run": True, "request": body}
        return self._request("POST", f"{self._exec_base()}/orders", body=body)

    def close_position(self, position_id: Any, instrument_id: Optional[int] = None) -> Dict[str, Any]:
        body = {"action": "close", "positionId": position_id}
        if instrument_id is not None:
            body["instrumentId"] = instrument_id
        if self.s.dry_run:
            logger.info("[DRY-RUN] would POST close order: %s", body)
            return {"dry_run": True, "request": body}
        return self._request("POST", f"{self._exec_base()}/orders", body=body)

    # ----- instrument cache persistence -----
    def _load_cache(self) -> None:
        try:
            if os.path.exists(self._cache_path):
                with open(self._cache_path, "r") as f:
                    self._instrument_cache = {k.upper(): int(v) for k, v in json.load(f).items()}
        except Exception as e:
            logger.debug("Could not load instrument cache: %s", e)

    def _save_cache(self) -> None:
        try:
            os.makedirs(self.s.state_dir, exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._instrument_cache, f)
        except Exception as e:
            logger.debug("Could not save instrument cache: %s", e)


def _safe_json(resp: "httpx.Response") -> Any:
    try:
        return resp.json()
    except Exception:
        return {"_raw_text": resp.text}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
