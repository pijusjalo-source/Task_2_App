"""File storage.

On Vercel the app's own disk is temporary and read-only (like Heroku's
ephemeral filesystem), so uploaded files must go to a separate storage
service: Vercel Blob. When a Blob store is connected to the project, Vercel
sets BLOB_READ_WRITE_TOKEN automatically and we use it.

Without that token (running on your laptop) files are saved to a local
folder instead, so the app still works for development.
"""

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

ALLOWED_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/csv": ".csv",
}
MAX_BYTES = 4 * 1024 * 1024  # Vercel Functions accept request bodies up to ~4.5 MB

LOCAL_DIR = Path("local_uploads")


class StorageError(ValueError):
    pass


@dataclass
class StoredFile:
    url: str
    name: str


def using_blob() -> bool:
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN"))


def storage_kind() -> str:
    return "Vercel Blob" if using_blob() else "Local folder (dev only)"


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return name[:80] or "file"


def validate_upload(filename: str, content_type: str, data: bytes) -> None:
    if not data:
        raise StorageError("The file is empty")
    if len(data) > MAX_BYTES:
        raise StorageError("File is too large (max 4 MB)")
    if content_type not in ALLOWED_TYPES:
        raise StorageError("Only PNG, JPG, WEBP, PDF or CSV files are allowed")


def save_file(folder: str, filename: str, content_type: str, data: bytes) -> StoredFile:
    """Store bytes and return a public URL to them."""
    path = f"{folder}/{uuid.uuid4().hex[:8]}-{_safe_name(filename)}"
    if using_blob():
        from vercel.blob import put  # official Vercel Python SDK

        result = put(path, data, access="public", content_type=content_type)
        return StoredFile(url=result.url, name=filename)

    target = LOCAL_DIR / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return StoredFile(url=f"/local-files/{path}", name=filename)


def delete_file(url: str | None) -> None:
    """Remove a stored file. Errors are ignored: a missing file is not fatal."""
    if not url:
        return
    try:
        if url.startswith("/local-files/"):
            (LOCAL_DIR / url.removeprefix("/local-files/")).unlink(missing_ok=True)
        elif using_blob():
            from vercel.blob import delete

            delete(url)
    except Exception as exc:  # noqa: BLE001
        print(f"[storage] could not delete {url}: {exc}")
