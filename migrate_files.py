"""One-off migration (task 4): copy files from Vercel Blob to the new S3 bucket
and rewrite their links in the database from the Blob URL to /files/<key>."""
import urllib.request
from urllib.parse import unquote, urlsplit

from sqlalchemy import text

import services
from db import get_engine

COLUMNS = [("holdings", "attachment_url"), ("portfolio_reports", "report_url")]

with get_engine().begin() as conn:
    for table, column in COLUMNS:
        rows = conn.execute(text(f"SELECT id, {column} FROM {table} WHERE {column} LIKE 'https://%'")).all()
        for row_id, url in rows:
            key = unquote(urlsplit(url).path).lstrip("/")          # the path after the domain = the file's key
            with urllib.request.urlopen(url, timeout=30) as resp:   # Blob files are public: a normal download
                data, content_type = resp.read(), resp.headers.get_content_type()
            new_url = services.upload_file(key, data, content_type)  # the same function the app uses
            conn.execute(text(f"UPDATE {table} SET {column} = :new WHERE id = :id"), {"new": new_url, "id": row_id})
            print(f"{table} #{row_id}: {key} ({len(data)} bytes) -> {new_url}")