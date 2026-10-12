"""Mag7 Portfolio: a CRUD app for a portfolio of Magnificent 7 stocks.

Runs on Vercel (PaaS) as one Python function. This one FastAPI app serves:
  * the web application -> HTML pages                      (requirement: web application)
  * the public API      -> JSON under /api/..., docs at /docs  (requirement: public API)
  * the background job  -> GET /api/cron/daily, called by Vercel Cron

Vercel finds the app because this file is app.py in the repo root and the
FastAPI object is named `app`.
"""

import os
from datetime import timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy import delete, func, insert, select, update

import services
from db import get_engine, holdings, reports
from schemas import HoldingIn, friendly_errors

app = FastAPI(title="Mag7 Portfolio API",
              description="Public API for a portfolio of Magnificent 7 stocks. VU cloud computing assignment.")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ---------------------------------------------------------------- how values are shown in the templates

def money(v):
    return "—" if v is None else f"{v:,.2f}"


def pl(v, suffix=""):
    """A profit/loss number with its sign: green when positive, red when negative."""
    if v is None:
        return "—"
    colour = "up" if v > 0 else "down" if v < 0 else ""
    return Markup(f'<span class="{colour}">{v:+,.2f}{suffix}</span>')


def local_time(dt):
    if dt is None:
        return "—"
    if dt.tzinfo is None:  # SQLite gives times without a zone; they are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Europe/Vilnius")).strftime("%Y-%m-%d %H:%M")


templates.env.filters.update(money=money, local=local_time)
templates.env.globals.update(pl=pl, mag7=services.MAG7, symbols=services.SYMBOLS, benchmark=services.BENCHMARK)


# ---------------------------------------------------------------- holdings in the database (used by pages and API)

def list_holdings() -> list:
    with get_engine().connect() as conn:
        rows = conn.execute(select(holdings).order_by(holdings.c.id.desc())).mappings().all()
    price_map = services.latest_prices()
    return [services.add_pnl(r, price_map) for r in rows]


def get_holding(holding_id: int) -> dict:
    with get_engine().connect() as conn:
        row = conn.execute(select(holdings).where(holdings.c.id == holding_id)).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Holding not found")
    return services.add_pnl(row, services.latest_prices())


def holding_values(data: HoldingIn) -> dict:
    """The validated fields plus the company name, which is filled in from the ticker."""
    return {**data.model_dump(), "company_name": services.MAG7[data.ticker]}


def create_holding(data: HoldingIn) -> int:
    with get_engine().begin() as conn:
        return conn.execute(insert(holdings).values(**holding_values(data)).returning(holdings.c.id)).scalar_one()


def update_holding(holding_id: int, values: dict) -> None:
    with get_engine().begin() as conn:
        conn.execute(update(holdings).where(holdings.c.id == holding_id).values(**values, updated_at=func.now()))


def delete_holding(holding_id: int) -> None:
    h = get_holding(holding_id)
    services.delete_file(h["attachment_url"])  # also remove its file from Blob
    with get_engine().begin() as conn:
        conn.execute(delete(holdings).where(holdings.c.id == holding_id))


async def read_upload(upload):
    """Returns (file, error). file is (name, content type, bytes), or None if no file was chosen."""
    if upload is None or not getattr(upload, "filename", ""):
        return None, None
    data = await upload.read()
    error = services.check_upload(upload.content_type, data)
    if error:
        return None, error
    return (upload.filename, upload.content_type, data), None


def save_attachment(h: dict, file: tuple) -> None:
    """Upload the file to Blob, remember its URL on the holding and delete the file it replaces."""
    name, content_type, data = file
    url = services.upload_file(services.attachment_path(h["id"], name), data, content_type)
    update_holding(h["id"], {"attachment_url": url, "attachment_name": name})
    services.delete_file(h["attachment_url"])


# ---------------------------------------------------------------- web pages
# After a successful POST we redirect (303) to a GET page, so that reloading
# the page in the browser does not send the form a second time.

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_list(request: Request, msg: str = ""):
    items = list_holdings()
    totals = services.portfolio_totals(items, services.latest_prices())
    return templates.TemplateResponse(request, "list.html", {"items": items, "s": totals, "msg": msg})


def show_form(request: Request, values, errors: dict, holding: Optional[dict]):
    """Adding and editing use the same template; `holding` is None when adding."""
    return templates.TemplateResponse(
        request, "form.html",
        {"values": values, "errors": errors, "holding": holding, "prices": services.latest_prices()},
        status_code=422 if errors else 200)


async def validate_form(form):
    """Check the submitted form with the same HoldingIn model that the API uses.
    Returns (values, data, file, errors); errors is {field: message}, empty when all is fine."""
    values = {name: form.get(name, "") for name in ("ticker", "shares", "buy_price", "buy_date", "notes")}
    values["is_active"] = "is_active" in form  # an unticked checkbox sends nothing
    data, errors = None, {}
    try:
        data = HoldingIn.model_validate(values)
    except ValidationError as exc:
        errors = friendly_errors(exc)
    file, file_error = await read_upload(form.get("attachment"))
    if file_error:
        errors["attachment"] = file_error
    return values, data, file, errors


@app.get("/holdings/new", response_class=HTMLResponse, include_in_schema=False)
def page_new(request: Request):
    return show_form(request, {"is_active": True}, {}, None)


@app.post("/holdings/new", response_class=HTMLResponse, include_in_schema=False)
async def page_create(request: Request):
    values, data, file, errors = await validate_form(await request.form())
    if errors:
        return show_form(request, values, errors, None)
    h = get_holding(create_holding(data))
    if file:
        save_attachment(h, file)
    return RedirectResponse(f"/holdings/{h['id']}?msg=created", status_code=303)


@app.get("/holdings/{holding_id}", response_class=HTMLResponse, include_in_schema=False)
def page_detail(request: Request, holding_id: int, msg: str = ""):
    return templates.TemplateResponse(request, "detail.html", {"h": get_holding(holding_id), "msg": msg})


@app.get("/holdings/{holding_id}/edit", response_class=HTMLResponse, include_in_schema=False)
def page_edit(request: Request, holding_id: int):
    h = get_holding(holding_id)
    return show_form(request, h, {}, h)


@app.post("/holdings/{holding_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def page_update(request: Request, holding_id: int):
    h = get_holding(holding_id)
    form = await request.form()
    values, data, file, errors = await validate_form(form)
    if errors:
        return show_form(request, values, errors, h)
    update_holding(holding_id, holding_values(data))
    if file:
        save_attachment(h, file)
    elif "remove_attachment" in form:
        services.delete_file(h["attachment_url"])
        update_holding(holding_id, {"attachment_url": None, "attachment_name": None})
    return RedirectResponse(f"/holdings/{holding_id}?msg=updated", status_code=303)


@app.post("/holdings/{holding_id}/delete", include_in_schema=False)
def page_delete(holding_id: int):
    delete_holding(holding_id)
    return RedirectResponse("/?msg=deleted", status_code=303)


@app.get("/market", response_class=HTMLResponse, include_in_schema=False)
def page_market(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "market.html", {
        "prices": services.latest_prices(), "msg": msg, "has_key": bool(os.getenv("FINNHUB_API_KEY"))})


@app.post("/market/refresh", include_in_schema=False)
def page_market_refresh():
    result = services.refresh_prices()
    return RedirectResponse("/market?msg=" + ("failed" if result["errors"] else "ok"), status_code=303)


@app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
def page_reports(request: Request, msg: str = ""):
    with get_engine().connect() as conn:
        rows = conn.execute(select(reports).order_by(reports.c.id.desc()).limit(30)).mappings().all()
    return templates.TemplateResponse(request, "reports.html", {"reports": rows, "msg": msg})


@app.post("/reports/run", include_in_schema=False)
def page_run_report():
    services.run_daily_job(trigger="manual")
    return RedirectResponse("/reports?msg=ran", status_code=303)


# ---------------------------------------------------------------- public API (JSON)
# FastAPI validates the JSON body against HoldingIn by itself and answers
# 422 with the reason per field when something is wrong.

@app.get("/api/holdings", tags=["holdings"])
def api_list():
    """List all holdings with their current price and P&L."""
    return list_holdings()


@app.get("/api/holdings/{holding_id}", tags=["holdings"])
def api_get(holding_id: int):
    """Get one holding by id."""
    return get_holding(holding_id)


@app.post("/api/holdings", status_code=201, tags=["holdings"])
def api_create(data: HoldingIn):
    """Add a holding."""
    return get_holding(create_holding(data))


@app.put("/api/holdings/{holding_id}", tags=["holdings"])
def api_update(holding_id: int, data: HoldingIn):
    """Replace all fields of a holding (same validation as create)."""
    get_holding(holding_id)  # answers 404 if it does not exist
    update_holding(holding_id, holding_values(data))
    return get_holding(holding_id)


@app.delete("/api/holdings/{holding_id}", status_code=204, tags=["holdings"])
def api_delete(holding_id: int):
    """Delete a holding and its attached file."""
    delete_holding(holding_id)
    return Response(status_code=204)


@app.post("/api/holdings/{holding_id}/attachment", tags=["holdings"])
async def api_upload(holding_id: int, file: UploadFile = File(...)):
    """Upload a file (PNG, JPG, WEBP, PDF or CSV, max 4 MB) for a holding. It is stored in Vercel Blob."""
    h = get_holding(holding_id)
    upload, error = await read_upload(file)
    if upload is None:
        raise HTTPException(status_code=422, detail=error or "No file sent")
    save_attachment(h, upload)
    return get_holding(holding_id)


@app.get("/api/portfolio", tags=["portfolio"])
def api_portfolio():
    """Portfolio totals (active holdings only) and today's change of SPY as the benchmark."""
    return services.portfolio_totals(list_holdings(), services.latest_prices())


@app.get("/api/prices", tags=["portfolio"])
def api_prices():
    """Latest saved prices of the Magnificent 7 and SPY."""
    return list(services.latest_prices().values())


@app.get("/api/cron/daily", tags=["background job"])
def api_cron(authorization: Optional[str] = Header(None)):
    """The background job. Vercel Cron calls this URL every weekday at 21:00 UTC
    (see vercel.json). If the CRON_SECRET environment variable is set, Vercel
    sends it as a Bearer token and every other caller is refused."""
    secret = os.getenv("CRON_SECRET")
    if secret and authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return services.run_daily_job(trigger="cron")
