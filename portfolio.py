"""Portfolio maths: combine YOUR holdings with the latest MARKET prices.

P&L (profit and loss) is never typed in; it is calculated:
  cost          = shares × buy_price
  market_value  = shares × current_price
  pnl           = market_value − cost
  pnl_pct       = pnl ÷ cost × 100
"""

from decimal import Decimal

from market import BENCHMARK

CENT = Decimal("0.01")


def enrich(holding: dict, price_map: dict) -> dict:
    h = dict(holding)
    cost = Decimal(h["shares"]) * Decimal(h["buy_price"])
    h["cost"] = cost.quantize(CENT)
    p = price_map.get(h["ticker"])
    if p:
        current = Decimal(p["price"])
        value = Decimal(h["shares"]) * current
        h["current_price"] = current.quantize(CENT)
        h["price_updated_at"] = p["fetched_at"]
        h["market_value"] = value.quantize(CENT)
        h["pnl"] = (value - cost).quantize(CENT)
        h["pnl_pct"] = ((value - cost) / cost * 100).quantize(CENT) if cost else None
    else:
        h.update(current_price=None, price_updated_at=None, market_value=None, pnl=None, pnl_pct=None)
    return h


def summarize(items: list, price_map: dict) -> dict:
    """Totals over ACTIVE positions only (closed ones are history)."""
    active = [i for i in items if i["is_active"]]
    cost = sum((i["cost"] for i in active), Decimal("0"))
    priced = [i for i in active if i["market_value"] is not None]
    value = sum((i["market_value"] for i in priced), Decimal("0"))
    priced_cost = sum((i["cost"] for i in priced), Decimal("0"))
    pnl = value - priced_cost if priced else None
    spy = price_map.get(BENCHMARK)
    return {
        "count": len(active),
        "total_cost": cost.quantize(CENT),
        "market_value": value.quantize(CENT) if priced else None,
        "pnl": pnl.quantize(CENT) if pnl is not None else None,
        "pnl_pct": (pnl / priced_cost * 100).quantize(CENT) if pnl is not None and priced_cost else None,
        "spy_change_pct": Decimal(spy["change_pct"]).quantize(CENT) if spy and spy.get("change_pct") is not None else None,
        "spy_price": Decimal(spy["price"]).quantize(CENT) if spy else None,
        "prices_at": max((p["fetched_at"] for p in price_map.values()), default=None),
    }
