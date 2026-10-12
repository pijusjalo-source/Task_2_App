"""Services: everything the app does besides showing pages.

  1. Market data  -> fetch prices from Finnhub (an external API) into the `prices` table
  2. P&L maths    -> combine the holdings with the latest saved prices
  3. File storage -> upload and delete files in Vercel Blob   (requirement: file storage)
  4. Daily job    -> prices + P&L + CSV report                (requirement: background operation)

The secrets (FINNHUB_API_KEY, BLOB_READ_WRITE_TOKEN) are read from environment
variables and never written in the code (12-Factor rule III: config in the environment).
"""

import csv
import io
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select, update
from vercel import blob  # official Vercel SDK; it reads BLOB_READ_WRITE_TOKEN by itself

from db import get_engine, holdings, prices, reports

MAG7 = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet (Google)",
    "AMZN": "Amazon",
    "NVDA": "Nvidia",
    "META": "Meta Platforms",
    "TSLA": "Tesla",
}
BENCHMARK = "SPY"  # an ETF (fund) that follows the S&P 500 index
SYMBOLS = {**MAG7, BENCHMARK: "SPDR S&P 500 ETF"}  # the 8 symbols we fetch prices for

CENT = Decimal("0.01")


# ---------------------------------------------------------------- 1. market data

def fetch_quote(symbol: str) -> dict:
    """Ask Finnhub for the latest quote of one symbol. Raises an error if that fails."""
    query = urllib.parse.urlencode({"symbol": symbol, "token": os.getenv("FINNHUB_API_KEY")})
    with urllib.request.urlopen(f"https://finnhub.io/api/v1/quote?{query}", timeout=10) as resp:
        data = json.load(resp)
    # Finnhub fields: c = current price, d = change, dp = change %, pc = previous close
    if not data.get("c"):
        raise ValueError("no price data")

    def number(key):
        return None if data.get(key) is None else Decimal(str(data[key]))

    return {"price": number("c"), "change": number("d"), "change_pct": number("dp"), "prev_close": number("pc")}


def refresh_prices() -> dict:
    """Fetch all 8 symbols and save them. One failure does not stop the others,
    and a symbol that fails simply keeps its old saved price."""
    if not os.getenv("FINNHUB_API_KEY"):
        return {"updated": 0, "errors": ["FINNHUB_API_KEY is not set"]}
    updated, errors = 0, []
    for symbol in SYMBOLS:
        try:
            values = {**fetch_quote(symbol), "fetched_at": datetime.now(timezone.utc)}
            # "Upsert": update the symbol's row, or insert it the first time.
            with get_engine().begin() as conn:
                result = conn.execute(update(prices).where(prices.c.symbol == symbol).values(**values))
                if result.rowcount == 0:
                    conn.execute(insert(prices).values(symbol=symbol, **values))
            updated += 1
        except Exception as exc:  # network error, wrong key, Finnhub down...
            errors.append(f"{symbol}: {exc}")
    return {"updated": updated, "errors": errors}


def latest_prices() -> dict:
    """The saved prices as {symbol: row}. Pages read these instead of calling
    Finnhub on every visit, so the app still works when Finnhub is down."""
    with get_engine().connect() as conn:
        rows = conn.execute(select(prices)).mappings().all()
    return {r["symbol"]: dict(r) for r in rows}


# ---------------------------------------------------------------- 2. P&L maths

def add_pnl(holding, price_map: dict) -> dict:
    """Add cost, current price, market value and P&L (profit and loss) to one holding.
    These are calculated every time we read, never stored."""
    h = dict(holding)
    h["cost"] = h["shares"] * h["buy_price"]
    h.update(current_price=None, price_updated_at=None, market_value=None, pnl=None, pnl_pct=None)
    p = price_map.get(h["ticker"])
    if p:  # stays None ("—" on the pages) until the first price is saved
        h["current_price"] = p["price"].quantize(CENT)
        h["price_updated_at"] = p["fetched_at"]
        h["market_value"] = (h["shares"] * p["price"]).quantize(CENT)
        h["pnl"] = h["market_value"] - h["cost"]
        h["pnl_pct"] = (h["pnl"] / h["cost"] * 100).quantize(CENT)
    return h


def portfolio_totals(items: list, price_map: dict) -> dict:
    """Totals over the ACTIVE holdings only, plus today's change of SPY as the benchmark."""
    active = [i for i in items if i["is_active"]]
    cost = sum((i["cost"] for i in active), Decimal("0.00"))
    value = pnl = pnl_pct = None
    if active and all(i["market_value"] is not None for i in active):
        value = sum(i["market_value"] for i in active)
        pnl = value - cost
        pnl_pct = (pnl / cost * 100).quantize(CENT)
    spy = price_map.get(BENCHMARK)
    return {
        "count": len(active),
        "total_cost": cost,
        "market_value": value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "spy_change_pct": spy["change_pct"].quantize(CENT) if spy and spy["change_pct"] is not None else None,
        "prices_at": max((p["fetched_at"] for p in price_map.values()), default=None),
    }


# ---------------------------------------------------------------- 3. file storage (Vercel Blob)
# On Vercel the app's own disk is read-only and temporary (ephemeral filesystem),
# so files must live in a separate storage service. Connecting a Blob store to the
# project makes Vercel set BLOB_READ_WRITE_TOKEN for us.

ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp", "application/pdf", "text/csv"}
MAX_BYTES = 4 * 1024 * 1024  # Vercel accepts request bodies up to about 4.5 MB


def blob_connected() -> bool:
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN"))


def check_upload(content_type: str, data: bytes) -> str | None:
    """Return a message if the file is not allowed, or None if it is fine."""
    if not blob_connected():
        return "File storage is not connected (BLOB_READ_WRITE_TOKEN is not set)"
    if not data:
        return "The file is empty"
    if len(data) > MAX_BYTES:
        return "The file is too large (max 4 MB)"
    if content_type not in ALLOWED_TYPES:
        return "Only PNG, JPG, WEBP, PDF or CSV files are allowed"
    return None


def attachment_path(holding_id: int, filename: str) -> str:
    """holdings/{id}/{random8}-{safe-filename}; the random part keeps names unique."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename)[:80]
    return f"holdings/{holding_id}/{uuid.uuid4().hex[:8]}-{safe}"


def upload_file(path: str, data: bytes, content_type: str) -> str:
    """Save the bytes in Vercel Blob and return the file's public URL."""
    return blob.put(path, data, access="public", content_type=content_type).url


def delete_file(url: str | None) -> None:
    """Delete a file from Vercel Blob. A failure is only logged, so that a
    holding can still be deleted when its file is already gone."""
    if url:
        try:
            blob.delete(url)
        except Exception as exc:
            print(f"[storage] could not delete {url}: {exc}")


# ---------------------------------------------------------------- 4. daily job (background operation)

def run_daily_job(trigger: str) -> dict:
    """Vercel Cron calls this (through GET /api/cron/daily) every weekday after the
    US market closes. The "Run daily job now" button calls the same function."""
    # 1. Fetch the prices. If Finnhub fails, the old saved prices are used below.
    result = refresh_prices()
    status = f"{result['updated']}/{len(SYMBOLS)} updated"
    if result["errors"]:
        status += " – " + result["errors"][0]

    # 2. Calculate P&L for the active holdings, best P&L % first.
    price_map = latest_prices()
    with get_engine().connect() as conn:
        rows = conn.execute(select(holdings).where(holdings.c.is_active)).mappings().all()
    items = [add_pnl(r, price_map) for r in rows]
    items.sort(key=lambda i: i["pnl_pct"] if i["pnl_pct"] is not None else 0, reverse=True)
    totals = portfolio_totals(items, price_map)

    # 3. Build the CSV report in memory (no local disk on Vercel) and upload it to Blob.
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["rank", "ticker", "company", "shares", "buy_price", "buy_date",
                     "current_price", "cost", "market_value", "pnl", "pnl_pct"])
    for rank, i in enumerate(items, start=1):
        writer.writerow([rank, i["ticker"], i["company_name"], i["shares"], i["buy_price"], i["buy_date"],
                         i["current_price"], i["cost"], i["market_value"], i["pnl"], i["pnl_pct"]])
    writer.writerow(["TOTAL", "", "", "", "", "", "", totals["total_cost"], totals["market_value"],
                     totals["pnl"], totals["pnl_pct"]])
    writer.writerow(["SPY change %", totals["spy_change_pct"]])
    report_url = None
    if blob_connected():
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        report_url = upload_file(f"reports/portfolio-report-{stamp}.csv", buffer.getvalue().encode(), "text/csv")

    # 4. Save one summary row per run.
    row = {
        "trigger": trigger,
        "prices_status": status[:200],
        "holdings_count": totals["count"],
        "total_cost": totals["total_cost"],
        "market_value": totals["market_value"],
        "pnl": totals["pnl"],
        "pnl_pct": totals["pnl_pct"],
        "spy_change_pct": totals["spy_change_pct"],
        "report_url": report_url,
    }
    with get_engine().begin() as conn:
        conn.execute(insert(reports).values(**row))

    # 5. print() output shows up in Vercel's logs.
    print(f"[job] {trigger}: {status}, {totals['count']} holdings, P&L {totals['pnl']}, report {report_url}")
    return row
