"""
app.py — Flask entry point for the Interview-Success recording portal.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env        # then edit ADMIN_USERS etc.
    python app.py               # http://localhost:8000
"""
#
import io
import os
import re
import json
import hashlib
import functools

from dotenv import load_dotenv
load_dotenv()  # must run before importing modules that read os.environ at import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, send_file, abort, Response,
)

import auth
import audit_service
import s3_service

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
# The largest legitimate body is the bulk-download key list: BULK_ZIP_MAX_FILES
# (250 by default) S3 keys at ~130 bytes each ≈ 33 KB. 1 MiB leaves ~30x headroom
# even if that limit is raised, while still bounding a hostile request.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024


@app.context_processor
def _asset_helper():
    """asset('search.js') -> /static/search.js?v=<mtime>, so a redeploy busts any
    cached copy and the browser always runs the JS/CSS that's actually deployed."""
    def asset(filename):
        try:
            v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            v = 0
        return url_for("static", filename=filename, v=v)
    return {"asset": asset}


# ─────────────────────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────────────────────
def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not signed in."}), 401
            return redirect(url_for("login_page", next=request.path))
        return fn(*a, **k)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not signed in."}), 401
            return redirect(url_for("login_page"))
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admins only."}), 403
            return redirect(url_for("search_page"))
        return fn(*a, **k)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Access control (authoritative — derived from the session, never the client)
# ─────────────────────────────────────────────────────────────────────────────
def _current_access():
    """What the signed-in user may reach:

    * ``departments`` — the department folders they can browse,
    * ``hosts``       — an optional per-department host restriction
      ({dept: [host, …]}; no entry means every host in that department),
    * ``meetings``    — individually shared meetings ({meeting_id: can_download}),
      which reach that meeting's files wherever they live, on top of the
      department grant rather than inside it,
    * ``can_download``— whether they may download within their departments.

    Admins get every department, every host and full download rights, so they
    need no meeting grants.
    """
    known = s3_service.all_departments()   # configured + auto-discovered
    if session.get("role") == "admin":
        return {"departments": list(known), "hosts": {}, "meetings": {}, "can_download": True}
    info = auth.user_access(session.get("user", ""), known)
    # Intersect with the departments that actually exist, so a stale grant can't
    # widen access if a department is renamed/removed in config.
    depts = [d for d in info["departments"] if d in known]
    hosts = {d: hs for d, hs in (info.get("hosts") or {}).items() if d in depts and hs}
    # Meeting grants are NOT intersected with anything: they are identified by the
    # meeting id alone and are meant to work outside the department grant.
    meetings = {str(k): bool(v) for k, v in (info.get("meetings") or {}).items() if k}
    return {"departments": depts, "hosts": hosts, "meetings": meetings,
            "can_download": bool(info["can_download"])}


def _authorized(key, access):
    """The indexed record for ``key`` if this caller may reach it, else None.

    Every route that touches a file goes through here so the access rule (a
    department grant OR an individually shared meeting) is applied identically,
    and adding a new route cannot accidentally skip half of it."""
    return s3_service.authorized_record(
        key, access["departments"], access["hosts"], access["meetings"])


def _can_download_anything(access):
    """Whether to offer download UI at all: either the account may download inside
    its departments, or at least one shared meeting was granted with download."""
    return bool(access["can_download"]) or any((access.get("meetings") or {}).values())


def _audit(action, record=None, details=None, username=None, role=None,
           success=True, dedupe_seconds=0, **fields):
    """Best-effort audit event built from server-authoritative session/record data.

    Audit failures never interrupt the user's request. Passwords and presigned
    URLs are intentionally never passed to this helper.
    """
    payload = {
        "username": session.get("user", "") if username is None else username,
        "role": session.get("role", "") if role is None else role,
        "success": success,
        "dedupe_seconds": dedupe_seconds,
        "details": dict(details or {}),
    }
    if record:
        candidates = record.get("candidates") or [record.get("candidate", "")]
        payload.update({
            "candidate": ", ".join(str(c) for c in candidates if c),
            "host": record.get("host", ""),
            "meeting_id": record.get("meeting_id", ""),
            "recording_date": record.get("date", ""),
            "department": record.get("department", ""),
            "file_type": record.get("file_type", ""),
        })
        payload["details"].update({
            "company": record.get("company", ""),
            "round": record.get("round", ""),
            "filename": record.get("filename", ""),
            "category": record.get("category", ""),
        })
    payload.update(fields)
    return audit_service.record_event(action, **payload)


class _BadDepartments(ValueError):
    """An admin-supplied department list contained names the portal doesn't know."""


def _clean_departments(raw):
    """Validate an admin-supplied department list against the known vocabulary
    (configured + auto-discovered).

    Raises rather than silently dropping unknown names: quietly returning a
    shorter list would answer {"ok": true} while stripping the user's access —
    which is exactly what would happen to a Training/* sub-department submitted
    while the index is still warming."""
    if raw is None:
        return []                      # key omitted entirely -> grant nothing
    if not isinstance(raw, list):
        raise _BadDepartments("Departments must be a list.")
    valid = set(s3_service.all_departments())
    seen, out, unknown = set(), [], []
    for d in raw:
        if d not in valid:
            if d not in unknown:
                unknown.append(str(d))
        elif d not in seen:
            seen.add(d)
            out.append(d)
    if unknown:
        raise _BadDepartments(
            "Unknown department(s): " + ", ".join(unknown[:10])
            + ". If one was just created in S3, refresh the index and try again."
        )
    return out


# A meeting folder in S3 is always all-digit (see _parse_key), so anything else
# could never match a record. Bounds keep a hostile payload from bloating
# users.json or the per-request access mask.
_MEETING_ID_MAX_LEN = 32
_MEETING_GRANT_MAX = 500

# ASCII digits only, deliberately: str.isdigit() also says yes to Arabic-Indic
# digits and to superscripts like "²", none of which can ever name an S3
# folder, so accepting them would only put ids in users.json that are guaranteed
# to match nothing.
_MEETING_ID_RE = re.compile(r"\A[0-9]{1,%d}\Z" % _MEETING_ID_MAX_LEN)

# Admins paste ids straight out of a spreadsheet column, a Slack message or a
# mail, so the separator is whatever came with them: newlines and tabs, commas
# and spaces, semicolons, pipes, and the fullwidth comma / ideographic space a
# copy made in some locales carries. Tokens are split on SEPARATORS ONLY and kept
# whole — splitting on "anything that is not a digit" would quietly cut a typo'd
# id ("9460172O227", with a capital O) into two plausible-looking ids and share
# two meetings nobody asked for, where keeping it whole lets it be reported back
# to the admin as one rejected token they can see and fix.
# \s covers tab/CR/LF/NBSP in both Python and JS, but NOT the zero-width space,
# word joiner or BOM that a copy out of Slack, Notion or a Word document carries.
# Those matter: without them two ids fuse into one token, which is at least
# reported here but is exactly the kind of "I pasted it and nothing happened"
# that this whole change exists to end.
# The non-ASCII ones are written as escapes rather than pasted in, so no
# invisible character sits in this file waiting to be deleted by a later edit.
_MEETING_ID_SPLIT_RE = re.compile("[\\s,;|\uff0c\u3000\u200b\ufeff\u2060]+")

# Wrapping punctuation a copied cell brings with it ("94601720227", [94601720227]).
# Only the OUTSIDE of a token is trimmed, never its interior, so a typo'd id
# still comes back whole and rejected rather than quietly repaired into a
# different, real meeting.
_MEETING_ID_TRIM_RE = re.compile("""^["'(\\[<]+|["')\\]>]+$""")

# A paste is bounded before it is split: MAX_CONTENT_LENGTH already caps the body
# at 1 MiB, but splitting a megabyte of prose into tokens to throw nearly all of
# them away is work nobody asked for.
_MEETING_PASTE_MAX_CHARS = 64 * 1024


def _split_meeting_ids(raw, limit=_MEETING_GRANT_MAX):
    """Parse pasted text (or a JSON list) into ``(ids, invalid, truncated)``.

    ``ids`` are the unique well-formed ids in the order they were pasted,
    ``invalid`` the tokens that could never name a meeting folder, and
    ``truncated`` says the paste ran past what one account may hold. Every token
    lands in exactly one of those three buckets, because an admin who pastes 40
    ids and quietly gets 38 grants has no way to tell which two went missing or
    why — which is the whole failure this parser exists to prevent."""
    if isinstance(raw, (list, tuple)):
        # Re-tokenised through the same rule rather than trusted as-is, so a JSON
        # entry of "1;2" is normalised exactly like the pasted form. One parser.
        raw = "\n".join(str(t) for t in raw[:5000])
    text = str(raw or "")[:_MEETING_PASTE_MAX_CHARS]
    ids, invalid, seen = [], [], set()
    for token in _MEETING_ID_SPLIT_RE.split(text.strip()):
        token = _MEETING_ID_TRIM_RE.sub("", token.strip())
        if not token:
            continue
        if not _MEETING_ID_RE.match(token):
            # Capped and deduped: this report is for a human to read, and someone
            # pasting a wall of prose should not turn it into a 1000-item string.
            if token not in invalid and len(invalid) < 25:
                invalid.append(token)
            continue
        if token in seen:
            continue
        seen.add(token)
        ids.append(token)
    return ids[:limit], invalid, len(ids) > limit


def _clean_meetings(raw):
    """Sanitise an admin-supplied meeting-grant list into
    [{"meeting_id": str, "can_download": bool}, …].

    Accepts a bare id string or an object, dedupes on the id (the LAST entry
    wins, so re-adding a meeting to toggle its download flag behaves as expected)
    and silently drops anything that could not name a real meeting. Unlike
    departments this does not raise on unknown ids: an id may legitimately be
    granted before the index has caught up with a just-uploaded recording.

    The cap counts DISTINCT ids rather than slicing the incoming list: slicing
    first meant a payload of 500 entries carrying 200 duplicates yielded only 300
    grants — a limit nobody asked for, and one that pasting a list of ids makes
    easy to hit by accident."""
    return _clean_meetings_report(raw)[0]


def _clean_meetings_report(raw):
    """``_clean_meetings`` plus how many distinct valid grants ran past the cap,
    as ``(grants, over_cap)``. The write path needs that count so it can refuse
    instead of storing a silently shorter list than the admin asked for."""
    if raw is None:
        return None, 0                 # key omitted -> leave the grant unchanged
    if not isinstance(raw, list):
        return [], 0
    out = {}
    for entry in raw:
        if isinstance(entry, str):
            entry = {"meeting_id": entry}
        if not isinstance(entry, dict):
            continue
        meeting_id = str(entry.get("meeting_id") or "").strip()
        if not _MEETING_ID_RE.match(meeting_id):
            continue
        out[meeting_id] = {"meeting_id": meeting_id,
                           "can_download": bool(entry.get("can_download", False))}
    grants = list(out.values())
    return grants[:_MEETING_GRANT_MAX], max(0, len(grants) - _MEETING_GRANT_MAX)


class _BadMeetings(ValueError):
    """An admin-supplied meeting-grant list could not be stored as it was given."""


def _validated_meetings(raw):
    """``_clean_meetings`` for the WRITE path, refusing where the quiet version
    would destroy access instead of reporting it.

    A grant list is access control, and PATCH REPLACES it wholesale, so storing
    less of it than was asked for is never right without saying so. Two ways the
    quiet version loses grants, both previously answered ``{"ok": true}``:

    * a non-list value — ``{"meetings": "94601720227,93490389605"}``, the exact
      string an admin pastes — cleans to ``[]`` and wipes every meeting the
      account had;
    * more than ``_MEETING_GRANT_MAX`` distinct ids is truncated to the cap, so
      the tail of a large paste vanishes with nothing said.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _BadMeetings(
            "Shared meetings must be a list of meeting IDs, not a single string. "
            "Paste the IDs into the Shared meetings box and they will be split "
            "for you.")
    grants, over_cap = _clean_meetings_report(raw)
    if over_cap:
        raise _BadMeetings(
            f"That is {len(grants) + over_cap} shared meetings, and at most "
            f"{_MEETING_GRANT_MAX} can be shared with one account. Remove "
            f"{over_cap} and save again — nothing has been changed.")
    return grants


def _clean_hosts(raw, departments):
    """Sanitise an admin-supplied {department: [host, …]} restriction map: keep
    entries only for departments actually granted, with unique non-empty host
    strings. A department with no (or an empty) entry means ALL its hosts."""
    if not isinstance(raw, dict):
        return {}
    granted = set(departments or [])
    out = {}
    for dept, hosts in raw.items():
        if dept not in granted or not isinstance(hosts, list):
            continue
        seen, clean = set(), []
        for h in hosts:
            if isinstance(h, str):
                h = h.strip()
                if h and len(h) <= 200 and h not in seen:
                    seen.add(h)
                    clean.append(h)
        if clean:
            out[dept] = clean
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return redirect(url_for("search_page") if session.get("user") else url_for("login_page"))


@app.route("/login")
def login_page():
    if session.get("user"):
        return redirect(url_for("search_page"))
    return render_template("login.html")


@app.route("/search")
@login_required
def search_page():
    return render_template(
        "search.html",
        username=session.get("user"),
        is_admin=session.get("role") == "admin",
        demo=s3_service.DEMO_MODE,
    )


@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html", username=session.get("user"))


@app.route("/logs")
@admin_required
def logs_page():
    response = app.make_response(render_template("logs.html", username=session.get("user")))
    response.headers["Cache-Control"] = "no-store"
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Auth API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def api_login():
    json_data = request.get_json(silent=True)
    if request.is_json and not isinstance(json_data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    data = json_data if json_data is not None else request.form
    raw_username = data.get("username") or ""
    username = raw_username.strip() if isinstance(raw_username, str) else ""
    password = data.get("password") or ""
    if not isinstance(password, str):
        password = ""
    role = auth.verify(username, password)
    if not role:
        return jsonify({"error": "Wrong username or password."}), 401
    session["user"] = username
    session["role"] = role
    _audit("login", username=username, role=role)
    return jsonify({"ok": True, "username": username, "role": role})


@app.route("/logout")
def logout():
    if session.get("user"):
        _audit("logout")
    session.clear()
    return redirect(url_for("login_page"))


# ─────────────────────────────────────────────────────────────────────────────
# Search API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/filters")
@login_required
def api_filters():
    try:
        access = _current_access()
        opts = s3_service.filter_options(departments=access["departments"],
                                         allowed_hosts=access["hosts"],
                                         allowed_meetings=access["meetings"])
        # Show the user's full assigned set (even a department with no files yet),
        # not just the ones that happen to have records.
        opts["departments"] = sorted(access["departments"], key=str.lower)
        hbd = opts.get("hosts_by_department", {})
        for d in access["departments"]:
            hbd.setdefault(d, [])          # a department with no files yet -> no hosts
        opts["hosts_by_department"] = hbd
        opts["can_download"] = _can_download_anything(access)
        # A cold cache is normal (the index warms in the background), but a FAILED
        # build looks exactly the same from here — empty hosts, ready=false. Pass
        # the reason through so the page can say "credentials expired" instead of
        # spinning on "indexing bucket…" forever.
        cache = s3_service.cache_info()
        if cache.get("error"):
            cache["error"] = _s3_err(cache["error"])
        opts["cache"] = cache
        return jsonify(opts)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _date_range_label(date_from, date_to):
    """A picked date range as one short string for the audit log's date column
    ("2026-06-01 to 2026-06-30", "from 2026-06-01", "until 2026-06-30")."""
    date_from = (date_from or "").strip()
    date_to = (date_to or "").strip()
    if date_from and date_to:
        return date_from if date_from == date_to else f"{date_from} to {date_to}"
    if date_from:
        return f"from {date_from}"
    if date_to:
        return f"until {date_to}"
    return ""


@app.route("/api/search")
@login_required
def api_search():
    try:
        access = _current_access()
        page = max(1, _int_arg("page", 1))
        per_page = max(1, min(_int_arg("per_page", 100), s3_service.RESULT_LIMIT))
        filters = {
            "candidate": request.args.get("candidate", ""),
            "company": request.args.get("company", ""),
            "date": request.args.get("date", ""),
            # Inclusive YYYY-MM-DD bounds from the date-range picker; either side
            # may be empty (open-ended), and both equal is a single day. Anything
            # that is not a full ISO date is ignored by s3_service.search.
            "date_from": request.args.get("date_from", ""),
            "date_to": request.args.get("date_to", ""),
            # A comma-separated set of exact YYYY-MM-DD days, for a selection of
            # separate dates that no single range can express. Non-ISO values are
            # dropped by s3_service.search.
            "dates": request.args.get("dates", ""),
            "meeting_id": request.args.get("meeting_id", ""),
            # Comma-separated category keys — the file-type filter is multi-select.
            "file_type": request.args.get("file_type", ""),
            "host": request.args.get("host", ""),
            "department": request.args.get("department", ""),
        }
        sort = request.args.get("sort", "")
        results, total, total_size = s3_service.search(
            **filters,
            allowed_departments=access["departments"],
            allowed_hosts=access["hosts"],
            allowed_meetings=access["meetings"],
            limit=per_page,
            offset=(page - 1) * per_page,
            sort=sort,
        )
        # Download is decided per row now that a single meeting can be shared
        # view-only. Copies, never mutation: `results` holds the shared cached
        # records and annotating them in place would leak one user's permission
        # into the next user's response.
        results = [dict(r, can_download=s3_service.record_downloadable(r, access))
                   for r in results]
        if any(str(v or "").strip() for v in filters.values()):
            _audit(
                "search",
                candidate=filters["candidate"],
                host=filters["host"],
                meeting_id=filters["meeting_id"],
                # One readable "what date did they ask for?" column, whether that
                # was typed free-text or picked as a range.
                recording_date=filters["date"] or filters["dates"] or _date_range_label(
                    filters["date_from"], filters["date_to"]),
                department=filters["department"],
                file_type=filters["file_type"],
                details={
                    "company": filters["company"],
                    "page": page,
                    "per_page": per_page,
                    "sort": sort,
                    "results_on_page": len(results),
                    "total_results": total,
                    "total_size": total_size,
                },
            )
        return jsonify({
            "count": len(results),
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, -(-total // per_page)),   # ceil
            "truncated": total > len(results),
            "total_size": total_size,
            # "may download something" — each row carries its own flag.
            "can_download": _can_download_anything(access),
            "results": results,
        })
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    try:
        s3_service.get_records(force=True)
        cache = s3_service.cache_info()
        _audit("refresh", details={"indexed_files": cache.get("count", 0)})
        return jsonify({"ok": True, "cache": cache})
    except Exception as e:
        _audit("refresh", success=False, details={"reason": "Index refresh failed"})
        return jsonify({"error": _s3_err(e)}), 502


# ─────────────────────────────────────────────────────────────────────────────
# Download API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/download")
@login_required
def api_download_one():
    key = request.args.get("key", "")
    access = _current_access()
    # Authorize the file FIRST, then ask whether this caller may download this
    # particular one: a meeting can be shared view-only with an account that
    # downloads freely elsewhere, so the permission is per record, not per user.
    rec = _authorized(key, access)
    if rec is None:
        abort(404, "File not found.")
    if not s3_service.record_downloadable(rec, access):
        abort(403, "This recording is view-only for your account.")

    if s3_service.DEMO_MODE:
        data, fname = s3_service.demo_file_response(key)
        if data is None:
            abort(404)
        _audit("download", record=rec)
        return send_file(io.BytesIO(data), as_attachment=True,
                         download_name=fname, mimetype="text/plain")

    try:
        url = s3_service.presigned_url(key)
        _audit("download", record=rec)
        return redirect(url)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


@app.route("/api/view")
@login_required
def api_view_one():
    """Inline, view-only access to a file. Returns a presigned URL with an 'inline'
    disposition (so the browser plays/renders it) instead of forcing a download.
    Available to every signed-in user for files in their allowed departments —
    including view-only accounts that may not use /api/download."""
    key = request.args.get("key", "")
    access = _current_access()
    rec = _authorized(key, access)
    if rec is None:
        abort(404, "File not found.")

    if s3_service.DEMO_MODE:
        data, fname = s3_service.demo_file_response(key)
        if data is None:
            abort(404)
        # as_attachment=False -> served inline
        _audit("view", record=rec)
        return send_file(io.BytesIO(data), download_name=fname, mimetype="text/plain")

    # Text files (transcripts, chat, notes, HTML…) are proxied THROUGH the app so
    # the preview's fetch() is same-origin and not blocked by S3 CORS. Media and
    # anything large is redirected straight to S3 (keeps big bytes off the server).
    if s3_service.is_text_preview(key):
        try:
            data, ctype = s3_service.get_object_bytes(key)
        except ValueError:
            # Too big to proxy — fall back to a direct inline S3 link.
            try:
                url = s3_service.presigned_url(key, inline=True)
                _audit("view", record=rec)
                return redirect(url)
            except Exception as e:
                return jsonify({"error": _s3_err(e)}), 502
        except Exception as e:
            return jsonify({"error": _s3_err(e)}), 502
        # content_type (not mimetype) so a charset already in ctype isn't doubled.
        resp = app.response_class(data, content_type=ctype)
        resp.headers["Content-Disposition"] = "inline"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        _audit("view", record=rec)
        return resp

    try:
        url = s3_service.presigned_url(key, inline=True)
        _audit("view", record=rec)
        return redirect(url)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


# Folder name -> what the caption track is actually called in the player. Zoom
# writes WebVTT into both TRANSCRIPT and CC folders.
_CAPTION_TRACK_LABELS = {"cc": "Closed captions", "transcript": "Transcript"}


@app.route("/api/captions")
@login_required
def api_captions():
    """Subtitle tracks available for one media file: the .vtt transcript / closed
    captions Zoom stored in the same meeting folder.

    Metadata only — every track is served through /api/view, which re-checks
    access on its own, and each key here is authorized again before it is even
    named, so this can never disclose a file the caller may not open."""
    key = request.args.get("key", "")
    access = _current_access()
    if _authorized(key, access) is None:
        abort(404, "File not found.")

    tracks = []
    for rec in s3_service.caption_records(key):
        if _authorized(rec["key"], access) is None:
            continue
        tracks.append({
            "label": _CAPTION_TRACK_LABELS.get(
                (rec.get("file_type") or "").strip().lower(), "Captions"),
            "filename": rec.get("filename", ""),
            "src": url_for("api_view_one", key=rec["key"]),
        })
    return jsonify({"tracks": tracks})


# How long a preflight's go-ahead stays valid. It only has to cover the moment
# between the page checking a selection and the browser starting the download.
_BULK_TOKEN_MAX_AGE = 300


def _bulk_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="bulk-zip-download")


def _bulk_keys_digest(submitted):
    """A stable fingerprint of a submitted key list, order and repeats ignored."""
    keys = sorted({k for k in (submitted or []) if isinstance(k, str)})
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _bulk_selection(submitted_keys, access):
    """Authorize a bulk selection: ``(records, None)`` when every selected file may
    be zipped by this caller, else ``(None, (response, status))`` saying why.

    Shared by the preflight and the download itself so the two can never
    disagree — and so the download re-checks everything rather than trusting a
    preflight that ran a moment earlier, when the grant may have been different."""
    records = []
    view_only = 0
    seen_keys = set()
    if isinstance(submitted_keys, list):
        for key in submitted_keys:
            if not isinstance(key, str) or key in seen_keys:
                continue
            rec = _authorized(key, access)
            if rec is None:
                continue
            seen_keys.add(rec["key"])
            # A selection may legitimately mix downloadable and view-only files
            # now that meetings can be shared view-only. Count them rather than
            # dropping them silently — a zip that is quietly short of what the
            # user selected is worse than a refusal that says why.
            if s3_service.record_downloadable(rec, access):
                records.append(rec)
            else:
                view_only += 1
    if view_only:
        return None, (jsonify({
            "error": f"{view_only} of the selected file(s) are view-only for your "
                     "account and cannot be zipped. Deselect them and try again."
        }), 403)
    if not records:
        return None, (jsonify({"error": "No files selected."}), 400)
    if len(records) > s3_service.BULK_ZIP_MAX_FILES:
        return None, (jsonify({
            "error": f"A ZIP can contain at most {s3_service.BULK_ZIP_MAX_FILES} files."
        }), 413)
    total_bytes = sum(int(rec.get("size") or 0) for rec in records)
    if total_bytes > s3_service.BULK_ZIP_MAX_BYTES:
        limit_gb = s3_service.BULK_ZIP_MAX_BYTES / (1024 ** 3)
        return None, (jsonify({
            "error": f"A ZIP can contain at most {limit_gb:g} GB of recordings."
        }), 413)
    return records, None


@app.route("/api/download/bulk/check", methods=["POST"])
@login_required
def api_download_bulk_check():
    """Preflight for a bulk zip: validate the selection and hand back a
    short-lived go-ahead token, WITHOUT building anything.

    The page needs this because the zip itself is now received by the browser's
    own download manager rather than by fetch() — which is what lets a
    multi-gigabyte archive stream to disk instead of being held in a tab's
    memory. The price is that a native download cannot show a friendly error, so
    every refusal ("view-only", "too many files") is surfaced here first, as JSON
    the page can display."""
    access = _current_access()
    if not _can_download_anything(access):
        return jsonify({"error": "Your account is view-only — downloads are disabled."}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    records, error = _bulk_selection(data.get("keys"), access)
    if error:
        return error
    token = _bulk_serializer().dumps({
        "u": session.get("user", ""),
        "k": _bulk_keys_digest(data.get("keys")),
    })
    return jsonify({
        "ok": True,
        "files": len(records),
        "total_size": sum(int(r.get("size") or 0) for r in records),
        "token": token,
    })


def _valid_bulk_token(token, submitted_keys):
    """Did this form post come from our own page, for this user and these keys?

    A plain form post is what lets the browser receive the zip natively, but a
    form is also exactly what another site can submit on a signed-in user's
    behalf. The JSON path never had that exposure (a cross-site page cannot send
    application/json without a CORS preflight), so the form path carries a
    signed, expiring token from the preflight — bound to the user and the exact
    selection — which another origin has no way to obtain."""
    try:
        payload = _bulk_serializer().loads(token or "", max_age=_BULK_TOKEN_MAX_AGE)
    except BadSignature:                  # includes SignatureExpired
        return False
    return (isinstance(payload, dict)
            and payload.get("u") == session.get("user", "")
            and payload.get("k") == _bulk_keys_digest(submitted_keys))


@app.route("/api/download/bulk", methods=["POST"])
@login_required
def api_download_bulk():
    """Stream a ZIP of the selected recordings.

    Accepts the selection as a JSON body (API callers) or as a form post carrying
    a preflight token (the Search page — see api_download_bulk_check). Either
    way the archive is STREAMED as it is assembled from S3: nothing is staged on
    the server's disk, and bytes start flowing at once.

    The old build-it-first approach copied the whole selection from S3 to a temp
    file before sending a byte. For 99 files / 6.6 GB that left the connection
    silent for minutes; Cloudflare, which fronts the portal, gave up on the idle
    origin and answered 502 with its own HTML page — which the Search page could
    only show as a bare "Could not build the zip."."""
    access = _current_access()
    if not _can_download_anything(access):
        return jsonify({"error": "Your account is view-only — downloads are disabled."}), 403
    if request.is_json:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
        submitted_keys = data.get("keys") or []
    else:
        try:
            submitted_keys = json.loads(request.form.get("keys") or "[]")
        except ValueError:
            submitted_keys = None
        if not _valid_bulk_token(request.form.get("token"), submitted_keys):
            return jsonify({
                "error": "This download link has expired. Select the files and "
                         "click Download again."
            }), 403
    records, error = _bulk_selection(submitted_keys, access)
    if error:
        return error

    # Pull the first chunk before committing to a 200. Once streaming starts the
    # status line has gone and a failure can only cut the download short, so the
    # likeliest failures — expired AWS credentials, a recording deleted since the
    # last index — are caught here while they can still be reported properly.
    stream = s3_service.stream_zip(records)
    try:
        first_chunk = next(stream)
    except StopIteration:
        first_chunk = b""
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502

    total_bytes = sum(int(rec.get("size") or 0) for rec in records)
    meeting_ids = sorted({r.get("meeting_id", "") for r in records if r.get("meeting_id")})
    candidates = sorted({r.get("candidate", "") for r in records if r.get("candidate")})
    hosts = sorted({r.get("host", "") for r in records if r.get("host")})
    departments = sorted({r.get("department", "") for r in records if r.get("department")})
    _audit(
        "bulk_download",
        candidate=candidates[0] if len(candidates) == 1 else ("Multiple" if candidates else ""),
        host=hosts[0] if len(hosts) == 1 else ("Multiple" if hosts else ""),
        meeting_id=meeting_ids[0] if len(meeting_ids) == 1 else ("Multiple" if meeting_ids else ""),
        department=departments[0] if len(departments) == 1 else ("Multiple" if departments else ""),
        details={
            "file_count": len(records),
            "total_size": total_bytes,
            "meeting_ids": meeting_ids[:50],
            "items": [{
                "candidate": r.get("candidate", ""),
                "host": r.get("host", ""),
                "meeting_id": r.get("meeting_id", ""),
                "recording_date": r.get("date", ""),
                "department": r.get("department", ""),
                "file_type": r.get("file_type", ""),
            } for r in records[:50]],
            "items_truncated": len(records) > 50,
        },
    )

    user = session.get("user", "")

    def body():
        yield first_chunk
        try:
            yield from stream
        except GeneratorExit:
            raise                        # the browser went away — nothing to report
        except Exception:
            # Too late for a status code. Log it, and let the connection drop so
            # the browser marks the download failed, rather than saving a zip with
            # no central directory as though it were complete.
            app.logger.exception("Bulk zip for %s failed part-way through", user)
            raise

    return Response(body(), mimetype="application/zip", headers={
        "Content-Disposition": 'attachment; filename="interview-recordings.zip"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        # Proxies buffer upstream responses by default, which for a stream defeats
        # the purpose. deploy/nginx.conf already turns it off; this makes it hold
        # for any nginx in front, whatever its config says.
        "X-Accel-Buffering": "no",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Admin API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_users_list():
    # hosts_by_department drives the admin host pickers (non-blocking: empty
    # lists while the index is still warming, filled on the next load).
    opts = s3_service.filter_options()
    users = auth.list_users(s3_service.all_departments())
    # Annotate each shared meeting with what it actually is, so the admin sees
    # "96355112813 · Akhilendra · Interview-Success" instead of a bare number
    # they have no way to check. One index pass covers every user at once.
    granted_ids = {m["meeting_id"] for u in users for m in u.get("meetings") or []}
    details = s3_service.meeting_details(granted_ids) if granted_ids else {}
    for user in users:
        for meeting in user.get("meetings") or []:
            meeting["detail"] = details.get(meeting["meeting_id"])
    return jsonify({
        "admins": sorted(auth.get_admins().keys(), key=str.lower),
        "users": users,
        "departments": s3_service.all_departments(),
        "hosts_by_department": opts.get("hosts_by_department", {}),
    })


@app.route("/api/admin/meetings", methods=["GET"])
@admin_required
def api_admin_meetings():
    """Find meetings to share individually, across every department.

    Admin-only and intentionally unscoped: an admin already sees the whole
    bucket, and choosing a meeting to hand to someone is precisely the moment
    they need to look outside a single department. Returns one row per meeting id
    with enough context (candidates, dates, department, file count) that the
    admin can tell two similar sessions apart — and an `occurrences` count, since
    a recurring Zoom id covers every session that reused it."""
    query = request.args.get("q", "")
    limit = max(1, min(_int_arg("limit", s3_service.MEETING_LOOKUP_LIMIT), 50))
    try:
        meetings = s3_service.meeting_summaries(query, limit=limit)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502
    return jsonify({"meetings": meetings, "ready": s3_service.is_ready()})


@app.route("/api/admin/meetings/lookup", methods=["POST"])
@admin_required
def api_admin_meetings_lookup():
    """Resolve a PASTED LIST of meeting ids in one shot, for the admin picker.

    Separate from the free-text search above because the two answer different
    questions, and conflating them would hand over recordings nobody asked to
    share. The search is a SUBSTRING match ("akhilendra", "2026-07", "9635") over
    the whole index; this matches each id EXACTLY. A pasted "9635" must come back
    as one unresolved id, never as every 9635xxxxxxx meeting in the bucket.

    POST rather than GET because the payload is a list: gunicorn caps a request
    line at 4094 bytes by default, which a few hundred pasted ids blow straight
    through — and the browser would surface that as a bare 414 with nothing
    useful in it. /api/download/bulk posts its key list for the same reason.

    Every id the admin pasted comes back in exactly one of `meetings` (resolved),
    `missing` (well-formed but not in the current index — still grantable, since
    a recording uploaded minutes ago has not been indexed yet) or `invalid` (could
    never name a meeting folder). Nothing is dropped silently."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    ids, invalid, truncated = _split_meeting_ids(data.get("ids"))
    try:
        details = s3_service.meeting_details(ids) if ids else {}
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502
    # Order follows the paste, so the panel reads back in the order it was typed
    # and the admin can check it against whatever they copied from.
    return jsonify({
        "meetings":  [details[mid] for mid in ids if mid in details],
        "missing":   [mid for mid in ids if mid not in details],
        "invalid":   invalid,
        "truncated": truncated,
        "limit":     _MEETING_GRANT_MAX,
        "ready":     s3_service.is_ready(),
    })


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_users_create():
    data = request.get_json(silent=True)
    if request.is_json and not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    if data is None:
        data = {}
    try:
        departments = _clean_departments(data.get("departments"))
    except _BadDepartments as e:
        return jsonify({"error": str(e)}), 400
    try:
        meetings = _validated_meetings(data.get("meetings")) or []
        auth.create_user(
            data.get("username", ""), data.get("password", ""),
            created_by=session.get("user", ""),
            departments=departments,
            hosts=_clean_hosts(data.get("hosts"), departments),
            can_download=bool(data.get("can_download", False)),
            meetings=meetings,
        )
        _audit("user_create", details={
            "target_user": str(data.get("username", "")),
            "departments": departments,
            "can_download": bool(data.get("can_download", False)),
            "meetings": [m["meeting_id"] for m in meetings],
            "meetings_downloadable": [m["meeting_id"] for m in meetings if m["can_download"]],
            # Scalars as well as the lists: audit_service truncates a long list at
            # 50 items, and 50 is exactly the boundary a pasted batch crosses — so
            # without these a big grant loses the record of HOW MANY were shared
            # on top of which ones.
            "meetings_count": len(meetings),
            "meetings_downloadable_count": sum(1 for m in meetings if m["can_download"]),
        })
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/admin/users/<username>", methods=["PATCH"])
@admin_required
def api_users_update(username):
    """Update an existing user's departments, host restriction and/or download
    permission."""
    data = request.get_json(silent=True)
    if request.is_json and not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    if data is None:
        data = {}
    try:
        departments = _clean_departments(data["departments"]) if "departments" in data else None
    except _BadDepartments as e:
        return jsonify({"error": str(e)}), 400
    hosts = None
    if "hosts" in data:
        # Validate against the departments being set now, or the user's current
        # grant when only the hosts are changing.
        target = departments if departments is not None \
            else auth.user_access(username, s3_service.all_departments())["departments"]
        hosts = _clean_hosts(data.get("hosts"), target)
    can_download = bool(data["can_download"]) if "can_download" in data else None
    try:
        meetings = _validated_meetings(data["meetings"]) if "meetings" in data else None
    except _BadMeetings as e:
        # 400, not the 404 the ValueError below maps to: the user exists, the
        # payload is what cannot be stored.
        return jsonify({"error": str(e)}), 400
    try:
        auth.update_user_access(username, departments=departments, hosts=hosts,
                                can_download=can_download, meetings=meetings)
        _audit("user_update", details={
            "target_user": username,
            "departments": departments,
            "can_download": can_download,
            "meetings": None if meetings is None else [m["meeting_id"] for m in meetings],
            "meetings_downloadable": None if meetings is None else
                [m["meeting_id"] for m in meetings if m["can_download"]],
            # See api_users_create: the lists get truncated at 50, the counts don't.
            "meetings_count": None if meetings is None else len(meetings),
            "meetings_downloadable_count": None if meetings is None else
                sum(1 for m in meetings if m["can_download"]),
        })
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/admin/users/<username>", methods=["DELETE"])
@admin_required
def api_users_delete(username):
    if not auth.delete_user(username):
        return jsonify({"error": "No such user."}), 404
    _audit("user_delete", details={"target_user": username})
    return jsonify({"ok": True})


@app.route("/api/admin/logs", methods=["GET"])
@admin_required
def api_audit_logs():
    try:
        page = max(1, _int_arg("page", 1))
        per_page = max(1, min(_int_arg("per_page", 50), 200))
        response = jsonify(audit_service.list_events(
            page=page,
            per_page=per_page,
            action=request.args.get("action", ""),
            username=request.args.get("username", ""),
            q=request.args.get("q", ""),
            date_from=request.args.get("date_from", ""),
            date_to=request.args.get("date_to", ""),
        ))
        response.headers["Cache-Control"] = "no-store"
        return response
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        app.logger.exception("Could not read the audit log")
        return jsonify({"error": "Could not read audit logs."}), 500


@app.route("/api/admin/logs", methods=["DELETE"])
@admin_required
def api_audit_logs_clear():
    """Permanently delete every audit event. The clear is itself audited, so the
    freshly emptied trail immediately shows who cleared it and how many rows went."""
    try:
        deleted = audit_service.clear_events()
    except Exception:
        app.logger.exception("Could not clear the audit log")
        return jsonify({"error": "Could not clear audit logs."}), 500
    _audit("logs_cleared", details={"deleted_events": deleted})
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/admin/logs/<int:event_id>", methods=["DELETE"])
@admin_required
def api_audit_log_delete(event_id):
    """Delete a single audit entry. Unlike the full clear, an individual removal
    is not itself audited — otherwise deleting one row would only replace it with
    another and the count could never drop."""
    try:
        removed = audit_service.delete_event(event_id)
    except Exception:
        app.logger.exception("Could not delete the audit log entry")
        return jsonify({"error": "Could not delete the log entry."}), 500
    if not removed:
        return jsonify({"error": "Log entry not found."}), 404
    return jsonify({"ok": True})


# Client-detected capture signals the browser is allowed to report. A browser
# CANNOT truly detect or block OS screenshots / screen recording — these are
# best-effort deterrents (PrintScreen key, focus-loss while a preview is open).
_CLIENT_CAPTURE_ACTIONS = {"screenshot", "screen_capture_suspected"}


@app.route("/api/log/capture", methods=["POST"])
@login_required
def api_log_capture():
    """Record a client-reported screen-capture signal. Username/role come from the
    session (trusted); only the signal kind + method are client-supplied and
    allow-listed. Admins are exempt (the client script also skips them)."""
    if session.get("role") == "admin":
        return jsonify({"ok": True, "skipped": "admin"})
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "")
    if kind not in _CLIENT_CAPTURE_ACTIONS:
        return jsonify({"error": "Unknown capture signal."}), 400

    details = {"client_reported": True}
    method = data.get("method")
    if isinstance(method, str) and method.strip():
        details["method"] = method.strip()[:64]

    rec, extra = None, {}
    key = data.get("key")
    if isinstance(key, str) and key:
        access = _current_access()
        if _authorized(key, access) is not None:
            rec = s3_service.record_for_key(key)
            extra["resource_key"] = key            # per-recording dedupe
    _audit(kind, record=rec, details=details, dedupe_seconds=10, **extra)
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
def _s3_err(e) -> str:
    """Turn an S3/boto failure into something the person reading it can act on.
    Accepts an exception or an already-stringified message (cache_info stores the
    latter), since both funnel to the same user-facing text."""
    msg = str(e)
    if any(t in msg for t in ("ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "RequestExpired", "token has expired")):
        return "AWS session credentials have expired. Refresh the STS/IAM credentials (or restart the service) and try again."
    if "AccessDenied" in msg:
        return "S3 access denied — check the EC2 IAM role / credentials and bucket policy."
    if "NoSuchBucket" in msg:
        return f"Bucket '{s3_service.BUCKET}' not found in region '{s3_service.REGION}'."
    if "Unable to locate credentials" in msg or "NoCredentialsError" in msg:
        return "No AWS credentials found. Attach an IAM role on EC2, or set keys / DEMO_MODE locally."
    return f"S3 error: {msg}"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
