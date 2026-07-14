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
import functools

from dotenv import load_dotenv
load_dotenv()  # must run before importing modules that read os.environ at import time

from werkzeug.wsgi import ClosingIterator

from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, send_file, abort,
)

import auth
import audit_service
import s3_service

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # request bodies are tiny (key lists)


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
    """What the signed-in user may reach: the list of departments they can browse,
    an optional per-department host restriction ({dept: [host, …]} — no entry
    means every host in that department) and whether they may download (vs
    view-only). Admins get every department, every host and full download rights."""
    if session.get("role") == "admin":
        return {"departments": list(s3_service.DEPARTMENTS), "hosts": {}, "can_download": True}
    info = auth.user_access(session.get("user", ""))
    # Intersect with the departments that actually exist, so a stale grant can't
    # widen access if a department is renamed/removed in config.
    depts = [d for d in info["departments"] if d in s3_service.DEPARTMENTS]
    hosts = {d: hs for d, hs in (info.get("hosts") or {}).items() if d in depts and hs}
    return {"departments": depts, "hosts": hosts, "can_download": bool(info["can_download"])}


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


def _clean_departments(raw):
    """Keep only known department names from an admin-supplied list (drops typos /
    anything not in DEPARTMENTS)."""
    if not isinstance(raw, list):
        return []
    valid = set(s3_service.DEPARTMENTS)
    seen, out = set(), []
    for d in raw:
        if d in valid and d not in seen:
            seen.add(d)
            out.append(d)
    return out


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
                                         allowed_hosts=access["hosts"])
        # Show the user's full assigned set (even a department with no files yet),
        # not just the ones that happen to have records.
        opts["departments"] = sorted(access["departments"], key=str.lower)
        hbd = opts.get("hosts_by_department", {})
        for d in access["departments"]:
            hbd.setdefault(d, [])          # a department with no files yet -> no hosts
        opts["hosts_by_department"] = hbd
        opts["can_download"] = access["can_download"]
        opts["cache"] = s3_service.cache_info()
        return jsonify(opts)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


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
            "meeting_id": request.args.get("meeting_id", ""),
            "file_type": request.args.get("file_type", ""),
            "host": request.args.get("host", ""),
            "department": request.args.get("department", ""),
        }
        sort = request.args.get("sort", "")
        results, total, total_size = s3_service.search(
            **filters,
            allowed_departments=access["departments"],
            allowed_hosts=access["hosts"],
            limit=per_page,
            offset=(page - 1) * per_page,
            sort=sort,
        )
        if any(str(v or "").strip() for v in filters.values()):
            _audit(
                "search",
                candidate=filters["candidate"],
                host=filters["host"],
                meeting_id=filters["meeting_id"],
                recording_date=filters["date"],
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
            "can_download": access["can_download"],
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
    if not access["can_download"]:
        abort(403, "Your account is view-only — downloads are disabled.")
    rec = s3_service.authorized_record(key, access["departments"], access["hosts"])
    if rec is None:
        abort(404, "File not found.")

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
    rec = s3_service.authorized_record(key, access["departments"], access["hosts"])
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


@app.route("/api/download/bulk", methods=["POST"])
@login_required
def api_download_bulk():
    access = _current_access()
    if not access["can_download"]:
        return jsonify({"error": "Your account is view-only — downloads are disabled."}), 403
    data = request.get_json(silent=True)
    if request.is_json and not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    if data is None:
        data = {}
    submitted_keys = data.get("keys") or []
    records = []
    seen_keys = set()
    if isinstance(submitted_keys, list):
        for key in submitted_keys:
            if not isinstance(key, str) or key in seen_keys:
                continue
            rec = s3_service.authorized_record(key, access["departments"], access["hosts"])
            if rec is not None:
                seen_keys.add(rec["key"])
                records.append(rec)
    keys = [rec["key"] for rec in records]
    if not keys:
        return jsonify({"error": "No files selected."}), 400
    if len(records) > s3_service.BULK_ZIP_MAX_FILES:
        return jsonify({
            "error": f"A ZIP can contain at most {s3_service.BULK_ZIP_MAX_FILES} files."
        }), 413
    total_bytes = sum(int(rec.get("size") or 0) for rec in records)
    if total_bytes > s3_service.BULK_ZIP_MAX_BYTES:
        limit_gb = s3_service.BULK_ZIP_MAX_BYTES / (1024 ** 3)
        return jsonify({
            "error": f"A ZIP can contain at most {limit_gb:g} GB of recordings."
        }), 413

    try:
        zip_path = s3_service.build_zip(keys)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502

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

    response = send_file(
        zip_path,
        as_attachment=True,
        download_name="interview-recordings.zip",
        mimetype="application/zip",
    )

    def _cleanup():
        try:
            os.unlink(zip_path)
        except OSError:
            pass

    # Tie cleanup to the file iterable itself. This runs after the file handle is
    # closed (including on Windows, where unlinking an open ZIP would fail).
    response.response = ClosingIterator(response.response, [_cleanup])
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Admin API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_users_list():
    # hosts_by_department drives the admin host pickers (non-blocking: empty
    # lists while the index is still warming, filled on the next load).
    opts = s3_service.filter_options()
    return jsonify({
        "admins": sorted(auth.get_admins().keys(), key=str.lower),
        "users": auth.list_users(),
        "departments": list(s3_service.DEPARTMENTS),
        "hosts_by_department": opts.get("hosts_by_department", {}),
    })


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_users_create():
    data = request.get_json(silent=True)
    if request.is_json and not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    if data is None:
        data = {}
    departments = _clean_departments(data.get("departments"))
    try:
        auth.create_user(
            data.get("username", ""), data.get("password", ""),
            created_by=session.get("user", ""),
            departments=departments,
            hosts=_clean_hosts(data.get("hosts"), departments),
            can_download=bool(data.get("can_download", False)),
        )
        _audit("user_create", details={
            "target_user": str(data.get("username", "")),
            "departments": departments,
            "can_download": bool(data.get("can_download", False)),
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
    departments = _clean_departments(data.get("departments")) if "departments" in data else None
    hosts = None
    if "hosts" in data:
        # Validate against the departments being set now, or the user's current
        # grant when only the hosts are changing.
        target = departments if departments is not None \
            else auth.user_access(username)["departments"]
        hosts = _clean_hosts(data.get("hosts"), target)
    can_download = bool(data["can_download"]) if "can_download" in data else None
    try:
        auth.update_user_access(username, departments=departments, hosts=hosts,
                                can_download=can_download)
        _audit("user_update", details={
            "target_user": username,
            "departments": departments,
            "can_download": can_download,
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


# ─────────────────────────────────────────────────────────────────────────────
def _s3_err(e: Exception) -> str:
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
