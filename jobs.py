"""Background operation: the daily price update and portfolio report.

Vercel Cron calls GET /api/cron/daily every weekday at 21:00 UTC, which is
after the US market closes (16:00 New York time) all year round. Nobody
clicks anything; the platform triggers it. The job:
  1. fetches the latest prices of the Mag 7 + SPY from Finnhub -> prices table,
  2. combines them with your holdings to calculate P&L,
  3. writes a CSV report (positions ranked by P&L %) to Vercel Blob,
  4. saves a summary row in the portfolio_reports table.
If Finnhub fails, steps 2-4 still run with the last saved prices.
"""

import csv
import io
from datetime import datetime, timezone

from sqlalchemy import insert, select

from db import get_engine, holdings, reports
from market import refresh_prices, latest_prices
from portfolio import enrich, summarize
from storage import save_file


def run_daily_job(trigger: str = "cron") -> dict:
    result = refresh_prices()
    status = f"{result['updated']}/{result['total']} prices updated"
    if result["errors"]:
        status += " – " + result["errors"][0]

    price_map = latest_prices()
    with get_engine().connect() as conn:
        rows = conn.execute(select(holdings)).mappings().all()
    items = [enrich(dict(r), price_map) for r in rows]
    summary = summarize(items, price_map)

    ranked = sorted(
        [i for i in items if i["is_active"]],
        key=lambda i: i["pnl_pct"] if i["pnl_pct"] is not None else -10**9,
        reverse=True,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "ticker", "company", "shares", "buy_price", "buy_date",
                "current_price", "cost", "market_value", "pnl", "pnl_pct"])
    for n, i in enumerate(ranked, start=1):
        w.writerow([n, i["ticker"], i["company_name"], i["shares"], i["buy_price"], i["buy_date"],
                    i["current_price"], i["cost"], i["market_value"], i["pnl"], i["pnl_pct"]])
    w.writerow([])
    w.writerow(["TOTAL", "", "", "", "", "", "", summary["total_cost"], summary["market_value"],
                summary["pnl"], summary["pnl_pct"]])
    w.writerow(["SPY today %", summary["spy_change_pct"]])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    stored = save_file("reports", f"portfolio-report-{stamp}.csv", "text/csv", buf.getvalue().encode())

    row = {
        "trigger": trigger,
        "prices_status": status[:200],
        "holdings_count": summary["count"],
        "total_cost": summary["total_cost"],
        "market_value": summary["market_value"],
        "pnl": summary["pnl"],
        "pnl_pct": summary["pnl_pct"],
        "spy_change_pct": summary["spy_change_pct"],
        "report_url": stored.url,
    }
    with get_engine().begin() as conn:
        conn.execute(insert(reports).values(**row))

    print(f"[job] daily job done: {row}")  # shows up in Vercel's logs
    return {k: (str(v) if v is not None and not isinstance(v, (int, str)) else v) for k, v in row.items()}
