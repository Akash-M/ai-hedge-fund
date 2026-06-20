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
    # Info endpoints use an explicit env segment (/demo or /real).
    # Execution endpoints: demo has a /demo segment; real omits it.
    def _info_base(self) -> str:
        return f"{V1}/trading/info/{'real' if self.s.is_real else 'demo'}"

    def _exec_base_v2(self) -> str:
        return f"{V2}/trading/execution" + ("" if self.s.is_real else "/demo")

    def _exec_base_v1(self) -> str:
        return f"{V1}/trading/execution" + ("" if self.s.is_real else "/demo")

    # ----- market data -----
    def search_instrument(self, symbol: str) -> Optional[int]:
        """Resolve a ticker symbol -> eToro instrumentId (cached).

        Primary: GET /api/v1/instruments/{symbol}. Fallback: the search endpoint
        filtered by internalSymbolFull (the precise filter per eToro docs).
        """
        symbol = symbol.upper().strip()
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]

        iid = None
        try:
            data = self._request("GET", f"{V1}/instruments/{symbol}",
                                  params={"fields": "instrumentId,symbol"})
            payload = _first(data, "data", "instrument", default=data)
            iid = _first(payload, "instrumentId", "InstrumentID", "instrumentID", "id")
        except EToroAPIError as e:
            logger.warning("instruments/%s lookup failed (%s); trying search", symbol, e)

        if iid is None:
            data = self._request("GET", f"{V1}/market-data/search",
                                  params={"internalSymbolFull": symbol,
                                          "fields": "instrumentId,internalSymbolFull,symbol"})
            items = _first(data, "data", "instruments", "results", default=[]) or []
            if isinstance(items, dict):
                items = items.get("items", []) or [items]
            chosen = None
            for it in items:
                sym = str(_first(it, "internalSymbolFull", "symbolFull", "symbol", "ticker",
                                  default="")).upper()
                if sym == symbol:
                    chosen = it
                    break
            if chosen is None and items:
                chosen = items[0]
            iid = _first(chosen or {}, "instrumentId", "InstrumentID", "instrumentID", "id")

        if iid is None:
            logger.warning("No instrument found for %s", symbol)
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
        """Return the live account via GET /trading/info/{env}/pnl.

        eToro wraps the payload in `clientPortfolio` with `credit` (cash),
        `positions[]`, `orders`, `ordersForOpen`, `mirrors`, and `unrealizedPnL`.
        Doubles as the credential/health check.

        Normalized shape:
          { "cash": float, "equity": float, "credit": float, "unrealized_pnl": float,
            "currency": str,
            "positions": [{instrument_id, position_id, is_buy, units, amount, open_rate, pnl}],
            "raw": <clientPortfolio> | None }
        """
        data = self._request("GET", f"{self._info_base()}/pnl")
        cp = _first(data, "clientPortfolio", "data", default=data) or {}
        if os.getenv("AIHF_DEBUG_API"):
            logger.warning("eToro /pnl clientPortfolio keys: %s",
                           list(cp.keys()) if isinstance(cp, dict) else type(cp).__name__)

        credit = _safe_float(_first(cp, "credit", "availableBuyingPower", "cash", default=0.0))
        unrealized = _safe_float(_first(cp, "unrealizedPnL", "unrealizedPnl", default=0.0))
        currency = _first(cp, "currency", "currencyCode", default=self.s.base_currency.upper())

        positions: List[Dict[str, Any]] = []
        invested = 0.0
        for p in (_first(cp, "positions", "openPositions", default=[]) or []):
            amt = _safe_float(_first(p, "amount", "investedAmount", "Amount"))
            invested += amt
            positions.append({
                "instrument_id": _safe_int(_first(p, "instrumentId", "InstrumentID", "instrumentID")),
                "position_id": _first(p, "positionId", "positionID", "PositionID", "id"),
                "is_buy": bool(_first(p, "isBuy", "IsBuy", default=True)),
                "units": _safe_float(_first(p, "units", "Units")),
                "amount": amt,
                "open_rate": _safe_float(_first(p, "openRate", "OpenRate", "rate")),
                "pnl": _safe_float(_first(p, "pnL", "pnl", "netProfit", "unrealizedPnL")),
            })

        # Available cash = credit minus capital committed to pending open orders.
        pending = 0.0
        for o in (_first(cp, "ordersForOpen", default=[]) or []):
            pending += _safe_float(_first(o, "amount", "Amount"))
        for o in (_first(cp, "orders", default=[]) or []):
            pending += _safe_float(_first(o, "amount", "Amount"))
        cash = max(0.0, credit - pending)
        equity = cash + invested + unrealized

        return {
            "cash": cash,
            "equity": equity,
            "credit": credit,
            "unrealized_pnl": unrealized,
            "currency": currency,
            "positions": positions,
            "raw": cp if os.getenv("AIHF_DEBUG_API") else None,
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
        return self._request("POST", f"{self._exec_base_v2()}/orders", body=body)

    def close_position(self, position_id: Any, instrument_id: Optional[int] = None) -> Dict[str, Any]:
        """Full close of a position. eToro: POST .../market-close-orders/positions/{id}
        with {InstrumentID, UnitsToDeduct: null} (null => close the whole position)."""
        body: Dict[str, Any] = {"InstrumentID": instrument_id, "UnitsToDeduct": None}
        if self.s.dry_run:
            logger.info("[DRY-RUN] would close position %s (instrument %s)", position_id, instrument_id)
            return {"dry_run": True, "request": {"positionId": position_id, **body}}
        return self._request(
            "POST",
            f"{self._exec_base_v1()}/market-close-orders/positions/{position_id}",
            body=body,
        )

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
