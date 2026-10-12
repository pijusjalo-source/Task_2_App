"""Market data from Finnhub (an external backing service).

The API key is read from the FINNHUB_API_KEY environment variable, set in the
Vercel dashboard, never written in the code (12-Factor rule III).

We fetch 8 symbols: the Magnificent 7 plus SPY (an ETF that tracks the
S&P 500) as a benchmark, and save them in the `prices` table. Pages read the
saved prices instead of calling Finnhub on every visit: faster pages, far
below the free limit, and the app still works if Finnhub is down.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select, update

from db import get_engine, prices

MAG7 = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet (Google)",
    "AMZN": "Amazon",
    "NVDA": "Nvidia",
    "META": "Meta Platforms",
    "TSLA": "Tesla",
}
BENCHMARK = "SPY"
BENCHMARK_NAME = "SPDR S&P 500 ETF"
ALL_SYMBOLS = list(MAG7) + [BENCHMARK]

FINNHUB_URL = "https://finnhub.io/api/v1/quote"


class MarketDataError(RuntimeError):
    pass


def api_key() -> str | None:
    return os.getenv("FINNHUB_API_KEY")


def fetch_quote(symbol: str) -> dict:
    """Ask Finnhub for one symbol's latest quote."""
    key = api_key()
    if not key:
        raise MarketDataError("FINNHUB_API_KEY is not set")
    url = FINNHUB_URL + "?" + urllib.parse.urlencode({"symbol": symbol, "token": key})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise MarketDataError(f"Finnhub returned HTTP {exc.code} for {symbol}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise MarketDataError(f"Could not reach Finnhub for {symbol}: {exc}") from exc

    # Finnhub fields: c = current, d = change, dp = change %, pc = previous close, t = time
    if not data or not data.get("c"):
        raise MarketDataError(f"No price data for {symbol}")
    return {
        "price": Decimal(str(data["c"])),
        "change": Decimal(str(data["d"])) if data.get("d") is not None else None,
        "change_pct": Decimal(str(data["dp"])) if data.get("dp") is not None else None,
        "prev_close": Decimal(str(data["pc"])) if data.get("pc") else None,
        "quote_time": datetime.fromtimestamp(data["t"], tz=timezone.utc) if data.get("t") else None,
    }


def save_price(symbol: str, quote: dict) -> None:
    values = {**quote, "fetched_at": datetime.now(timezone.utc)}
    with get_engine().begin() as conn:
        res = conn.execute(update(prices).where(prices.c.symbol == symbol).values(**values))
        if res.rowcount == 0:
            conn.execute(insert(prices).values(symbol=symbol, **values))


def refresh_prices() -> dict:
    """Fetch all 8 symbols. Returns how many worked and any errors."""
    if not api_key():
        return {"updated": 0, "total": len(ALL_SYMBOLS), "errors": ["FINNHUB_API_KEY is not set"]}
    updated, errors = 0, []
    for symbol in ALL_SYMBOLS:
        try:
            save_price(symbol, fetch_quote(symbol))
            updated += 1
        except MarketDataError as exc:
            errors.append(str(exc))
    print(f"[market] refreshed {updated}/{len(ALL_SYMBOLS)} prices; errors: {errors}")
    return {"updated": updated, "total": len(ALL_SYMBOLS), "errors": errors}


def latest_prices() -> dict:
    """{symbol: row} of the saved prices."""
    with get_engine().connect() as conn:
        rows = conn.execute(select(prices)).mappings().all()
    return {r["symbol"]: dict(r) for r in rows}
