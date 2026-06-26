"""
app.py — Flask entry point for the Interview-Success recording portal.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env        # then edit ADMIN_USERS etc.
    python app.py               # http://localhost:8000
"""

import os
import functools

from dotenv import load_dotenv
load_dotenv()  # must run before importing modules that read os.environ at import time

from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, send_file, after_this_request, abort,
)

import auth
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


# ─────────────────────────────────────────────────────────────────────────────
# Auth API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = auth.verify(username, password)
    if not role:
        return jsonify({"error": "Wrong username or password."}), 401
    session["user"] = username
    session["role"] = role
    return jsonify({"ok": True, "username": username, "role": role})


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ─────────────────────────────────────────────────────────────────────────────
# Search API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/filters")
@login_required
def api_filters():
    try:
        opts = s3_service.filter_options()
        opts["cache"] = s3_service.cache_info()
        return jsonify(opts)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


@app.route("/api/search")
@login_required
def api_search():
    try:
        results, total, total_size = s3_service.search(
            candidate=request.args.get("candidate", ""),
            company=request.args.get("company", ""),
            date=request.args.get("date", ""),
            meeting_id=request.args.get("meeting_id", ""),
            file_type=request.args.get("file_type", ""),
            host=request.args.get("host", ""),
        )
        return jsonify({
            "count": len(results),
            "total": total,
            "truncated": total > len(results),
            "total_size": total_size,
            "results": results,
        })
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    try:
        s3_service.get_records(force=True)
        return jsonify({"ok": True, "cache": s3_service.cache_info()})
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


# ─────────────────────────────────────────────────────────────────────────────
# Download API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/download")
@login_required
def api_download_one():
    key = request.args.get("key", "")
    if not key or not key.startswith(s3_service.ROOT_PREFIX):
        abort(400, "Invalid key.")
    if not s3_service._key_exists(key):
        abort(404, "File not found.")

    if s3_service.DEMO_MODE:
        data, fname = s3_service.demo_file_response(key)
        if data is None:
            abort(404)
        import io
        return send_file(io.BytesIO(data), as_attachment=True,
                         download_name=fname, mimetype="text/plain")

    try:
        return redirect(s3_service.presigned_url(key))
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502


@app.route("/api/download/bulk", methods=["POST"])
@login_required
def api_download_bulk():
    data = request.get_json(silent=True) or {}
    keys = data.get("keys") or []
    keys = [k for k in keys if isinstance(k, str) and k.startswith(s3_service.ROOT_PREFIX)]
    if not keys:
        return jsonify({"error": "No files selected."}), 400

    try:
        zip_path = s3_service.build_zip(keys)
    except Exception as e:
        return jsonify({"error": _s3_err(e)}), 502

    @after_this_request
    def _cleanup(resp):
        try:
            os.unlink(zip_path)
        except OSError:
            pass
        return resp

    return send_file(
        zip_path,
        as_attachment=True,
        download_name="interview-recordings.zip",
        mimetype="application/zip",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Admin API
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_users_list():
    return jsonify({
        "admins": sorted(auth.get_admins().keys(), key=str.lower),
        "users": auth.list_users(),
    })


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_users_create():
    data = request.get_json(silent=True) or {}
    try:
        auth.create_user(data.get("username", ""), data.get("password", ""),
                         created_by=session.get("user", ""))
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/admin/users/<username>", methods=["DELETE"])
@admin_required
def api_users_delete(username):
    auth.delete_user(username)
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
