"""Mag7 Portfolio Tracker: a CRUD app for a portfolio of Magnificent 7 stocks.

Runs on Vercel (PaaS) as a Python function. One FastAPI app serves:
  * the web application   -> HTML pages under /
  * the public API        -> JSON under /api/... (interactive docs at /docs)
  * the background job    -> /api/cron/daily, called by Vercel Cron after US market close
"""

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import delete, func, insert, select, update

import market
import storage
from db import database_kind, get_engine, holdings, reports
from jobs import run_daily_job
from portfolio import enrich, summarize
from schemas import HoldingIn, HoldingOut, friendly_errors

APP_NAME = "Mag7 Portfolio"
BASE_DIR = Path(__file__).resolve().parent
LOCAL_TZ = ZoneInfo("Europe/Vilnius")

app = FastAPI(
    title=f"{APP_NAME} API",
    description="Public API for a portfolio of Magnificent 7 stocks with daily prices from Finnhub. "
                "VU cloud computing assignment.",
    version="1.0.0",
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _money(v):
    return "—" if v is None else f"{Decimal(v):,.2f}"


def _signed(v, suffix=""):
    if v is None:
        return "—"
    v = Decimal(v)
    return f"{'+' if v > 0 else ''}{v:,.2f}{suffix}"


def _local(dt):
    if dt is None:
        return "—"
    if dt.tzinfo is None:          # SQLite returns naive UTC times
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


templates.env.filters.update(money=_money, signed=_signed, local=_local)
templates.env.globals.update(app_name=APP_NAME, mag7=market.MAG7, benchmark=market.BENCHMARK)


# ---------------------------------------------------------------- helpers

SORTS = {
    "newest": lambda i: -i["id"],
    "ticker": lambda i: i["ticker"],
    "value": lambda i: -(i["market_value"] if i["market_value"] is not None else i["cost"]),
    "pnl": lambda i: -(i["pnl_pct"] if i["pnl_pct"] is not None else Decimal("-1e9")),
    "buy_date": lambda i: -i["buy_date"].toordinal(),
}


def query_holdings(q: Optional[str] = None, sort: str = "newest", active: Optional[bool] = None) -> list:
    stmt = select(holdings)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(func.lower(holdings.c.ticker).like(like) | func.lower(holdings.c.company_name).like(like))
    if active is not None:
        stmt = stmt.where(holdings.c.is_active == active)
    with get_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    price_map = market.latest_prices()
    items = [enrich(dict(r), price_map) for r in rows]
    items.sort(key=SORTS.get(sort, SORTS["newest"]))
    return items


def get_holding(holding_id: int) -> dict:
    with get_engine().connect() as conn:
        row = conn.execute(select(holdings).where(holdings.c.id == holding_id)).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Holding not found")
    return enrich(dict(row), market.latest_prices())


def _values(data: HoldingIn) -> dict:
    return {**data.model_dump(), "company_name": market.MAG7[data.ticker]}


def create_holding(data: HoldingIn) -> int:
    with get_engine().begin() as conn:
        result = conn.execute(insert(holdings).values(**_values(data)).returning(holdings.c.id))
        return result.scalar_one()


def update_holding(holding_id: int, values: dict) -> None:
    with get_engine().begin() as conn:
        conn.execute(update(holdings).where(holdings.c.id == holding_id)
                     .values(**values, updated_at=func.now()))


def delete_holding(holding_id: int) -> None:
    h = get_holding(holding_id)
    storage.delete_file(h["attachment_url"])
    with get_engine().begin() as conn:
        conn.execute(delete(holdings).where(holdings.c.id == holding_id))


async def read_upload(upload) -> Optional[tuple]:
    """Return (filename, content_type, bytes) if a file was chosen, else None."""
    if upload is None or not getattr(upload, "filename", ""):
        return None
    data = await upload.read()
    storage.validate_upload(upload.filename, upload.content_type, data)
    return upload.filename, upload.content_type, data


def attach_file(holding_id: int, old_url: Optional[str], file: tuple) -> None:
    stored = storage.save_file(f"holdings/{holding_id}", *file)
    update_holding(holding_id, {"attachment_url": stored.url, "attachment_name": stored.name})
    storage.delete_file(old_url)


def form_values(form) -> dict:
    return {
        "ticker": form.get("ticker", ""),
        "shares": form.get("shares", ""),
        "buy_price": form.get("buy_price", ""),
        "buy_date": form.get("buy_date", ""),
        "is_active": form.get("is_active") == "on",   # unticked checkbox sends nothing
        "notes": form.get("notes", ""),
    }


def form_context(mode, values, errors, holding):
    return {"mode": mode, "values": values, "errors": errors, "holding": holding,
            "prices": market.latest_prices()}


# ---------------------------------------------------------------- web pages

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_list(request: Request, q: str = "", sort: str = "newest", status: str = "all", msg: str = ""):
    active = {"active": True, "closed": False}.get(status)
    items = query_holdings(q or None, sort, active)
    all_items = items if (not q and active is None) else query_holdings()
    summary = summarize(all_items, market.latest_prices())
    return templates.TemplateResponse(request, "list.html", {
        "items": items, "q": q, "sort": sort, "status": status, "msg": msg, "s": summary,
    })


@app.get("/holdings/new", response_class=HTMLResponse, include_in_schema=False)
def page_new(request: Request, ticker: str = ""):
    return templates.TemplateResponse(request, "form.html", form_context(
        "new", {"is_active": True, "ticker": ticker.upper()}, {}, None))


@app.post("/holdings/new", response_class=HTMLResponse, include_in_schema=False)
async def page_create(request: Request):
    form = await request.form()
    values = form_values(form)
    errors, data = {}, None
    try:
        data = HoldingIn.model_validate(values)
    except ValidationError as exc:
        errors = friendly_errors(exc)
    try:
        file = await read_upload(form.get("attachment"))
    except storage.StorageError as exc:
        errors["attachment"], file = str(exc), None
    if errors:
        return templates.TemplateResponse(request, "form.html",
                                          form_context("new", values, errors, None), status_code=422)
    new_id = create_holding(data)
    if file:
        attach_file(new_id, None, file)
    return RedirectResponse(f"/holdings/{new_id}?msg=created", status_code=303)


@app.get("/holdings/{holding_id}", response_class=HTMLResponse, include_in_schema=False)
def page_detail(request: Request, holding_id: int, msg: str = ""):
    return templates.TemplateResponse(request, "detail.html", {"h": get_holding(holding_id), "msg": msg})


@app.get("/holdings/{holding_id}/edit", response_class=HTMLResponse, include_in_schema=False)
def page_edit(request: Request, holding_id: int):
    h = get_holding(holding_id)
    return templates.TemplateResponse(request, "form.html", form_context("edit", h, {}, h))


@app.post("/holdings/{holding_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def page_update(request: Request, holding_id: int):
    h = get_holding(holding_id)
    form = await request.form()
    values = form_values(form)
    errors, data = {}, None
    try:
        data = HoldingIn.model_validate(values)
    except ValidationError as exc:
        errors = friendly_errors(exc)
    try:
        file = await read_upload(form.get("attachment"))
    except storage.StorageError as exc:
        errors["attachment"], file = str(exc), None
    if errors:
        return templates.TemplateResponse(request, "form.html",
                                          form_context("edit", values, errors, h), status_code=422)
    update_holding(holding_id, _values(data))
    if file:
        attach_file(holding_id, h["attachment_url"], file)
    elif form.get("remove_attachment") == "on" and h["attachment_url"]:
        storage.delete_file(h["attachment_url"])
        update_holding(holding_id, {"attachment_url": None, "attachment_name": None})
    return RedirectResponse(f"/holdings/{holding_id}?msg=updated", status_code=303)


@app.post("/holdings/{holding_id}/delete", include_in_schema=False)
def page_delete(holding_id: int):
    delete_holding(holding_id)
    return RedirectResponse("/?msg=deleted", status_code=303)


@app.get("/market", response_class=HTMLResponse, include_in_schema=False)
def page_market(request: Request, msg: str = ""):
    price_map = market.latest_prices()
    owned = {i["ticker"] for i in query_holdings(active=True)}
    rows = [{"symbol": s, "name": market.MAG7.get(s, market.BENCHMARK_NAME), "owned": s in owned,
             "p": price_map.get(s)} for s in market.ALL_SYMBOLS]
    return templates.TemplateResponse(request, "market.html", {
        "rows": rows, "msg": msg, "has_key": bool(market.api_key()),
    })


@app.post("/market/refresh", include_in_schema=False)
def page_market_refresh():
    r = market.refresh_prices()
    msg = "ok" if not r["errors"] else ("nokey" if not market.api_key() else "partial")
    return RedirectResponse(f"/market?msg={msg}", status_code=303)


@app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
def page_reports(request: Request, msg: str = ""):
    with get_engine().connect() as conn:
        rows = conn.execute(select(reports).order_by(reports.c.id.desc()).limit(30)).mappings().all()
    return templates.TemplateResponse(request, "reports.html", {"reports": rows, "msg": msg})


@app.post("/reports/run", include_in_schema=False)
def page_run_report():
    run_daily_job(trigger="manual")
    return RedirectResponse("/reports?msg=ran", status_code=303)


@app.get("/about", response_class=HTMLResponse, include_in_schema=False)
def page_about(request: Request):
    return templates.TemplateResponse(request, "about.html", {
        "db": database_kind(), "storage": storage.storage_kind(),
        "market": "Finnhub (API key set)" if market.api_key() else "Finnhub (no API key set)",
        "region": os.getenv("VERCEL_REGION", "local"), "env": os.getenv("VERCEL_ENV", "development"),
    })


@app.get("/local-files/{path:path}", include_in_schema=False)
def local_file(path: str):
    """Serves uploads when running locally without Blob storage."""
    target = (storage.LOCAL_DIR / path).resolve()
    if storage.using_blob() or not target.is_file() or storage.LOCAL_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=404)
    return FileResponse(target)


# ---------------------------------------------------------------- public API

@app.get("/api/holdings", response_model=List[HoldingOut], tags=["portfolio"])
def api_list(
    q: Optional[str] = Query(None, description="Search in ticker or company name"),
    sort: str = Query("newest", enum=list(SORTS)),
    active: Optional[bool] = Query(None, description="Filter by active/closed"),
):
    """List all holdings with current price and P&L, with optional search, filter and sort."""
    return query_holdings(q, sort, active)


@app.get("/api/holdings/{holding_id}", response_model=HoldingOut, tags=["portfolio"])
def api_get(holding_id: int):
    """Get one holding by id."""
    return get_holding(holding_id)


@app.post("/api/holdings", response_model=HoldingOut, status_code=201, tags=["portfolio"])
def api_create(data: HoldingIn):
    """Add a holding. Invalid input returns 422 with the reason per field."""
    return get_holding(create_holding(data))


@app.put("/api/holdings/{holding_id}", response_model=HoldingOut, tags=["portfolio"])
def api_update(holding_id: int, data: HoldingIn):
    """Replace all fields of a holding (same validation as create)."""
    get_holding(holding_id)
    update_holding(holding_id, _values(data))
    return get_holding(holding_id)


@app.delete("/api/holdings/{holding_id}", status_code=204, tags=["portfolio"])
def api_delete(holding_id: int):
    """Delete a holding and its attached file."""
    delete_holding(holding_id)
    return Response(status_code=204)


@app.post("/api/holdings/{holding_id}/attachment", response_model=HoldingOut, tags=["files"])
async def api_upload(holding_id: int, file: UploadFile = File(...)):
    """Upload a file (PNG, JPG, WEBP, PDF, CSV, max 4 MB) for a holding. Stored in Vercel Blob."""
    h = get_holding(holding_id)
    try:
        f = await read_upload(file)
    except storage.StorageError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if f is None:
        raise HTTPException(status_code=422, detail="No file sent")
    attach_file(holding_id, h["attachment_url"], f)
    return get_holding(holding_id)


@app.get("/api/portfolio", tags=["portfolio"])
def api_portfolio():
    """Portfolio totals: cost, market value, P&L, and SPY's daily change as benchmark."""
    s = summarize(query_holdings(), market.latest_prices())
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in s.items()}


@app.get("/api/prices", tags=["market data"])
def api_prices():
    """Latest saved prices for the Magnificent 7 and SPY."""
    price_map = market.latest_prices()
    return [{"symbol": s, "name": market.MAG7.get(s, market.BENCHMARK_NAME),
             **{k: (str(v) if isinstance(v, Decimal) else v)
                for k, v in (price_map.get(s) or {}).items() if k != "symbol"}}
            for s in market.ALL_SYMBOLS]


@app.get("/api/reports", tags=["background job"])
def api_reports():
    """Results of the daily background job, newest first."""
    with get_engine().connect() as conn:
        rows = conn.execute(select(reports).order_by(reports.c.id.desc()).limit(30)).mappings().all()
    return [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in rows]


@app.get("/api/cron/daily", tags=["background job"])
def api_cron(authorization: Optional[str] = Header(None)):
    """Called by Vercel Cron every weekday after US market close. If CRON_SECRET is
    set, Vercel sends it as a Bearer token and only those calls are accepted."""
    secret = os.getenv("CRON_SECRET")
    if secret and authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return run_daily_job(trigger="cron")


@app.get("/api/health", tags=["system"])
def api_health():
    """Shows which backing services the app is connected to."""
    with get_engine().connect() as conn:
        count = conn.execute(select(func.count()).select_from(holdings)).scalar_one()
    return {"status": "ok", "database": database_kind(), "file_storage": storage.storage_kind(),
            "market_data": "Finnhub" if market.api_key() else "Finnhub (no key)", "holdings": count,
            "time": datetime.now(timezone.utc).isoformat()}
