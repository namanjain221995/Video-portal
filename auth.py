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
        ({"username": u, "created_at": d.get("created_at"), "created_by": d.get("created_by")}
         for u, d in users.items()),
        key=lambda x: x["username"].lower(),
    )


def create_user(username: str, password: str, created_by: str = "") -> None:
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
        }
        _save_users(users)


def delete_user(username: str) -> None:
    with _lock:
        users = _load_users()
        if username in users:
            del users[username]
            _save_users(users)


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
