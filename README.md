# Mag7 Portfolio

A CRUD (Create, Read, Update, Delete) web app for a portfolio of **Magnificent 7** stocks
(AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA), with daily prices from **Finnhub**, P&L
(profit and loss) per position, and the **S&P 500 (SPY)** as a benchmark.
Deployed on **Vercel** (PaaS). VU course "Programavimas debesų kompiuterijoje", practice task 3.

| Requirement | How it is met |
|---|---|
| CRUD + list | Pages `/` (portfolio), `/holdings/new`, `/holdings/{id}`, `/holdings/{id}/edit`, delete button |
| Validation of ≥4 data types | `schemas.py`: text (ticker must be one of the Mag 7), integer (shares), decimal (buy price), date (buy date), boolean (still holding) |
| Web application | FastAPI + Jinja2 HTML templates |
| Public API | JSON under `/api/...` (holdings, portfolio, prices, reports); interactive docs at `/docs` |
| Database | Neon Postgres via Vercel Marketplace (`DATABASE_URL`) |
| File storage | Vercel Blob (`BLOB_READ_WRITE_TOKEN`): attachments per holding and the daily CSV reports |
| Background operation | Vercel Cron, weekdays 21:00 UTC (after US close) → `/api/cron/daily`: fetch prices, calculate P&L, save CSV to Blob, save summary |

## Files

- `app.py` – web pages and public API
- `schemas.py` – validation rules
- `db.py` – tables (`holdings` = your portfolio, `prices` = market data, `portfolio_reports`) and connection
- `market.py` – Finnhub price fetching
- `portfolio.py` – cost, market value, P&L calculations
- `storage.py` – file uploads (Vercel Blob, local folder in development)
- `jobs.py` – the daily background job
- `vercel.json` – cron schedule
- `templates/` – HTML pages

## Environment variables

| Name | Set by |
|---|---|
| `DATABASE_URL` | automatically, when Neon is connected in Vercel → Storage |
| `BLOB_READ_WRITE_TOKEN` | automatically, when a Blob store is connected |
| `FINNHUB_API_KEY` | you, in Vercel → Settings → Environment Variables |
| `CRON_SECRET` | optional; protects the cron endpoint |

## Run locally

```bash
pip install -r requirements.txt uvicorn
uvicorn app:app --reload
```

Without `DATABASE_URL` it uses a local SQLite file; without `BLOB_READ_WRITE_TOKEN` files go to `local_uploads/`.
