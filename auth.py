"""
auth.py
-------
Two tiers of login:

1. Admins   — defined in .env as ADMIN_USERS="user1:pass1,user2:pass2".
              Stored as plaintext in .env (gitignored). Add as many as you like.
              Admins can open the Admin page and create/delete normal users.

2. Users    — created at runtime by an admin through the Admin page.
              Persisted to users.json with passwords HASHED (werkzeug pbkdf2).
              Normal users can search + download but cannot open the Admin page.
"""

import os
import json
import threading
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash, check_password_hash

USERS_FILE = os.environ.get("USERS_FILE", "users.json")
_lock = threading.Lock()

# Departments granted to users created before per-department access existed (i.e.
# records with no "departments" field). The portal historically only served
# Interview-Success, so that preserves exactly what they could already see.
LEGACY_DEFAULT_DEPTS = ["Interview-Success"]


# ── Admins (from .env) ───────────────────────────────────────────────────────
def get_admins() -> dict:
    """Parse ADMIN_USERS='a:1,b:2' -> {'a': '1', 'b': '2'} (plaintext)."""
    raw = os.environ.get("ADMIN_USERS", "")
    admins = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user, pw = pair.split(":", 1)
        user = user.strip()
        if user:
            admins[user] = pw
    return admins


# ── Users (from users.json) ──────────────────────────────────────────────────
def _load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_users(users: dict) -> None:
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, USERS_FILE)


def list_users() -> list:
    users = _load_users()
    return sorted(
        ({"username": u,
          "created_at": d.get("created_at"),
          "created_by": d.get("created_by"),
          "departments": d.get("departments", list(LEGACY_DEFAULT_DEPTS)),
          "hosts": d.get("hosts") or {},
          "can_download": bool(d.get("can_download", True))}
         for u, d in users.items()),
        key=lambda x: x["username"].lower(),
    )


def user_access(username: str) -> dict:
    """The access a normal user was granted: which departments they may browse,
    an optional per-department host restriction ({dept: [host, …]} — a missing or
    empty entry means EVERY host in that department), and whether they may
    download (vs view-only). Missing fields fall back to the legacy defaults so
    pre-existing accounts keep working unchanged."""
    rec = _load_users().get((username or "").strip()) or {}
    depts = rec.get("departments")
    if depts is None:
        depts = list(LEGACY_DEFAULT_DEPTS)
    return {"departments": list(depts),
            "hosts": dict(rec.get("hosts") or {}),
            "can_download": bool(rec.get("can_download", True))}


def create_user(username: str, password: str, created_by: str = "",
                departments=None, hosts=None, can_download: bool = True) -> None:
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are both required.")
    if " " in username:
        raise ValueError("Username cannot contain spaces.")
    if username in get_admins():
        raise ValueError("That name is already an admin in .env.")
    with _lock:
        users = _load_users()
        if username in users:
            raise ValueError("A user with that name already exists.")
        users[username] = {
            "password": generate_password_hash(password),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "created_by": created_by,
            "departments": list(departments or []),
            "hosts": dict(hosts or {}),
            "can_download": bool(can_download),
        }
        _save_users(users)


def update_user_access(username: str, departments=None, hosts=None, can_download=None) -> None:
    """Change an existing user's department grant, per-department host
    restriction and/or download permission. Pass None for a field to leave it
    unchanged. Host entries for departments the user no longer has are pruned."""
    username = (username or "").strip()
    with _lock:
        users = _load_users()
        rec = users.get(username)
        if rec is None:
            raise ValueError("No such user.")
        if departments is not None:
            rec["departments"] = list(departments)
        if hosts is not None:
            rec["hosts"] = dict(hosts)
        granted = set(rec.get("departments") or [])
        rec["hosts"] = {d: h for d, h in (rec.get("hosts") or {}).items()
                        if d in granted and h}
        if can_download is not None:
            rec["can_download"] = bool(can_download)
        _save_users(users)


def delete_user(username: str) -> bool:
    with _lock:
        users = _load_users()
        if username in users:
            del users[username]
            _save_users(users)
            return True
    return False


# ── Verification ─────────────────────────────────────────────────────────────
def verify(username: str, password: str):
    """Return 'admin', 'user', or None."""
    username = (username or "").strip()
    admins = get_admins()
    if username in admins and admins[username] == password:
        return "admin"
    users = _load_users()
    rec = users.get(username)
    if rec and check_password_hash(rec["password"], password):
        return "user"
    return None
