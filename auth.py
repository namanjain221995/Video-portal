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

# Departments that were split into sub-departments in the bucket. A stored grant
# naming the old parent is read as a grant for exactly its children, so existing
# users keep precisely the access they had (the children together cover the whole
# parent) instead of silently losing it when the parent stops being a department.
# The same expansion is applied to the per-department host restriction — without
# it a "Training" host mask would key off a department that no longer exists and
# so restrict nothing, which would WIDEN access rather than preserve it.
SPLIT_DEPARTMENTS = {
    "Training": ["Training/Resume-Based", "Training/Advanced",
                 "Training/Interview-Readiness", "Training/Other"],
}


def _expand_split(departments, hosts):
    """Rewrite a stored grant so any split parent is replaced by its children."""
    if not any(d in SPLIT_DEPARTMENTS for d in departments):
        return list(departments), dict(hosts)
    out, seen = [], set()
    for dept in departments:
        for name in SPLIT_DEPARTMENTS.get(dept, [dept]):
            if name not in seen:
                seen.add(name)
                out.append(name)
    new_hosts = {}
    for dept, host_list in (hosts or {}).items():
        for name in SPLIT_DEPARTMENTS.get(dept, [dept]):
            if name in seen and host_list:
                # A child may also carry its own entry; keep the union.
                merged = list(dict.fromkeys(list(new_hosts.get(name, [])) + list(host_list)))
                new_hosts[name] = merged
    return out, new_hosts


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


def _stored_meetings(rec: dict) -> list:
    """A user's individual meeting grants, normalised to
    [{"meeting_id": str, "can_download": bool}, …].

    These are ADDITIVE and stand on their own: a meeting grant reaches exactly
    that meeting's files no matter which department they live in, which is the
    point — sharing one interview with someone who should not get the whole
    department. Each carries its own download flag, so a meeting can be shared
    view-only even when the account may download elsewhere."""
    out, seen = [], set()
    for entry in rec.get("meetings") or []:
        if isinstance(entry, str):                 # tolerate a bare id
            entry = {"meeting_id": entry}
        if not isinstance(entry, dict):
            continue
        meeting_id = str(entry.get("meeting_id") or "").strip()
        if not meeting_id or meeting_id in seen:
            continue
        seen.add(meeting_id)
        out.append({"meeting_id": meeting_id,
                    "can_download": bool(entry.get("can_download", False))})
    return out


def list_users() -> list:
    """Accounts as the Admin page should show them — i.e. the EFFECTIVE grant, with
    split parents already expanded, so the ticked boxes match what user_access()
    actually enforces. Saving a row then rewrites the stored grant in the new
    vocabulary, which is how a legacy record migrates itself."""
    users = _load_users()
    out = []
    for username, rec in users.items():
        depts, hosts = _expand_split(
            rec.get("departments", list(LEGACY_DEFAULT_DEPTS)),
            rec.get("hosts") or {},
        )
        out.append({"username": username,
                    "created_at": rec.get("created_at"),
                    "created_by": rec.get("created_by"),
                    "departments": depts,
                    "hosts": hosts,
                    "meetings": _stored_meetings(rec),
                    "can_download": bool(rec.get("can_download", True))})
    return sorted(out, key=lambda x: x["username"].lower())


def user_access(username: str) -> dict:
    """The access a normal user was granted: which departments they may browse,
    an optional per-department host restriction ({dept: [host, …]} — a missing or
    empty entry means EVERY host in that department), individually shared meetings
    ({meeting_id: can_download}), and whether they may download inside their
    departments (vs view-only). Missing fields fall back to the legacy defaults so
    pre-existing accounts keep working unchanged."""
    rec = _load_users().get((username or "").strip()) or {}
    depts = rec.get("departments")
    if depts is None:
        depts = list(LEGACY_DEFAULT_DEPTS)
    depts, hosts = _expand_split(depts, rec.get("hosts") or {})
    return {"departments": depts,
            "hosts": hosts,
            "meetings": {m["meeting_id"]: m["can_download"] for m in _stored_meetings(rec)},
            "can_download": bool(rec.get("can_download", True))}


def create_user(username: str, password: str, created_by: str = "",
                departments=None, hosts=None, can_download: bool = True,
                meetings=None) -> None:
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
            "meetings": _stored_meetings({"meetings": meetings}),
            "can_download": bool(can_download),
        }
        _save_users(users)


def update_user_access(username: str, departments=None, hosts=None,
                       can_download=None, meetings=None) -> None:
    """Change an existing user's department grant, per-department host
    restriction, individually shared meetings and/or download permission. Pass
    None for a field to leave it unchanged. Host entries for departments the user
    no longer has are pruned.

    Meeting grants are deliberately NOT pruned against the department list: a
    shared meeting is meant to survive on its own, and dropping it when the
    department is unticked would silently revoke the very access it exists for."""
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
        if meetings is not None:
            rec["meetings"] = _stored_meetings({"meetings": meetings})
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
