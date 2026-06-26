"""
s3_service.py
-------------
All S3 access for the Interview-Success recording portal lives here.

The bucket contains TWO key layouts (both handled by _parse_key):

    Layout A (10 segments after the prefix):
        Interview-Success/{Host}/{Year}/{Month}/{Candidate}/{Company}/{Date}/{Round}/{MeetingID}/{FileType}/{file}
    Layout B (11 segments after the prefix — an extra MeetingID + a Time-*-IST folder):
        Interview-Success/{Host}/{Year}/{Month}/{Candidate}/{MeetingID}/{Company}/{Date}/{Round}/{Time-*-IST}/{FileType}/{file}

Reliable anchors in BOTH layouts:  candidate = seg[3], file_type = seg[-2],
filename = seg[-1], the date matches YYYY-MM-DD, the meeting id is the all-digit
segment, company is the segment right before the date and round the one right after.

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

import boto3
from botocore.config import Config

# ─────────────────────────────────────────────────────────────────────────────
# Config (all overridable via .env)
# ─────────────────────────────────────────────────────────────────────────────
BUCKET       = os.environ.get("S3_BUCKET_NAME", "zoom-automation-bucket")
ROOT_PREFIX  = os.environ.get("ROOT_PREFIX", "Interview-Success/")
REGION       = os.environ.get("AWS_REGION", "us-east-1")
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

# Low-cardinality fields are interned so the 50k records don't hold 50k copies of
# the same ~21 hosts / ~8 file-types / handful of dates — a big per-worker RAM win.
_INTERN_FIELDS = ("host", "year", "month", "company", "date", "round",
                  "file_type", "category", "ext")


def _intern_rec(d: dict) -> dict:
    for k in _INTERN_FIELDS:
        v = d.get(k)
        if isinstance(v, str):
            d[k] = sys.intern(v)
    return d


def _parse_key(key: str, size):
    """Turn an S3 key into a structured record, or None if it is not a leaf file
    under the expected Interview-Success layout (folder placeholders, short keys)."""
    seg = key.split("/")[_ROOT_DEPTH:]   # drop the Interview-Success/ prefix
    if len(seg) < 10:
        return None

    filename = seg[-1].strip()
    if not filename:
        return None  # folder placeholder / trailing slash

    host, year, month, candidate = seg[0], seg[1], seg[2], seg[3]
    file_type = seg[-2]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # Everything between the candidate and the file_type folder. Layout A holds
    # [Company, Date, Round, MeetingID]; Layout B holds
    # [MeetingID, Company, Date, Round, Time-*-IST]. Anchor on the date.
    mid = seg[4:-2]
    company = date = rnd = ""
    di = next((i for i, s in enumerate(mid) if _DATE_RE.match(s)), None)
    if di is not None:
        date = mid[di]
        if di - 1 >= 0:
            company = mid[di - 1]
        if di + 1 < len(mid):
            rnd = mid[di + 1]
    meeting_id = next((s for s in mid if s.isdigit()), "")

    return _intern_rec({
        "host":       host,
        "year":       year,
        "month":      month,
        "candidate":  candidate,
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
    for page in paginator.paginate(Bucket=BUCKET, Prefix=ROOT_PREFIX):
        for obj in page.get("Contents", []):
            rec = _parse_key(obj["Key"], obj.get("Size", 0))
            if rec:
                records.append(rec)
    return records


def _load_disk_index(max_age):
    """Return the parsed index from INDEX_FILE if it exists and is younger than
    max_age seconds, else None."""
    if not INDEX_FILE or not os.path.exists(INDEX_FILE):
        return None
    try:
        if (time.time() - os.path.getmtime(INDEX_FILE)) >= max_age:
            return None
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None
        return [_intern_rec(r) for r in records]
    except (OSError, ValueError):
        return None


# ── Cross-process election so only ONE worker ever lists S3 at a time ─────────
def _lock_path():
    return (INDEX_FILE + ".lock") if INDEX_FILE else None


def _acquire_scan_lock(stale_sec=180):
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
    deadline = time.time() + 150
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
            json.dump({"records": records, "ts": time.time()}, f)
        os.replace(tmp, INDEX_FILE)
    except OSError:
        pass  # disk cache is an optimisation; never fatal


def _build_options(records):
    """Distinct values for the (small) server-side dropdowns. Company is now a
    free-text input and file-type is a static category list, so only hosts remain."""
    return {"hosts": sorted({r["host"] for r in records}, key=str.lower)}


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
def _match_candidate(query: str, candidate: str) -> bool:
    """Token match that ignores underscores/case so 'sirikonda' or
    'akhilendra sirikonda' both match 'Akhilendra_NA_Sirikonda'."""
    q = (query or "").lower().replace("_", " ").strip()
    if not q:
        return True
    c = candidate.lower().replace("_", " ")
    return all(tok in c for tok in q.split())


def filter_options(block: bool = False):
    """Values for the server-side dropdowns (just hosts now). Non-blocking by
    default: returns whatever is already cached so a page load never triggers a
    ~27s S3 scan. Pass block=True to force the index to be built first."""
    if DEMO_MODE:
        return {"hosts": sorted({r["host"] for r in DEMO_RECORDS}, key=str.lower)}
    if block:
        get_records()
    with _lock:
        opts = _cache["options"]
    return dict(opts) if opts else {"hosts": []}


def search(candidate="", company="", date="", meeting_id="", file_type="", host="", limit=None):
    """Filter the index. Returns (rows, total, total_size) where rows is capped at
    `limit` (default RESULT_LIMIT) but total/total_size reflect the FULL match set.

    Empty query short-circuits to ([], 0, 0) WITHOUT touching S3 — so landing the
    page (or a blank submit) never scans or serialises the whole bucket."""
    candidate  = (candidate or "").strip()
    company    = (company or "").strip().lower()
    date       = (date or "").strip().lower()
    meeting_id = (meeting_id or "").strip().lower()
    file_type  = (file_type or "").strip().lower()   # a category key (video/audio/…)
    host       = (host or "").strip().lower()

    if not any([candidate, company, date, meeting_id, file_type, host]):
        return [], 0, 0

    recs = get_records()
    out = []
    for r in recs:
        if not _match_candidate(candidate, r["candidate"]):
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

    out.sort(key=lambda r: (r["candidate"].lower(), r["date"], r["meeting_id"], r["file_type"]))
    total = len(out)
    total_size = sum(r["size"] for r in out)
    lim = RESULT_LIMIT if limit is None else limit
    return out[:lim], total, total_size


# ─────────────────────────────────────────────────────────────────────────────
# Downloads
# ─────────────────────────────────────────────────────────────────────────────
def _key_exists(key: str) -> bool:
    return key in _records_by_key()


def presigned_url(key: str) -> str:
    """Temporary direct-to-S3 download link (keeps EC2 out of the data path)."""
    filename = key.rsplit("/", 1)[-1]
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=URL_EXPIRY,
    )


def _flat_name(rec: dict) -> str:
    """Readable, unique name for a file inside the bulk zip."""
    base = f"{rec['candidate']}__{rec['company']}__{rec['date']}__{rec['round']}__{rec['meeting_id']}__{rec['file_type']}__{rec['filename']}"
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
def _mk(host, cand, company, date, rnd, mid, files):
    out = []
    for ft, fname, size in files:
        key = f"Interview-Success/{host}/2026/June/{cand}/{company}/{date}/{rnd}/{mid}/{ft}/{fname}"
        rec = _parse_key(key, size)
        if rec:
            out.append(rec)
    return out


DEMO_RECORDS = []
DEMO_RECORDS += _mk("Vivek_Parmar", "Akhilendra_NA_Sirikonda", "Gartner", "2026-06-10",
                    "Introduction_Call", "96355112813",
                    [("MP4", "rec_96355112813.mp4", 184_000_000),
                     ("M4A", "audio_96355112813.m4a", 12_400_000),
                     ("TRANSCRIPT", "transcript_96355112813.vtt", 84_120)])
DEMO_RECORDS += _mk("Vivek_Parmar", "Aditya_Walker", "Amazon", "2026-06-12",
                    "Technical_Round_1", "96355119001",
                    [("MP4", "rec_96355119001.mp4", 221_000_000),
                     ("TRANSCRIPT", "transcript_96355119001.vtt", 91_300)])
DEMO_RECORDS += _mk("Abhishek_Jain", "Chaitanya_Nenavath", "Google", "2026-06-15",
                    "HR_Round", "96355120044",
                    [("MP4", "rec_96355120044.mp4", 142_000_000),
                     ("M4A", "audio_96355120044.m4a", 9_800_000)])
DEMO_RECORDS += _mk("Abhishek_Jain", "Sanjana_Gupta", "Amazon", "2026-06-15",
                    "Technical_Round_2", "96355120099",
                    [("MP4", "rec_96355120099.mp4", 305_000_000),
                     ("M4A", "audio_96355120099.m4a", 15_100_000),
                     ("TRANSCRIPT", "transcript_96355120099.vtt", 102_400)])
DEMO_RECORDS += _mk("Ishita_Aggarwal", "Bala_Praneeth_Reddy_Basani", "Gartner", "2026-06-18",
                    "Final_Round", "96355121200",
                    [("MP4", "rec_96355121200.mp4", 198_000_000),
                     ("TRANSCRIPT", "transcript_96355121200.vtt", 77_900)])
DEMO_RECORDS += _mk("Ishita_Aggarwal", "Dharani_Katta", "Microsoft", "2026-06-20",
                    "Introduction_Call", "96355121888",
                    [("MP4", "rec_96355121888.mp4", 167_000_000),
                     ("M4A", "audio_96355121888.m4a", 11_200_000),
                     ("TRANSCRIPT", "transcript_96355121888.vtt", 65_400)])


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
