"""Durable SQLite-backed audit events for the recording portal.

The module is intentionally dependency-free.  Each process opens short-lived
connections, while WAL mode, a busy timeout, and idempotent schema creation
make it safe for multiple Gunicorn workers to use the same database file.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

AUDIT_DB = os.getenv("AUDIT_DB", "audit.db").strip() or "audit.db"


def _retention_days_from_env() -> int:
    raw_value = os.getenv("AUDIT_RETENTION_DAYS", "180").strip()
    try:
        return max(0, min(int(raw_value), 365_000))
    except (TypeError, ValueError):
        logger.warning("Invalid AUDIT_RETENTION_DAYS; using 180 days")
        return 180


AUDIT_RETENTION_DAYS = _retention_days_from_env()


def _max_rows_from_env() -> int:
    raw_value = os.getenv("AUDIT_MAX_ROWS", "100000").strip()
    try:
        return max(0, min(int(raw_value), 10_000_000))
    except (TypeError, ValueError):
        logger.warning("Invalid AUDIT_MAX_ROWS; using 100000 rows")
        return 100_000


AUDIT_MAX_ROWS = _max_rows_from_env()

_BUSY_TIMEOUT_MS = 10_000
_MAX_PER_PAGE = 200
_MAX_DETAILS_CHARS = 16_384
_MAX_DETAILS_DEPTH = 6
_MAX_DETAILS_ITEMS = 50
_MAX_DETAIL_STRING = 2_000
_FIELD_LIMITS = {
    "action": 96,
    "username": 255,
    "role": 64,
    "candidate": 255,
    "host": 255,
    "meeting_id": 128,
    "recording_date": 64,
    "department": 128,
    "file_type": 64,
    "resource_key": 2_048,
}

_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_DATABASES: set[str] = set()
_PASSWORD_IN_TEXT_RE = re.compile(
    r"(?i)\b(password|passwd|passcode|pwd)\b(\s*(?:=|:)\s*|\s+)([^\s,;&]+)"
)


def _utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _database_path() -> str:
    path = Path(AUDIT_DB).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _initialize_schema(connection: sqlite3.Connection, database: str) -> None:
    """Create the schema once per worker; SQLite serializes other workers."""
    if database in _INITIALIZED_DATABASES:
        return

    with _SCHEMA_LOCK:
        if database in _INITIALIZED_DATABASES:
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    candidate TEXT NOT NULL DEFAULT '',
                    host TEXT NOT NULL DEFAULT '',
                    meeting_id TEXT NOT NULL DEFAULT '',
                    recording_date TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL DEFAULT '',
                    file_type TEXT NOT NULL DEFAULT '',
                    resource_key TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '{}',
                    success INTEGER NOT NULL DEFAULT 1
                        CHECK (success IN (0, 1))
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_timestamp "
                "ON audit_events(timestamp DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_action_timestamp "
                "ON audit_events(action, timestamp DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_username_timestamp "
                "ON audit_events(username, timestamp DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_dedupe "
                "ON audit_events(username, action, resource_key, timestamp DESC)"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        _INITIALIZED_DATABASES.add(database)


def _connect() -> sqlite3.Connection:
    database = _database_path()
    connection = sqlite3.connect(
        database,
        timeout=_BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
    )
    try:
        if os.name != "nt":
            try:
                os.chmod(database, 0o600)
            except OSError:
                logger.warning("Could not restrict audit database permissions")
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        _initialize_schema(connection, database)
        return connection
    except Exception:
        connection.close()
        raise


def _bounded_field(value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip()[: _FIELD_LIMITS[field]]


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        any(
            part in normalized
            for part in ("password", "passwd", "passcode", "secret")
        )
        or normalized.endswith(("token", "apikey", "accesskeyid", "cookie"))
        or normalized in {
            "pwd",
            "token",
            "accesstoken",
            "refreshtoken",
            "sessiontoken",
            "apikey",
            "authorization",
            "cookie",
        }
    )


def _redact_password_text(value: str) -> str:
    value = value[:_MAX_DETAIL_STRING]
    return _PASSWORD_IN_TEXT_RE.sub(r"\1\2[REDACTED]", value)


def _safe_json_value(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _redact_password_text(value)
    if isinstance(value, bytes):
        return _redact_password_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if depth >= _MAX_DETAILS_DEPTH:
        return "[truncated: maximum depth]"

    seen = seen if seen is not None else set()
    value_id = id(value)
    if value_id in seen:
        return "[circular reference]"

    if isinstance(value, Mapping):
        seen.add(value_id)
        result: dict[str, Any] = {}
        try:
            items = list(islice(value.items(), _MAX_DETAILS_ITEMS + 1))
            for raw_key, raw_item in items[:_MAX_DETAILS_ITEMS]:
                key = str(raw_key)[:128]
                if _is_sensitive_key(key):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = _safe_json_value(
                        raw_item, depth=depth + 1, seen=seen
                    )
            if len(items) > _MAX_DETAILS_ITEMS:
                result["_truncated_items"] = max(
                    1, len(value) - _MAX_DETAILS_ITEMS
                )
            return result
        finally:
            seen.discard(value_id)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        seen.add(value_id)
        try:
            result = [
                _safe_json_value(item, depth=depth + 1, seen=seen)
                for item in value[:_MAX_DETAILS_ITEMS]
            ]
            if len(value) > _MAX_DETAILS_ITEMS:
                result.append(f"[truncated {len(value) - _MAX_DETAILS_ITEMS} items]")
            return result
        finally:
            seen.discard(value_id)

    return _redact_password_text(str(value))


def _serialize_details(details: Any) -> str:
    safe_details = {} if details is None else _safe_json_value(details)
    serialized = json.dumps(
        safe_details,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(serialized) <= _MAX_DETAILS_CHARS:
        return serialized

    shortened = {
        "_truncated": True,
        # Re-encoding the JSON preview can at most double its size through
        # quote/backslash escaping, so half the storage limit remains bounded.
        "preview": serialized[: (_MAX_DETAILS_CHARS - 100) // 2],
    }
    return json.dumps(shortened, ensure_ascii=False, separators=(",", ":"))


def _purge_expired(connection: sqlite3.Connection, now: datetime) -> None:
    if AUDIT_RETENTION_DAYS == 0:
        return
    cutoff = _utc_iso(now - timedelta(days=AUDIT_RETENTION_DAYS))
    connection.execute("DELETE FROM audit_events WHERE timestamp < ?", (cutoff,))


def record_event(
    action: Any,
    username: Any = "",
    role: Any = "",
    candidate: Any = "",
    host: Any = "",
    meeting_id: Any = "",
    recording_date: Any = "",
    department: Any = "",
    file_type: Any = "",
    resource_key: Any = "",
    details: Any = None,
    success: bool = True,
    dedupe_seconds: int = 0,
) -> bool:
    """Persist one audit event, returning ``False`` on failure or deduplication.

    Audit failures are logged without event data and are never propagated into
    the portal request that produced them.
    """
    connection: sqlite3.Connection | None = None
    try:
        values = {
            "action": _bounded_field(action, "action"),
            "username": _bounded_field(username, "username"),
            "role": _bounded_field(role, "role"),
            "candidate": _bounded_field(candidate, "candidate"),
            "host": _bounded_field(host, "host"),
            "meeting_id": _bounded_field(meeting_id, "meeting_id"),
            "recording_date": _bounded_field(recording_date, "recording_date"),
            "department": _bounded_field(department, "department"),
            "file_type": _bounded_field(file_type, "file_type"),
            "resource_key": _bounded_field(resource_key, "resource_key"),
        }
        if not values["action"]:
            logger.warning("Audit event was not recorded because action is empty")
            return False

        try:
            dedupe_window = max(0, min(int(dedupe_seconds), 31_536_000))
        except (TypeError, ValueError):
            dedupe_window = 0

        timestamp_value = datetime.now(timezone.utc)
        timestamp = _utc_iso(timestamp_value)
        serialized_details = _serialize_details(details)

        connection = _connect()
        connection.execute("BEGIN IMMEDIATE")
        _purge_expired(connection, timestamp_value)

        if dedupe_window:
            dedupe_cutoff = _utc_iso(
                timestamp_value - timedelta(seconds=dedupe_window)
            )
            duplicate = connection.execute(
                """
                SELECT 1
                FROM audit_events
                WHERE username = ? AND action = ? AND resource_key = ?
                  AND timestamp >= ?
                LIMIT 1
                """,
                (
                    values["username"],
                    values["action"],
                    values["resource_key"],
                    dedupe_cutoff,
                ),
            ).fetchone()
            if duplicate is not None:
                connection.commit()
                return False

        cursor = connection.execute(
            """
            INSERT INTO audit_events (
                timestamp, action, username, role, candidate, host,
                meeting_id, recording_date, department, file_type,
                resource_key, details, success
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                values["action"],
                values["username"],
                values["role"],
                values["candidate"],
                values["host"],
                values["meeting_id"],
                values["recording_date"],
                values["department"],
                values["file_type"],
                values["resource_key"],
                serialized_details,
                1 if success else 0,
            ),
        )
        if AUDIT_MAX_ROWS:
            # AUTOINCREMENT ids are monotonic, so this indexed delete guarantees
            # no more than AUDIT_MAX_ROWS live rows without a COUNT(*) scan on
            # every request. SQLite reuses the freed pages for later events.
            oldest_allowed_id = int(cursor.lastrowid or 0) - AUDIT_MAX_ROWS
            if oldest_allowed_id > 0:
                connection.execute(
                    "DELETE FROM audit_events WHERE id <= ?", (oldest_allowed_id,)
                )
        connection.commit()
        return True
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        logger.exception("Unable to record audit event")
        return False
    finally:
        if connection is not None:
            connection.close()


def _positive_int(value: Any, default: int, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    result = max(1, result)
    return min(result, maximum) if maximum is not None else result


def _date_bound(value: Any, *, end: bool) -> tuple[str, bool] | None:
    """Return an ISO bound and whether it is an exclusive upper bound."""
    if value is None or not str(value).strip():
        return None
    raw_value = str(value).strip()

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_value):
            parsed_date = date.fromisoformat(raw_value)
            parsed = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
            if end:
                parsed += timedelta(days=1)
            return _utc_iso(parsed), end

        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid audit date: {raw_value!r}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _utc_iso(parsed), False


def _like_pattern(value: str) -> str:
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def _decoded_event(row: sqlite3.Row) -> dict[str, Any]:
    event = dict(row)
    event["occurred_at"] = event.get("timestamp", "")
    try:
        event["details"] = json.loads(event.get("details") or "{}")
    except (TypeError, json.JSONDecodeError):
        event["details"] = {"_invalid": True}
    event["success"] = bool(event.get("success"))
    return event


def list_events(
    page: int = 1,
    per_page: int = 50,
    action: Any = "",
    username: Any = "",
    q: Any = "",
    date_from: Any = "",
    date_to: Any = "",
) -> dict[str, Any]:
    """Return newest-first audit events with pagination and admin filters."""
    requested_page = _positive_int(page, 1)
    page_size = _positive_int(per_page, 50, _MAX_PER_PAGE)
    action_filter = _bounded_field(action, "action")
    username_filter = _bounded_field(username, "username")
    query_text = str(q or "").strip()[:255]
    from_bound = _date_bound(date_from, end=False)
    to_bound = _date_bound(date_to, end=True)

    conditions: list[str] = []
    parameters: list[Any] = []
    if action_filter:
        conditions.append("action = ?")
        parameters.append(action_filter)
    if username_filter:
        conditions.append("username = ?")
        parameters.append(username_filter)
    if from_bound:
        conditions.append("timestamp >= ?")
        parameters.append(from_bound[0])
    if to_bound:
        conditions.append("timestamp < ?" if to_bound[1] else "timestamp <= ?")
        parameters.append(to_bound[0])
    if query_text:
        search_pattern = _like_pattern(query_text)
        searchable = (
            "action",
            "username",
            "role",
            "candidate",
            "host",
            "meeting_id",
            "recording_date",
            "department",
            "file_type",
            "resource_key",
            "details",
        )
        conditions.append(
            "(" + " OR ".join(f"{field} LIKE ? ESCAPE '!'" for field in searchable) + ")"
        )
        parameters.extend([search_pattern] * len(searchable))

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    connection = _connect()
    try:
        if AUDIT_RETENTION_DAYS:
            connection.execute("BEGIN IMMEDIATE")
            _purge_expired(connection, datetime.now(timezone.utc))
            connection.commit()

        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_events" + where_clause,
                parameters,
            ).fetchone()[0]
        )
        pages = math.ceil(total / page_size) if total else 0
        current_page = min(requested_page, pages) if pages else 1
        offset = (current_page - 1) * page_size

        rows = connection.execute(
            "SELECT * FROM audit_events"
            + where_clause
            + " ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
            [*parameters, page_size, offset],
        ).fetchall()

        actions = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT action FROM audit_events "
                "WHERE action <> '' ORDER BY action LIMIT 500"
            ).fetchall()
        ]
        usernames = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT username FROM audit_events "
                "WHERE username <> '' ORDER BY username LIMIT 500"
            ).fetchall()
        ]

        return {
            "events": [_decoded_event(row) for row in rows],
            "total": total,
            "pages": pages,
            "page": current_page,
            "per_page": page_size,
            "actions": actions,
            "users": usernames,
            "filter_options": {
                "actions": actions,
                "usernames": usernames,
            },
        }
    finally:
        connection.close()
