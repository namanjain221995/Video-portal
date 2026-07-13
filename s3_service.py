"""
s3_service.py
-------------
All S3 access for the Interview-Success recording portal lives here.

The bucket has several department folders at the top level (HR, Interview-Success,
Marketing, …). The FIRST path segment is the department; below it there are THREE
key layouts (all handled by _parse_key):

    Layout A (10 segments below the department) — Interview-Success:
        {Dept}/{Host}/{Year}/{Month}/{Candidate}/{Company}/{Date}/{Round}/{MeetingID}/{FileType}/{file}
    Layout B (11 segments — extra MeetingID + a Time-*-IST folder) — Interview-Success:
        {Dept}/{Host}/{Year}/{Month}/{Candidate}/{MeetingID}/{Company}/{Date}/{Round}/{Time-*-IST}/{FileType}/{file}
    Layout C (9 segments — NO Company/Round, has a Time-*-IST folder) — HR/Marketing/Training/…:
        {Dept}/{Host}/{Year}/{Month}/{Candidate}/{Date}/{Time-*-IST}/{MeetingID}/{FileType}/{file}

Reliable anchors in ALL layouts:  department = seg[0], host/year/month/candidate =
the next four, file_type = seg[-2], filename = seg[-1]; the date matches YYYY-MM-DD,
the meeting id is the all-digit segment, company is the label just before the date
and round the one just after (absent in layout C, which has neither).

The module lists everything under Interview-Success/, parses each key into a
structured record, caches the result (TTL + a shared on-disk index so the 3
gunicorn workers don't each re-scan S3), and exposes search / filter / download
helpers on top of that cache.

Set DEMO_MODE=true in .env to run the whole UI locally with bundled sample
data and no AWS account at all.
"""

import io
import os
import re
import sys
import json
import time
import zipfile
import tempfile
import threading
import mimetypes

import boto3
from botocore.config import Config

# ─────────────────────────────────────────────────────────────────────────────
# Config (all overridable via .env)
# ─────────────────────────────────────────────────────────────────────────────
BUCKET       = os.environ.get("S3_BUCKET_NAME", "zoom-automation-bucket")
ROOT_PREFIX  = os.environ.get("ROOT_PREFIX", "Interview-Success/")
REGION       = os.environ.get("AWS_REGION", "us-east-1")
# Top-level "department" folders in the bucket. Each holds the same internal
# layout ({Host}/{Year}/{Month}/{Candidate}/…). The portal scans every one of
# these and tags each record with its department; access is then granted per
# user by an admin. Override via DEPARTMENTS="HR,Marketing,…" in .env.
DEPARTMENTS  = [d.strip() for d in os.environ.get(
    "DEPARTMENTS",
    "HR,Interview-Success,Marketing,Training,Customer-Success,Techsphere,Executive-Assistant,"
    "QMS,Other,CEO,COO,Business-Development,Advanced-Training",
).split(",") if d.strip()]
CACHE_TTL    = int(os.environ.get("CACHE_TTL_SEC", "300"))
URL_EXPIRY   = int(os.environ.get("PRESIGNED_URL_EXPIRY_SEC", "3600"))
DEMO_MODE    = os.environ.get("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes")
# Max rows returned by a single search (keeps the JSON payload + browser table
# bounded — a broad filter could otherwise match >10k files).
RESULT_LIMIT = int(os.environ.get("SEARCH_RESULT_LIMIT", "500"))
# Optional shared on-disk index (set to a path on a persistent volume, e.g.
# /data/index.json in Docker). When set, workers load the parsed index from this
# file instead of each re-listing the whole bucket, and it survives restarts.
INDEX_FILE   = os.environ.get("INDEX_FILE", "").strip()
# How often the bucket is actually re-listed from S3 (the expensive operation).
# Decoupled from CACHE_TTL: workers reload the cheap disk index every CACHE_TTL,
# but S3 is only re-scanned when the shared index is older than INDEX_TTL (or via
# the manual "Refresh index" button). Recordings change slowly, so this is generous.
INDEX_TTL    = int(os.environ.get("INDEX_REFRESH_SEC", "1800"))
# How long a single full bucket scan may take before peers assume the scanning
# worker died. Must comfortably exceed a real scan (≈3 min for ~80k files across
# all departments), otherwise losers steal the lock / scan themselves and every
# worker re-lists S3 at once on a cold boot. Also caps the loser wait.
SCAN_TIMEOUT = int(os.environ.get("SCAN_TIMEOUT_SEC", "900"))

if not ROOT_PREFIX.endswith("/"):
    ROOT_PREFIX += "/"

# Number of path segments produced by ROOT_PREFIX itself, e.g. "Interview-Success/" -> 1
_ROOT_DEPTH = len([p for p in ROOT_PREFIX.split("/") if p])

_lock = threading.Lock()          # guards _cache reads/writes (fast)
_scan_lock = threading.Lock()     # serialises (re)builds so we never scan twice at once
_cache = {"records": None, "by_key": None, "options": None, "ts": 0.0}
_s3 = None


# ─────────────────────────────────────────────────────────────────────────────
# File-type categories (single source of truth — the frontend mirrors the labels)
# ─────────────────────────────────────────────────────────────────────────────
# Primary signal is the (now reliably parsed) raw folder name; extension is the
# fallback. This guarantees the Time-*-IST folders can never appear as a type.
_RAW_TO_CATEGORY = {
    "mp4": "video", "m4a": "audio",
    "transcript": "transcript", "cc": "transcript",   # CC = closed captions, same family
    "chat": "chat", "questions": "questions",
    "llm": "summary", "docs": "notes",
}
_EXT_TO_CATEGORY = {
    "mp4": "video", "m4a": "audio", "vtt": "transcript",
    "txt": "notes", "html": "questions",
}
# Insertion order == dropdown order.
CATEGORY_LABELS = {
    "video":      "Video (.mp4)",
    "audio":      "Audio (.m4a)",
    "transcript": "Transcript (.vtt)",
    "chat":       "Chat (.txt)",
    "questions":  "Questions (.html)",
    "summary":    "AI summary (.txt)",
    "notes":      "Notes (.txt)",
    "other":      "Other",
}


def _categorize(file_type_raw: str, ext: str) -> str:
    return (_RAW_TO_CATEGORY.get((file_type_raw or "").strip().lower())
            or _EXT_TO_CATEGORY.get((ext or "").strip().lower(), "other"))


def _client():
    """Lazily build a boto3 S3 client. Uses the EC2 instance role / env creds /
    ~/.aws automatically — we never put keys in code."""
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=REGION,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )
    return _s3


# ─────────────────────────────────────────────────────────────────────────────
# Key parsing (layout-aware — see module docstring)
# ─────────────────────────────────────────────────────────────────────────────
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Some departments insert a "Time-8-30-PM-IST" folder where Interview-Success has
# a Round. It must never be read as a company or round.
_TIME_RE = re.compile(r"^time-.*ist$", re.I)


def _is_time(s: str) -> bool:
    return bool(_TIME_RE.match(s or ""))

# Group sessions (e.g. Advanced-Training) put EVERY attendee in the candidate
# folder, hyphen-joined, often behind a numeric id prefix:
#     700758249_Shafahad_Mohammed-Abdu_Raziq-Nandini_K-Ram_Reddy-…
# Underscores stay INSIDE a person's name; hyphens separate people. A leading
# "digits(-digits)*_" chunk (meeting/employee id) is stripped before splitting.
_ID_PREFIX_RE = re.compile(r"^\d[\d\-]*_")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")


def _split_candidates(candidate: str) -> list:
    """The individual people inside a candidate folder name. A normal 1-person
    folder yields a single cleaned name; a hyphen-joined group yields one entry
    per attendee (deduped, order kept). Falls back to the raw string when the
    folder holds no recognisable name at all."""
    base = _ID_PREFIX_RE.sub("", (candidate or "").strip())
    seen, out = set(), []
    for part in base.split("-"):
        part = part.strip("_ ")
        if not part or not _HAS_LETTER_RE.search(part):
            continue  # empty / leftover pure-numeric id fragment
        k = part.lower()
        if k not in seen:
            seen.add(k)
            out.append(part)
    return out or [candidate]

# Low-cardinality fields are interned so the 50k records don't hold 50k copies of
# the same ~21 hosts / ~8 file-types / handful of dates — a big per-worker RAM win.
_INTERN_FIELDS = ("department", "host", "year", "month", "company", "date", "round",
                  "file_type", "category", "ext")


def _intern_rec(d: dict) -> dict:
    for k in _INTERN_FIELDS:
        v = d.get(k)
        if isinstance(v, str):
            d[k] = sys.intern(v)
    # Attendee names repeat across the ~10 files of the same meeting — intern them
    # too so a group session doesn't hold N copies of every name per file. Records
    # loaded from a pre-upgrade disk index have no "candidates" yet — backfill it
    # here (every record, parsed or disk-loaded, passes through this function).
    cands = d.get("candidates")
    if not isinstance(cands, list):
        cands = _split_candidates(d.get("candidate", ""))
    d["candidates"] = [sys.intern(c) for c in cands if isinstance(c, str)]
    return d


def _parse_key(key: str, size):
    """Turn an S3 key into a structured record, or None if it is not a leaf file
    under the expected {Department}/{Host}/… layout (folder placeholders, short keys).

    The first path segment is the department (HR, Interview-Success, …); the rest
    is the per-department layout the parser already understood."""
    parts = key.split("/")
    department = parts[0]
    seg = parts[1:]                      # everything below the department folder
    if len(seg) < 9:
        return None

    filename = seg[-1].strip()
    if not filename:
        return None  # folder placeholder / trailing slash

    host, year, month, candidate = seg[0], seg[1], seg[2], seg[3]
    file_type = seg[-2]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # Everything between the candidate and the file_type folder, across layouts:
    #   Interview-Success A (10 seg): [Company, Date, Round, MeetingID]
    #   Interview-Success B (11 seg): [MeetingID, Company, Date, Round, Time-*-IST]
    #   Other depts        C ( 9 seg): [Date, Time-*-IST, MeetingID]  (no Company/Round)
    # Anchor on the date: company sits just before it, round just after — but only
    # if that neighbour is a real label (not the meeting id and not a Time-*-IST
    # folder), so layout C correctly yields empty company/round.
    mid = seg[4:-2]
    company = date = rnd = ""
    di = next((i for i, s in enumerate(mid) if _DATE_RE.match(s)), None)
    if di is not None:
        date = mid[di]
        prev = mid[di - 1] if di - 1 >= 0 else ""
        nxt  = mid[di + 1] if di + 1 < len(mid) else ""
        if prev and not prev.isdigit() and not _is_time(prev):
            company = prev
        if nxt and not nxt.isdigit() and not _is_time(nxt):
            rnd = nxt
    meeting_id = next((s for s in mid if s.isdigit()), "")

    return _intern_rec({
        "department": department,
        "host":       host,
        "year":       year,
        "month":      month,
        "candidate":  candidate,
        "candidates": _split_candidates(candidate),   # people in the meeting (1+)
        "company":    company,
        "date":       date,
        "round":      rnd,
        "meeting_id": meeting_id,
        "file_type":  file_type,                       # corrected raw folder (MP4/CC/docs…)
        "category":   _categorize(file_type, ext),     # canonical key for the type filter
        "filename":   filename,
        "ext":        ext,
        "key":        key,
        "size":       int(size or 0),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Listing + cache (in-process TTL cache backed by a shared on-disk index)
# ─────────────────────────────────────────────────────────────────────────────
def _scan_s3():
    client = _client()
    records = []
    paginator = client.get_paginator("list_objects_v2")
    # List each department folder separately so an unrelated top-level prefix in
    # the bucket can never leak into the index.
    for dept in DEPARTMENTS:
        for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{dept}/"):
            for obj in page.get("Contents", []):
                rec = _parse_key(obj["Key"], obj.get("Size", 0))
                if rec:
                    records.append(rec)
    return records


# Bump whenever the record shape or DEPARTMENTS coverage changes: a persisted
# index with an older schema is rejected, forcing ONE clean re-scan on the first
# boot after a deploy instead of serving pre-upgrade records for up to INDEX_TTL.
INDEX_SCHEMA = 2


def _load_disk_index(max_age):
    """Return the parsed index from INDEX_FILE if it exists, is younger than
    max_age seconds and matches the current schema, else None."""
    if not INDEX_FILE or not os.path.exists(INDEX_FILE):
        return None
    try:
        if (time.time() - os.path.getmtime(INDEX_FILE)) >= max_age:
            return None
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema") != INDEX_SCHEMA:
            return None  # pre-upgrade index — rebuild from S3
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None
        return [_intern_rec(r) for r in records]
    except (OSError, ValueError):
        return None


# ── Cross-process election so only ONE worker ever lists S3 at a time ─────────
def _lock_path():
    return (INDEX_FILE + ".lock") if INDEX_FILE else None


def _acquire_scan_lock(stale_sec=SCAN_TIMEOUT):
    """Atomically claim the right to re-list S3. Returns True if THIS process won.
    A lock older than stale_sec is assumed orphaned (worker died mid-scan) and stolen."""
    path = _lock_path()
    if not path:
        return True  # no shared file -> single-process semantics, just scan
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > stale_sec:
                os.unlink(path)
                return _acquire_scan_lock(stale_sec)
        except OSError:
            pass
        return False
    except OSError:
        return True  # cannot use a lock file (perms, etc.) -> fall back to scanning


def _release_scan_lock():
    path = _lock_path()
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _rebuild_from_s3(force):
    """(Re)list S3, but coordinate across workers: the election winner scans and
    publishes the shared index; the losers wait for it and load from disk."""
    if _acquire_scan_lock():
        try:
            if not force:  # a peer may have just published a fresh index
                recs = _load_disk_index(INDEX_TTL)
                if recs is not None:
                    return recs
            recs = _scan_s3()
            _save_disk_index(recs)
            return recs
        finally:
            _release_scan_lock()
    # Lost the election: wait for the winner to publish, then load it.
    deadline = time.time() + SCAN_TIMEOUT
    while time.time() < deadline:
        time.sleep(1.0)
        recs = _load_disk_index(INDEX_TTL)
        if recs is not None:
            return recs
    # Winner is overdue / died — scan ourselves rather than serve nothing.
    recs = _scan_s3()
    _save_disk_index(recs)
    return recs


def _store(records):
    options = _build_options(records)
    by_key = {r["key"]: r for r in records}
    with _lock:
        _cache["records"] = records
        _cache["by_key"] = by_key
        _cache["options"] = options
        _cache["ts"] = time.time()


def _save_disk_index(records):
    if not INDEX_FILE:
        return
    try:
        tmp = INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": INDEX_SCHEMA, "records": records, "ts": time.time()}, f)
        os.replace(tmp, INDEX_FILE)
    except OSError:
        pass  # disk cache is an optimisation; never fatal


def _build_options(records):
    """Distinct values for the (small) server-side dropdowns. Company is now a
    free-text input and file-type is a static category list, so hosts +
    departments remain."""
    return {
        "hosts":       sorted({r["host"] for r in records}, key=str.lower),
        "departments": sorted({r["department"] for r in records}, key=str.lower),
    }


def get_records(force: bool = False):
    """Return all parsed records. Served from an in-process TTL cache, which is
    populated from the shared on-disk index when possible (so only one worker
    ever has to actually re-list the bucket) and only falls back to S3 otherwise."""
    if DEMO_MODE:
        return DEMO_RECORDS

    now = time.time()
    with _lock:
        if _cache["records"] is not None and (now - _cache["ts"]) < CACHE_TTL and not force:
            return _cache["records"]

    # Serialise (re)builds within this process so concurrent threads don't all rebuild.
    with _scan_lock:
        with _lock:
            if _cache["records"] is not None and (time.time() - _cache["ts"]) < CACHE_TTL and not force:
                return _cache["records"]

        # Prefer the cheap shared disk index; only re-list S3 when it is stale
        # (older than INDEX_TTL) or an explicit refresh was requested.
        records = None if force else _load_disk_index(INDEX_TTL)
        if records is None:
            records = _rebuild_from_s3(force)

        _store(records)
        return records


def is_ready() -> bool:
    if DEMO_MODE:
        return True
    with _lock:
        return _cache["records"] is not None


def cache_info():
    if DEMO_MODE:
        return {"demo": True, "count": len(DEMO_RECORDS), "age_sec": 0, "ready": True}
    with _lock:
        ready = _cache["records"] is not None
        age = time.time() - _cache["ts"] if ready else None
        count = len(_cache["records"]) if ready else 0
    return {"demo": False, "count": count,
            "age_sec": round(age) if age is not None else None, "ready": ready}


def _records_by_key():
    if DEMO_MODE:
        return {r["key"]: r for r in DEMO_RECORDS}
    get_records()  # ensure cache is fresh (handles TTL/cold start)
    with _lock:
        return _cache["by_key"] or {}


# ─────────────────────────────────────────────────────────────────────────────
# Search + filters
# ─────────────────────────────────────────────────────────────────────────────
def _cand_tokens(query: str) -> list:
    """Normalised candidate-search tokens: lowercase, underscores/hyphens read
    as spaces, so 'sirikonda' or 'akhilendra sirikonda' both hit
    'Akhilendra_NA_Sirikonda'."""
    return (query or "").lower().replace("_", " ").replace("-", " ").split()


def _name_matches(toks, name: str) -> bool:
    n = (name or "").lower().replace("_", " ").replace("-", " ")
    return all(t in n for t in toks)


def _match_candidate(toks, rec: dict) -> bool:
    """True when the query names someone in this recording. In a group session
    (several attendees in one candidate folder) EVERY token must land inside ONE
    attendee's name — so 'mohammed reddy' can't match a meeting where Mohammed
    and Reddy are different people. A query carrying a number (a pasted id like
    '700758249') falls back to the raw folder name, whose numeric prefix is not
    an attendee. Single-candidate records keep the old whole-string behaviour
    (an id prefix like '152026_' stays searchable)."""
    if not toks:
        return True
    cands = rec.get("candidates") or [rec["candidate"]]
    if len(cands) > 1:
        if any(_name_matches(toks, c) for c in cands):
            return True
        return any(t.isdigit() for t in toks) and _name_matches(toks, rec["candidate"])
    return _name_matches(toks, rec["candidate"])


def filter_options(departments=None, block: bool = False):
    """Values for the server-side dropdowns (hosts + departments). Non-blocking by
    default: returns whatever is already cached so a page load never triggers a
    ~27s S3 scan. Pass block=True to force the index to be built first.

    When `departments` is given (a user's allowed set), hosts are scoped to those
    departments so a user never sees host names from departments they can't access."""
    if DEMO_MODE:
        recs = DEMO_RECORDS
    else:
        if block:
            get_records()
        with _lock:
            recs = _cache["records"]
        if not recs:
            return {"hosts": [], "departments": []}

    if departments is not None:
        allowed = set(departments)
        recs = [r for r in recs if r["department"] in allowed]

    # Hosts grouped per department, so the UI can narrow the Host dropdown to the
    # chosen department instead of always showing every allowed department's hosts.
    by_dept = {}
    for r in recs:
        by_dept.setdefault(r["department"], set()).add(r["host"])
    hosts_by_department = {d: sorted(hs, key=str.lower) for d, hs in by_dept.items()}
    all_hosts = sorted({h for hs in by_dept.values() for h in hs}, key=str.lower)

    return {
        "hosts":               all_hosts,                                   # union (All departments)
        "departments":         sorted(by_dept.keys(), key=str.lower),
        "hosts_by_department": hosts_by_department,
    }


# User-selectable sort orders. Applied as a stable re-sort on top of the default
# (department, candidate, date…) tuple, so equal keys keep a deterministic order.
_SORTS = {
    "date_desc": (lambda r: r["date"], True),
    "date_asc":  (lambda r: r["date"], False),
    "size_desc": (lambda r: r["size"], True),
    "size_asc":  (lambda r: r["size"], False),
    "candidate": (lambda r: r["candidate"].lower(), False),
}


def search(candidate="", company="", date="", meeting_id="", file_type="", host="",
           department="", allowed_departments=None, limit=None, offset=0, sort=""):
    """Filter the index. Returns (rows, total, total_size) where rows is the
    `offset:offset+limit` page (limit defaults to RESULT_LIMIT) of the sorted
    match set, while total/total_size reflect the FULL match set.

    `allowed_departments` is the access mask for the signed-in user: records outside
    it are dropped BEFORE any other filter, so a user can never reach a department
    they were not granted (admins pass the full list). `department` is an optional
    user-chosen narrowing within that allowed set.

    Empty query short-circuits to ([], 0, 0) WITHOUT touching S3 — so landing the
    page (or a blank submit) never scans or serialises the whole bucket. The access
    mask is NOT counted as a query, so a blank submit still returns nothing."""
    candidate  = (candidate or "").strip()
    company    = (company or "").strip().lower()
    date       = (date or "").strip().lower()
    meeting_id = (meeting_id or "").strip().lower()
    file_type  = (file_type or "").strip().lower()   # a category key (video/audio/…)
    host       = (host or "").strip().lower()
    department = (department or "").strip()
    # Tokenise up front: separator-only input ('-', '_') yields no tokens and must
    # count as NO query, or it would slip past the blank-submit guard and dump the
    # caller's whole allowed corpus.
    cand_toks = _cand_tokens(candidate)

    if not any([cand_toks, company, date, meeting_id, file_type, host, department]):
        return [], 0, 0

    allowed = set(allowed_departments) if allowed_departments is not None else None
    recs = get_records()
    out = []
    for r in recs:
        if allowed is not None and r["department"] not in allowed:   # access mask first
            continue
        if department and department != r["department"]:
            continue
        if not _match_candidate(cand_toks, r):
            continue
        if company and company not in r["company"].lower():     # substring, free-text
            continue
        if date and date not in r["date"].lower():              # "2026-06" matches a month
            continue
        if meeting_id and meeting_id not in r["meeting_id"].lower():
            continue
        if file_type and file_type != r["category"]:            # canonical category
            continue
        if host and host != r["host"].lower():
            continue
        out.append(r)

    out.sort(key=lambda r: (r["department"].lower(), r["candidate"].lower(), r["date"], r["meeting_id"], r["file_type"]))
    if sort in _SORTS:
        keyf, rev = _SORTS[sort]
        out.sort(key=keyf, reverse=rev)   # stable → the tuple above breaks ties
    total = len(out)
    total_size = sum(r["size"] for r in out)
    lim = RESULT_LIMIT if limit is None else limit
    start = max(0, int(offset or 0))
    rows = out[start:start + lim]
    if cand_toks:
        # Tell the UI WHICH attendee(s) matched in a group session. Shallow copies
        # only for the returned page — the shared cached records are never mutated.
        rows = [
            dict(r, matched_candidates=[c for c in r["candidates"] if _name_matches(cand_toks, c)])
            if len(r.get("candidates") or []) > 1 else r
            for r in rows
        ]
    return rows, total, total_size


# ─────────────────────────────────────────────────────────────────────────────
# Downloads
# ─────────────────────────────────────────────────────────────────────────────
def _key_exists(key: str) -> bool:
    return key in _records_by_key()


def department_of(key: str) -> str:
    """The top-level department folder a key belongs to ('' for a malformed key)."""
    return key.split("/", 1)[0] if key else ""


def key_allowed(key: str, allowed_departments) -> bool:
    """Server-side gate for download/view: the key must exist in the index AND sit
    in a department the caller was granted. Never trust a key from the client alone."""
    if not key:
        return False
    if department_of(key) not in (allowed_departments or []):
        return False
    return _key_exists(key)


# Mime types we want the browser to render/play inline (the rest fall back to
# Python's mimetypes guess). m4a is audio/mp4; vtt is text/vtt.
_INLINE_CONTENT_TYPES = {
    "mp4":  "video/mp4",
    "m4a":  "audio/mp4",
    "vtt":  "text/vtt; charset=utf-8",
    "txt":  "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


def content_type_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _INLINE_CONTENT_TYPES.get(ext) or mimetypes.guess_type(filename)[0] or ""


# Small text-ish files are proxied through the app for inline preview (same-origin,
# so the browser's fetch() isn't blocked by S3 CORS). Media stays a direct redirect.
_TEXT_PREVIEW_EXTS = {"vtt", "txt", "html", "htm", "json", "csv", "srt", "log", "md"}
# Hard ceiling so a mislabelled huge file can never be slurped into app memory.
TEXT_PREVIEW_MAX_BYTES = int(os.environ.get("TEXT_PREVIEW_MAX_BYTES", str(15 * 1024 * 1024)))


def is_text_preview(key: str) -> bool:
    fn = key.rsplit("/", 1)[-1]
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    return ext in _TEXT_PREVIEW_EXTS


def get_object_bytes(key: str, max_bytes: int = TEXT_PREVIEW_MAX_BYTES):
    """Read an object's bytes (capped) for in-app preview. Returns (data, content_type).
    Raises if the object is larger than max_bytes so we never blow up memory."""
    obj = _client().get_object(Bucket=BUCKET, Key=key)
    length = obj.get("ContentLength")
    if length is not None and length > max_bytes:
        raise ValueError("File too large to preview in-app (%d bytes)." % length)
    data = obj["Body"].read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("File too large to preview in-app.")
    ctype = content_type_for(key.rsplit("/", 1)[-1]) or obj.get("ContentType") or "text/plain; charset=utf-8"
    return data, ctype


def presigned_url(key: str, inline: bool = False) -> str:
    """Temporary direct-to-S3 link (keeps EC2 out of the data path).

    inline=False  -> 'attachment' (forces a download, used by the download buttons).
    inline=True   -> 'inline' + a sensible Content-Type, so the browser plays/renders
                     the file in place. Used by /api/view for view-only access."""
    filename = key.rsplit("/", 1)[-1]
    disposition = "inline" if inline else "attachment"
    params = {
        "Bucket": BUCKET,
        "Key": key,
        "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
    }
    if inline:
        ct = content_type_for(filename)
        if ct:
            params["ResponseContentType"] = ct
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=URL_EXPIRY)


def _flat_name(rec: dict) -> str:
    """Readable, unique name for a file inside the bulk zip. Group sessions use
    the first attendee + a count instead of the full hyphen-joined roster, which
    would otherwise blow past Windows' 255-char extraction limit."""
    cands = rec.get("candidates") or [rec["candidate"]]
    cand = cands[0] if len(cands) == 1 else f"{cands[0]}_and_{len(cands) - 1}_more"
    base = f"{cand}__{rec['company']}__{rec['date']}__{rec['round']}__{rec['meeting_id']}__{rec['file_type']}__{rec['filename']}"
    return base.replace("/", "_")


def build_zip(keys):
    """Stream the given S3 objects into a temp zip on disk and return its path.
    ZIP_STORED (no compression) because media is already compressed — fast and
    memory-light. Caller is responsible for deleting the returned path."""
    rec_by_key = _records_by_key()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for key in keys:
                rec = rec_by_key.get(key)
                if rec is None:
                    continue
                arcname = _flat_name(rec)
                if DEMO_MODE:
                    zf.writestr(arcname, _demo_bytes(rec))
                    continue
                obj = _client().get_object(Bucket=BUCKET, Key=key)
                with zf.open(arcname, "w") as dest:
                    for chunk in obj["Body"].iter_chunks(1024 * 256):
                        dest.write(chunk)
        tmp.close()
        return tmp.name
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# DEMO MODE — sample data + fake file bytes so the UI is fully testable offline
# ─────────────────────────────────────────────────────────────────────────────
def _mk(dept, host, cand, company, date, rnd, mid, files):
    out = []
    for ft, fname, size in files:
        key = f"{dept}/{host}/2026/June/{cand}/{company}/{date}/{rnd}/{mid}/{ft}/{fname}"
        rec = _parse_key(key, size)
        if rec:
            out.append(rec)
    return out


DEMO_RECORDS = []
DEMO_RECORDS += _mk("Interview-Success", "Vivek_Parmar", "Akhilendra_NA_Sirikonda", "Gartner",
                    "2026-06-10", "Introduction_Call", "96355112813",
                    [("MP4", "rec_96355112813.mp4", 184_000_000),
                     ("M4A", "audio_96355112813.m4a", 12_400_000),
                     ("TRANSCRIPT", "transcript_96355112813.vtt", 84_120)])
DEMO_RECORDS += _mk("Interview-Success", "Vivek_Parmar", "Aditya_Walker", "Amazon",
                    "2026-06-12", "Technical_Round_1", "96355119001",
                    [("MP4", "rec_96355119001.mp4", 221_000_000),
                     ("TRANSCRIPT", "transcript_96355119001.vtt", 91_300)])
DEMO_RECORDS += _mk("Interview-Success", "Abhishek_Jain", "Chaitanya_Nenavath", "Google",
                    "2026-06-15", "HR_Round", "96355120044",
                    [("MP4", "rec_96355120044.mp4", 142_000_000),
                     ("M4A", "audio_96355120044.m4a", 9_800_000)])
DEMO_RECORDS += _mk("HR", "Abhishek_Jain", "Sanjana_Gupta", "Amazon",
                    "2026-06-15", "Technical_Round_2", "96355120099",
                    [("MP4", "rec_96355120099.mp4", 305_000_000),
                     ("M4A", "audio_96355120099.m4a", 15_100_000),
                     ("TRANSCRIPT", "transcript_96355120099.vtt", 102_400)])
DEMO_RECORDS += _mk("Marketing", "Ishita_Aggarwal", "Bala_Praneeth_Reddy_Basani", "Gartner",
                    "2026-06-18", "Final_Round", "96355121200",
                    [("MP4", "rec_96355121200.mp4", 198_000_000),
                     ("TRANSCRIPT", "transcript_96355121200.vtt", 77_900)])
DEMO_RECORDS += _mk("Training", "Ishita_Aggarwal", "Dharani_Katta", "Microsoft",
                    "2026-06-20", "Introduction_Call", "96355121888",
                    [("MP4", "rec_96355121888.mp4", 167_000_000),
                     ("M4A", "audio_96355121888.m4a", 11_200_000),
                     ("TRANSCRIPT", "transcript_96355121888.vtt", 65_400)])


def _mk_c(dept, host, cand, date, time_folder, mid, files):
    """Layout C keys ({Dept}/{Host}/{Y}/{M}/{Candidate}/{Date}/{Time-*-IST}/{MeetingID}/{FileType}/{file})
    — the shape HR/QMS/Advanced-Training/… write (no Company/Round folders)."""
    out = []
    for ft, fname, size in files:
        key = f"{dept}/{host}/2026/June/{cand}/{date}/{time_folder}/{mid}/{ft}/{fname}"
        rec = _parse_key(key, size)
        if rec:
            out.append(rec)
    return out


# A group training session: every attendee lives in ONE candidate folder,
# hyphen-joined behind a numeric id — exactly how Advanced-Training uploads look.
DEMO_RECORDS += _mk_c("Advanced-Training", "Rahul_Verma",
                      "700758249_Shafahad_Mohammed-Abdu_Raziq-Arbaazuddin_Mohammed-"
                      "gangadhar_dandu-Mohammed_Monis_Khan-Nandini_K-Ram_Reddy-Syed_Faraaz-Venkata_Jagan_Mohan",
                      "2026-06-22", "Time-8-30-PM-IST", "700758249",
                      [("MP4", "rec_700758249.mp4", 402_000_000),
                       ("TRANSCRIPT", "transcript_700758249.vtt", 118_000)])
DEMO_RECORDS += _mk_c("QMS", "Priya_Nair", "Rohan_Mehta",
                      "2026-06-21", "Time-4-00-PM-IST", "96355125555",
                      [("MP4", "rec_96355125555.mp4", 150_000_000),
                       ("M4A", "audio_96355125555.m4a", 10_300_000)])


def _demo_bytes(rec: dict) -> bytes:
    txt = (
        f"DEMO PLACEHOLDER FILE\n"
        f"--------------------\n"
        f"candidate : {rec['candidate']}\n"
        f"company   : {rec['company']}\n"
        f"date      : {rec['date']}\n"
        f"round     : {rec['round']}\n"
        f"meeting   : {rec['meeting_id']}\n"
        f"file_type : {rec['file_type']}\n"
        f"filename  : {rec['filename']}\n"
        f"s3_key    : {rec['key']}\n\n"
        f"(DEMO_MODE=true — real bytes are served from S3 in production.)\n"
    )
    return txt.encode("utf-8")


def demo_file_response(key: str):
    rec = next((r for r in DEMO_RECORDS if r["key"] == key), None)
    if rec is None:
        return None, None
    return _demo_bytes(rec), rec["filename"]


# ─────────────────────────────────────────────────────────────────────────────
# Background warm-up: each worker builds its index off the request path at boot,
# so the first real user never waits on the ~27s scan. Errors (e.g. expired STS
# creds) are swallowed here — they surface normally on the next real request.
# ─────────────────────────────────────────────────────────────────────────────
def _warm():
    try:
        get_records()
    except Exception:
        pass


if not DEMO_MODE:
    threading.Thread(target=_warm, name="index-warm", daemon=True).start()
