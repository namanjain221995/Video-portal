"""On-disk store for webcam capture photos (login enrolment + screen-capture snapshots).

These photos are identifiable personal data. Keep CAPTURE_DIR off version control
and backups, restrict OS permissions, serve them to admins only, and honour the
retention window. A browser cannot capture the camera covertly — the OS shows a
permission prompt and an active-camera indicator every time, by design.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CAPTURE_DIR = os.getenv("CAPTURE_DIR", "captures").strip() or "captures"


def _retention_days() -> int:
    try:
        return max(0, min(int(os.getenv("CAPTURE_RETENTION_DAYS", "90")), 3650))
    except (TypeError, ValueError):
        return 90


CAPTURE_RETENTION_DAYS = _retention_days()

_MAX_BYTES = 3 * 1024 * 1024                       # a webcam JPEG is far under this
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(jpg|png|webp)$")


def _dir() -> Path:
    path = Path(CAPTURE_DIR).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _detect_ext(data: bytes):
    """Trust the bytes, not the client-declared mime, to pick an extension."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_data_url(data_url) -> str | None:
    """Validate + store a base64 image data URL (data:image/jpeg;base64,...).
    Returns the stored filename, or None if it was missing/oversized/not an image."""
    if not isinstance(data_url, str) or "," not in data_url:
        return None
    header, _, encoded = data_url.partition(",")
    if "base64" not in header.lower():
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if not raw or len(raw) > _MAX_BYTES:
        return None
    ext = _detect_ext(raw)
    if ext is None:
        return None

    name = secrets.token_hex(16) + ext
    try:
        target = _dir() / name
        with open(target, "wb") as handle:
            handle.write(raw)
        if os.name != "nt":
            try:
                os.chmod(target, 0o600)
            except OSError:
                logger.warning("Could not restrict capture photo permissions")
        return name
    except OSError:
        logger.exception("Could not store capture photo")
        return None


def path_for(name) -> str | None:
    """Absolute path for a stored photo, or None. Rejects anything that is not a
    plain, expected-extension basename (blocks path traversal)."""
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        return None
    target = _dir() / name
    return str(target) if target.is_file() else None


def purge_old(days: int | None = None) -> int:
    """Delete photos older than the retention window. Returns how many were removed."""
    days = CAPTURE_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86_400
    removed = 0
    try:
        for entry in _dir().iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed
