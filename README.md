# Mag7 Portfolio

A CRUD (Create, Read, Update, Delete) web app for a portfolio of **Magnificent 7** stocks
(AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA), with daily prices from **Finnhub**, P&L
(profit and loss) per position, and the **S&P 500 (SPY)** as a benchmark.
Deployed on **Render** (PaaS). VU course "Programavimas debesų kompiuterijoje", practice task 3,
migrated from Vercel to Render in practice task 4.

| Requirement | How it is met |
|---|---|
| CRUD + list | Pages `/` (portfolio), `/holdings/new`, `/holdings/{id}`, `/holdings/{id}/edit`, delete button |
| Validation of ≥4 data types | `schemas.py`, on create and update: text (ticker must be one of the Mag 7), integer (shares), decimal (buy price), date (buy date), boolean (still holding) |
| Web application | FastAPI + Jinja2 HTML templates |
| Public API | JSON under `/api/...` (holdings, portfolio, prices); interactive docs at `/docs` |
| Database | Render Postgres (`DATABASE_URL`) |
| File storage | Supabase Storage through the S3 API (`S3_*` variables): attachments per holding and the daily CSV reports; private bucket, files served via `/files/...` with temporary signed links |
| Background operation | GitHub Actions schedule, weekdays 21:00 UTC (after US close) → `/api/cron/daily`: fetch prices, calculate P&L, save CSV to storage, save summary |

## Files

- `app.py` – web pages, public API and the cron endpoint
- `schemas.py` – validation rules (one model for the forms and the API)
- `db.py` – database connection and the 3 tables (`holdings`, `prices`, `portfolio_reports`)
- `services.py` – Finnhub prices, P&L calculations, S3 file storage, the daily job
- `.github/workflows/` – the daily schedule (GitHub Actions)
- `templates/` – HTML pages

## Environment variables

| Name | Set by |
|---|---|
| `DATABASE_URL` | Render → web service → Environment (Internal Database URL of Render Postgres) |
| `S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET` | Render → Environment (values from Supabase → Storage → S3 Connection) |
| `FINNHUB_API_KEY` | Render → Environment |
| `CRON_SECRET` | optional; protects the cron endpoint |

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Without `DATABASE_URL` it uses a local SQLite file (`local.db`). File uploads need the `S3_*`
variables, so locally they show "File storage is not connected" unless they are set.

## Deploy on Render

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
