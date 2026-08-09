"""
s3_service.py
-------------
All S3 access for the Interview-Success recording portal lives here.

The bucket has several department folders at the top level (HR, Interview-Success,
Marketing, …). A department is normally the FIRST path segment, but it may also be
NESTED two levels deep — Training was split into its own sub-departments:

    Training/Resume-Based/  Training/Advanced/
    Training/Interview-Readiness/  Training/Other/

so the department is whichever configured DEPARTMENTS entry matches the longest
leading run of segments (see _department_of). Everything below the department is
one of THREE key layouts (all handled by _parse_key):

    Layout A (10 segments below the department) — Interview-Success:
        {Dept}/{Host}/{Year}/{Month}/{Candidate}/{Company}/{Date}/{Round}/{MeetingID}/{FileType}/{file}
    Layout B (11 segments — extra MeetingID + a Time-*-IST folder) — Interview-Success:
        {Dept}/{Host}/{Year}/{Month}/{Candidate}/{MeetingID}/{Company}/{Date}/{Round}/{Time-*-IST}/{FileType}/{file}
    Layout C (9 segments — NO Company/Round, has a Time-*-IST folder) — HR/Marketing/…
    and every Training/* sub-department, where {Host} is the trainer and
    {Candidate} is a single trainee or a hyphen-joined group roster:
        {Dept}/{Host}/{Year}/{Month}/{Candidate}/{Date}/{Time-*-IST}/{MeetingID}/{FileType}/{file}

Reliable anchors in ALL layouts:  host/year/month/candidate = the first four
segments BELOW the department, file_type = seg[-2], filename = seg[-1]; the date
matches YYYY-MM-DD, the meeting id is the all-digit segment, company is the label
just before the date and round the one just after (absent in layout C, which has
neither).

Group sessions are the one case where the key is NOT the whole story. Older ones
hyphen-joined every attendee into the candidate folder; current ones name that
folder literally "Group" and put the roster in a participants.json next to the
media folders. That file is the only object whose CONTENT this module reads, and
only to fill in attendee names for display and search — department and host always
come from the key, so a roster can never influence who may see a recording.

The module lists everything under each department prefix, parses each key into a
structured record, attaches any attendee roster, caches the result (TTL + a shared
on-disk index so the 3 gunicorn workers don't each re-scan S3), and exposes
search / filter / download helpers on top of that cache.

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
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

# ─────────────────────────────────────────────────────────────────────────────
# Config (all overridable via .env)
# ─────────────────────────────────────────────────────────────────────────────
BUCKET       = os.environ.get("S3_BUCKET_NAME", "zoom-automation-bucket")
ROOT_PREFIX  = os.environ.get("ROOT_PREFIX", "Interview-Success/")
REGION       = os.environ.get("AWS_REGION", "us-east-1")
# "Department" folders in the bucket. Each holds the same internal layout
# ({Host}/{Year}/{Month}/{Candidate}/…). The portal scans every one of these and
# tags each record with its department; access is then granted per user by an
# admin. Override via DEPARTMENTS="HR,Marketing,…" in .env.
#
# An entry may be NESTED ("Training/Resume-Based") when a top-level folder was
# split into sub-departments that are granted independently. Listing a parent and
# its children together is safe: only the parent is scanned, and each key is
# tagged with the LONGEST matching entry (see _department_of / _scan_prefixes).
#
# An entry may instead END IN "/*" ("Training/*"), which means: every immediate
# child folder of that parent is its own department, discovered from the bucket
# rather than listed here. A sub-department added in S3 later then appears by
# itself on the next index refresh — no .env edit and no restart. Discovery only
# makes a department VISIBLE and grantable; nobody can read it until an admin
# ticks it for them, so this never widens anyone's access on its own.
_DEPARTMENTS_RAW = [d.strip().strip("/") for d in os.environ.get(
    "DEPARTMENTS",
    "HR,Interview-Success,Marketing,Customer-Success,Techsphere,Executive-Assistant,"
    "QMS,Other,CEO,COO,Business-Development,Advanced-Training,Training/*",
).split(",") if d.strip().strip("/")]


def _split_department_config(entries):
    """(exact departments, auto-discovery parents) from the configured entries."""
    exact, parents = [], []
    for entry in entries:
        if entry.endswith("/*"):
            parent = entry[:-2].strip("/")
            if parent and parent not in parents:
                parents.append(parent)
        elif entry not in exact:
            exact.append(entry)
    return exact, parents


# DEPARTMENTS stays the list of explicitly configured names (what most callers
# mean); AUTO_PARENTS holds the "Parent/*" roots whose children are discovered.
# Use all_departments() when you need everything the portal currently knows.
DEPARTMENTS, AUTO_PARENTS = _split_department_config(_DEPARTMENTS_RAW)
CACHE_TTL    = int(os.environ.get("CACHE_TTL_SEC", "300"))
URL_EXPIRY   = int(os.environ.get("PRESIGNED_URL_EXPIRY_SEC", "3600"))
DEMO_MODE    = os.environ.get("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes")
# Max rows returned by a single search (keeps the JSON payload + browser table
# bounded — a broad filter could otherwise match >10k files).
RESULT_LIMIT = int(os.environ.get("SEARCH_RESULT_LIMIT", "500"))
# Hard server-side bounds for a temporary bulk ZIP. These protect local disk,
# S3 bandwidth and a Gunicorn worker even when a client crafts its own request.
BULK_ZIP_MAX_FILES = max(1, int(os.environ.get("BULK_ZIP_MAX_FILES", "250")))
BULK_ZIP_MAX_BYTES = max(1, int(os.environ.get(
    "BULK_ZIP_MAX_BYTES", str(10 * 1024 * 1024 * 1024),
)))
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
_cache = {"records": None, "by_key": None, "by_meeting": None, "options": None, "ts": 0.0}
# Departments seen in any index this process has held (guarded by _lock). Grows
# only — see the note in _store() for why a department must not become unknown
# again once discovered.
_seen_departments = set()
# Why the last index build failed, or None (guarded by _lock). Without this a
# failed build is indistinguishable from a slow one: filter_options() returns
# empty lists either way, so the UI would sit on "indexing bucket…" forever with
# an empty Host dropdown and never say that the credentials had expired.
_index_error = None
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
_TIME_SPLIT_RE = re.compile(r"[-_.\s]+")
_MERIDIEM = ("am", "pm")
# The time folders are written by the Zoom automation in Indian Standard Time, so
# a parsed clock time is IST unless the folder itself says otherwise. The portal
# stores that source zone on every record and converts for display in the browser.
DEFAULT_TIME_ZONE = "IST"


def _is_time(s: str) -> bool:
    return bool(_TIME_RE.match(s or ""))


def _parse_time_folder(segment: str):
    """(canonical "HH:MM", zone) for a "Time-8-30-PM-IST" folder, else ("", "").

    Tolerant of every shape the bucket actually contains — "Time-11-00-AM-IST",
    "Time-1-27-AM-IST" and the abbreviated "Time-6-IST" (hour only, no meridiem).
    Anything it cannot read confidently degrades to "no time" rather than to a
    wrong one: an empty Time column is honest, a fabricated 00:00 is not."""
    if not _is_time(segment):
        return "", ""
    parts = [p for p in _TIME_SPLIT_RE.split(segment)[1:] if p]   # drop the "Time" prefix
    if not parts:
        return "", ""
    zone = parts.pop().upper()                      # trailing IST (guaranteed by _is_time)
    meridiem = parts.pop().lower() if parts and parts[-1].lower() in _MERIDIEM else ""
    if not parts or not parts[0].isdigit():
        return "", ""
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if meridiem:
        if not 1 <= hour <= 12:
            return "", ""
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "", ""
    return "%02d:%02d" % (hour, minute), zone


def _time_of_key(key: str):
    """(time, zone) for a whole key — the fallback used to backfill a record that
    was parsed before times were indexed. _parse_key scans only the segments
    between the candidate and the file-type folder, which is where a real Time
    folder always sits."""
    for segment in (key or "").split("/"):
        time_of_day, zone = _parse_time_folder(segment)
        if time_of_day:
            return time_of_day, zone
    return "", ""


# ── Department resolution (supports nested entries like "Training/Resume-Based") ──
_DEPT_SET = set(DEPARTMENTS)
_DEPT_MAX_DEPTH = max((len(d.split("/")) for d in DEPARTMENTS), default=1)


def _department_of(parts):
    """(department, segments_consumed) for a key already split on "/".

    An explicitly configured entry wins, using the LONGEST match — so a key under
    Training/Resume-Based/… is tagged with the sub-department rather than with a
    plain "Training" that also happens to be configured. Otherwise, if the key
    sits under a "Parent/*" auto-discovery root, the department is the parent PLUS
    its next segment, which is how a sub-department created in S3 later gets
    indexed without appearing in any config. Returns (None, 0) for a key outside
    every department, so an unrelated top-level folder can never enter the index."""
    for depth in range(min(_DEPT_MAX_DEPTH, len(parts)), 0, -1):
        candidate = "/".join(parts[:depth])
        if candidate in _DEPT_SET:
            return candidate, depth
    for parent in AUTO_PARENTS:
        depth = len(parent.split("/"))
        # Need a child segment AND something below it, else this is a stray file
        # sitting directly in the parent folder rather than in a sub-department.
        if len(parts) > depth + 1 and "/".join(parts[:depth]) == parent:
            return "/".join(parts[:depth + 1]), depth + 1
    return None, 0


def _scan_prefixes():
    """The minimal set of prefixes to list. A department nested inside another
    configured department (or inside an auto-discovery parent) is dropped: listing
    the ancestor already returns its keys, and _department_of tags each one with
    the most specific match. Without this, having both "Training/*" and
    "Training/Advanced" configured would index every Advanced object twice."""
    # dict.fromkeys dedupes while keeping order: "Training" and "Training/*"
    # resolve to the same root, and listing it twice would index every object
    # underneath it twice.
    roots = list(dict.fromkeys(list(DEPARTMENTS) + list(AUTO_PARENTS)))
    return [d for d in roots
            if not any(other != d and d.startswith(other + "/") for other in roots)]


def all_departments():
    """Every department the portal currently knows about: the explicitly
    configured ones plus any discovered under a "Parent/*" entry in the live index.

    This is the vocabulary for granting access and for the admin/search pickers —
    prefer it over DEPARTMENTS anywhere a user-facing list or a grant is validated,
    or a newly discovered sub-department would be unreachable and, worse, an
    existing grant naming it would be treated as invalid."""
    if DEMO_MODE:
        discovered = {r["department"] for r in DEMO_RECORDS}
    else:
        with _lock:
            discovered = set(_seen_departments)
    return sorted(set(DEPARTMENTS) | discovered, key=str.lower)

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


# ── Name normalisation (one definition shared by indexing and searching) ──────
# Real rosters are messy: "naman_jain", "naman-jain", "Naman Jain", "563-bhanu_Varshini"
# (a numeric id glued to the front), "Naman_Sir" (an honorific stuck to the name).
# Everything is folded to lowercase space-separated words so any of those spellings
# matches any other, and word ORDER never matters (see _name_matches).
_TITLE_WORDS = {"sir", "madam", "maam", "mam", "mr", "mrs", "ms", "miss", "dr", "prof"}
# A leading numeric id: "700758249_", "563-", "12 ". Stripped from the QUERY only —
# a roster name keeps its id so a pasted "563" still finds that person.
_LEAD_ID_RE = re.compile(r"^\d+[\s\-_.]+")
# Apostrophes vanish rather than becoming a split, so "ma'am" folds to the
# honorific "maam" instead of the two meaningless words "ma" + "am", and
# "O'Brien" stays one word.
_APOSTROPHE_RE = re.compile(r"['’ʼ`]")
_NON_ALNUM_RE = re.compile(r"[^0-9a-zA-Z]+")
# Distinct names are few (a few thousand hosts/attendees) while _norm_name is called
# per record per search, so memoising it turns the hot path into a dict hit.
_norm_cache = {}


def _norm_name(value: str) -> str:
    """Comparable form of a name: no honorifics, single spaces, lowercase.
    Returns "" when nothing name-like is left."""
    try:
        return _norm_cache[value]
    except (KeyError, TypeError):
        pass
    text = _APOSTROPHE_RE.sub("", (value or "").strip())
    words = [w for w in _NON_ALNUM_RE.sub(" ", text).lower().split()
             if w and w not in _TITLE_WORDS]
    result = " ".join(words)
    if isinstance(value, str) and len(_norm_cache) < 200_000:
        _norm_cache[value] = result      # benign race: same value either way
    return result

# Low-cardinality fields are interned so the 50k records don't hold 50k copies of
# the same ~21 hosts / ~8 file-types / handful of dates — a big per-worker RAM win.
_INTERN_FIELDS = ("department", "host", "year", "month", "company", "date", "round",
                  "time", "time_zone", "file_type", "category", "ext")


def _intern_rec(d: dict) -> dict:
    # Records read from a disk index written before times were indexed have no
    # "time" — recover it from the key so a stale index degrades to a slower load
    # rather than to a blank Time column. (The schema bump normally rebuilds first.)
    if "time" not in d:
        d["time"], d["time_zone"] = _time_of_key(d.get("key", ""))
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
    under the expected {Department}/{Host}/… layout (folder placeholders, short
    keys, keys outside every configured department).

    The department is the longest configured DEPARTMENTS entry matching the start
    of the key — one segment for HR/Interview-Success/…, two for the nested
    Training/* sub-departments. Everything after it is the per-department layout
    the parser already understood, so a nested department needs no new layout."""
    parts = key.split("/")
    department, depth = _department_of(parts)
    if department is None:
        return None
    seg = parts[depth:]                  # everything below the department folder
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
    # The same Time folder that must never be read as a company or round IS the
    # meeting's start time — the only clock reading the layout carries. Layout A
    # (Interview-Success, 10 segments) has no such folder, so time stays "".
    time_of_day, time_zone = "", ""
    for s in mid:
        time_of_day, time_zone = _parse_time_folder(s)
        if time_of_day:
            break

    return _intern_rec({
        "department": department,
        "host":       host,
        "year":       year,
        "month":      month,
        "candidate":  candidate,
        "candidates": _split_candidates(candidate),   # people in the meeting (1+)
        "company":    company,
        "date":       date,
        "time":       time_of_day,                     # "HH:MM" in time_zone, or ""
        "time_zone":  time_zone or (DEFAULT_TIME_ZONE if time_of_day else ""),
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
# ── Attendee rosters (participants.json) ─────────────────────────────────────
# Group sessions no longer carry attendee names in the folder path: the folder is
# literally "Group" and the roster lives in a participants.json beside the media
# folders, e.g.
#   Training/Advanced/{Host}/{Y}/{M}/Group/{Date}/{Time}/{MeetingID}/participants.json
# It is the ONLY object whose CONTENT the index reads. Names from it are used for
# display and search only — department and host always come from the key, never
# from this file, so a roster can never affect who is allowed to see a recording.
PARTICIPANTS_FILENAME = "participants.json"
# A roster is well under 1 KB; the cap stops a mislabelled huge object being read.
PARTICIPANTS_MAX_BYTES = 256 * 1024
PARTICIPANTS_WORKERS = max(1, int(os.environ.get("PARTICIPANTS_FETCH_WORKERS", "16")))


def _meeting_prefix(key: str) -> str:
    """The '…/{MeetingID}/' prefix every file of one meeting shares. Media keys end
    in '{FileType}/{filename}', a roster key ends in just 'participants.json'."""
    head, _, tail = key.rpartition("/")
    if tail == PARTICIPANTS_FILENAME:
        return head + "/"
    parts = key.rsplit("/", 2)
    return parts[0] + "/" if len(parts) == 3 else ""


def _roster_names(payload) -> list:
    """Attendee names from a participants.json body, deduped on their normalised
    form so 'Naman_Jain' and 'naman jain' are not listed twice. Tolerant of shape
    drift: a plain string entry works as well as {"name": …}, and anything without
    a letter (a bare id) is skipped."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("candidates")
    if not isinstance(entries, list):
        return []
    seen, out = set(), []
    for entry in entries[:500]:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or not _HAS_LETTER_RE.search(name):
            continue
        key = _norm_name(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _read_roster(key: str):
    """(meeting_prefix, names) for one participants.json. Never raises: an
    unreadable or malformed roster degrades to no names rather than failing the
    whole bucket scan."""
    try:
        obj = _client().get_object(Bucket=BUCKET, Key=key)
        if (obj.get("ContentLength") or 0) > PARTICIPANTS_MAX_BYTES:
            return _meeting_prefix(key), []
        body = obj["Body"].read(PARTICIPANTS_MAX_BYTES + 1)
        if len(body) > PARTICIPANTS_MAX_BYTES:
            return _meeting_prefix(key), []
        return _meeting_prefix(key), _roster_names(json.loads(body))
    except Exception:
        return _meeting_prefix(key), []


def _attach_rosters(records, roster_keys):
    """Replace the path-derived attendee list with the real roster wherever a
    meeting has one. Returns how many meetings were resolved.

    Fetched concurrently because each roster is one small GET and a big bucket has
    thousands of them; a serial pass would add minutes to every scan."""
    if not roster_keys:
        return 0
    rosters = {}
    workers = min(PARTICIPANTS_WORKERS, len(roster_keys))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for prefix, names in pool.map(_read_roster, roster_keys):
            if prefix and names:
                rosters[prefix] = [sys.intern(n) for n in names]
    if not rosters:
        return 0
    for rec in records:
        names = rosters.get(_meeting_prefix(rec["key"]))
        if names:
            rec["candidates"] = names
    return len(rosters)


def _scan_s3():
    client = _client()
    records, roster_keys = [], []
    paginator = client.get_paginator("list_objects_v2")
    # List each department folder separately so an unrelated top-level prefix in
    # the bucket can never leak into the index. Nested departments are covered by
    # their parent's listing when one is configured (see _scan_prefixes).
    for dept in _scan_prefixes():
        for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{dept}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # A roster is metadata about the meeting, not a downloadable file:
                # collect it and keep it out of the searchable record set.
                if key.rpartition("/")[2] == PARTICIPANTS_FILENAME:
                    roster_keys.append(key)
                    continue
                rec = _parse_key(key, obj.get("Size", 0))
                if rec:
                    records.append(rec)
    _attach_rosters(records, roster_keys)
    return records


# Bump whenever the record shape or DEPARTMENTS coverage changes: a persisted
# index with an older schema is rejected, forcing ONE clean re-scan on the first
# boot after a deploy instead of serving pre-upgrade records for up to INDEX_TTL.
INDEX_SCHEMA = 5   # 3: nested departments; 4: attendee rosters; 5: meeting start time


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


def _group_by_meeting(records):
    """{meeting_prefix: [record, …]} — every file that belongs to one meeting.
    Built once per index so a sibling lookup (e.g. "which caption file goes with
    this video?") is a dict hit instead of a scan over every record."""
    by_meeting = {}
    for r in records:
        by_meeting.setdefault(_meeting_prefix(r["key"]), []).append(r)
    return by_meeting


def _store(records):
    options = _build_options(records)
    by_key = {r["key"]: r for r in records}
    by_meeting = _group_by_meeting(records)
    with _lock:
        _cache["records"] = records
        _cache["by_key"] = by_key
        _cache["by_meeting"] = by_meeting
        _cache["options"] = options
        _cache["ts"] = time.time()
        # Remember every department ever seen in this process. An auto-discovered
        # sub-department must not stop being grantable just because the cache is
        # momentarily cold — otherwise an admin saving a user mid-refresh would
        # have that user's grant rejected as an unknown department.
        _seen_departments.update(options.get("departments") or ())


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
    # Meetings whose attendee list came from a participants.json rather than from
    # the folder name. Surfaced in cache_info so "did the rosters load?" is
    # answerable from the UI instead of by reading logs the module doesn't write.
    with_roster = {
        _meeting_prefix(r["key"])
        for r in records
        if r.get("candidates") and r["candidates"] != _split_candidates(r["candidate"])
    }
    return {
        "hosts":       sorted({r["host"] for r in records}, key=str.lower),
        "departments": sorted({r["department"] for r in records}, key=str.lower),
        "rosters":     len(with_roster),
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
        global _index_error
        records = None if force else _load_disk_index(INDEX_TTL)
        if records is None:
            try:
                records = _rebuild_from_s3(force)
            except Exception as e:
                # Remember WHY, then re-raise: callers still fail as before, but a
                # page that only reads the cache can now report the real reason.
                with _lock:
                    _index_error = str(e)
                raise

        with _lock:
            _index_error = None
        _store(records)
        return records


def is_ready() -> bool:
    if DEMO_MODE:
        return True
    with _lock:
        return _cache["records"] is not None


def cache_info():
    if DEMO_MODE:
        return {"demo": True, "count": len(DEMO_RECORDS), "age_sec": 0, "ready": True,
                "rosters": _build_options(DEMO_RECORDS)["rosters"]}
    with _lock:
        ready = _cache["records"] is not None
        age = time.time() - _cache["ts"] if ready else None
        count = len(_cache["records"]) if ready else 0
        rosters = (_cache["options"] or {}).get("rosters", 0)
        error = _index_error
    return {"demo": False, "count": count, "rosters": rosters,
            "age_sec": round(age) if age is not None else None, "ready": ready,
            # Raw message; the caller translates it into something actionable.
            "error": error}


def _records_by_key():
    if DEMO_MODE:
        return {r["key"]: r for r in DEMO_RECORDS}
    get_records()  # ensure cache is fresh (handles TTL/cold start)
    with _lock:
        return _cache["by_key"] or {}


def _records_by_meeting():
    if DEMO_MODE:
        return _group_by_meeting(DEMO_RECORDS)
    get_records()  # ensure cache is fresh (handles TTL/cold start)
    with _lock:
        return _cache["by_meeting"] or {}


# ─────────────────────────────────────────────────────────────────────────────
# Search + filters
# ─────────────────────────────────────────────────────────────────────────────
def _cand_tokens(query: str) -> list:
    """Normalised candidate-search tokens. Underscores, hyphens, dots and case
    all disappear, and an honorific typed by the user ('naman sir') is dropped,
    so 'sirikonda', 'Akhilendra Sirikonda' and 'sirikonda-akhilendra' all hit
    'Akhilendra_NA_Sirikonda'."""
    # An id prefix is dropped from the query so pasting a roster entry verbatim
    # ("563-bhanu_Varshini") searches for the person, not the id.
    return _norm_name(_LEAD_ID_RE.sub("", (query or "").strip())).split()


def _name_matches(toks, name: str) -> bool:
    """Do all query tokens appear in this one person's name?

    Order-independent, so 'jain naman' finds 'Naman_Jain' just like 'naman jain'.
    Tokens match as substrings, which keeps partial names ('bhanu') working. The
    query is also tried with its separators removed against the name with its
    spaces removed, so 'namanjain' finds 'Naman_Jain' — people paste names both
    glued together and spaced out."""
    normalized = _norm_name(name)
    if not normalized:
        return False
    if all(t in normalized for t in toks):
        return True
    return "".join(toks) in normalized.replace(" ", "")


def _match_candidate(toks, rec: dict) -> bool:
    """True when the query names someone in this recording.

    EVERY token must land inside ONE person's name, so 'mohammed reddy' cannot
    match a group session where Mohammed and Reddy are two different attendees.
    The attendee list comes from participants.json when the meeting has one, and
    otherwise from splitting the candidate folder — this is what lets a search
    find a trainee inside a "Group" folder that does not carry any names.

    The raw folder is tried as a last resort so a pasted id ('152026_') or a
    literal folder name stays searchable; for a group that fallback is limited to
    numeric queries, because the folder holds no single attendee's name."""
    if not toks:
        return True
    cands = rec.get("candidates") or [rec["candidate"]]
    if any(_name_matches(toks, c) for c in cands):
        return True
    if len(cands) > 1 and not any(t.isdigit() for t in toks):
        return False
    return _name_matches(toks, rec["candidate"])


def _host_allowed(rec, allowed_hosts) -> bool:
    """Per-department host mask: {dept: [host, …]}. A department with no entry
    (or an empty list) is unrestricted; otherwise the record's host must be in
    the granted list."""
    if not allowed_hosts:
        return True
    hs = allowed_hosts.get(rec["department"])
    return not hs or rec["host"] in hs


def _in_department_grant(rec, allowed_departments, allowed_hosts) -> bool:
    """Does the caller reach this record through their DEPARTMENT grant?
    `allowed_departments=None` means "no mask" (an admin / an internal caller)."""
    if allowed_departments is not None and rec["department"] not in allowed_departments:
        return False
    return _host_allowed(rec, allowed_hosts)


def _in_meeting_grant(rec, allowed_meetings) -> bool:
    """Does the caller reach this record through an individually shared meeting?

    Keyed on the meeting id alone, so every file of that meeting (video, audio,
    transcript, chat) comes with it — that is what "share this meeting" means.
    Deliberately independent of the department mask: the whole point of a meeting
    grant is to reach one recording inside a department the user cannot browse."""
    return bool(allowed_meetings) and rec["meeting_id"] in allowed_meetings


def _visible(rec, allowed_departments, allowed_hosts, allowed_meetings) -> bool:
    """The single access rule: a record is reachable through the department grant
    OR through an individual meeting grant. Every route funnels through this."""
    return (_in_department_grant(rec, allowed_departments, allowed_hosts)
            or _in_meeting_grant(rec, allowed_meetings))


def record_downloadable(rec, access) -> bool:
    """May this caller DOWNLOAD this particular record (vs only stream it)?

    Download stopped being one flag per account when meetings became shareable:
    a meeting can be shared view-only with someone who may download their own
    departments, and vice versa. The most permissive route the caller actually
    has to the file wins — being denied a download of a file you could already
    download through your department would make no sense."""
    if access.get("can_download") and _in_department_grant(
            rec, access.get("departments"), access.get("hosts")):
        return True
    meetings = access.get("meetings") or {}
    return bool(meetings.get(rec["meeting_id"]))


def filter_options(departments=None, allowed_hosts=None, allowed_meetings=None,
                   block: bool = False):
    """Values for the server-side dropdowns (hosts + departments). Non-blocking by
    default: returns whatever is already cached so a page load never triggers a
    ~27s S3 scan. Pass block=True to force the index to be built first.

    When `departments` is given (a user's allowed set), hosts are scoped to those
    departments so a user never sees host names from departments they can't access.
    `allowed_hosts` additionally hides hosts outside a per-department grant."""
    if DEMO_MODE:
        recs = DEMO_RECORDS
    else:
        if block:
            get_records()
        with _lock:
            recs = _cache["records"]
        if not recs:
            return {"hosts": [], "departments": []}

    if departments is not None or allowed_hosts or allowed_meetings:
        allowed = set(departments) if departments is not None else None
        # Same rule the search uses, so a shared meeting's host actually appears
        # in the Host dropdown instead of the user filtering by a host they can
        # see in their own results but not select.
        recs = [r for r in recs if _visible(r, allowed, allowed_hosts, allowed_meetings)]

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
# The date sorts key on (date, time) so two recordings from the same day are
# ordered by when they actually happened rather than arbitrarily.
_SORTS = {
    "date_desc": (lambda r: (r["date"], r.get("time") or ""), True),
    "date_asc":  (lambda r: (r["date"], r.get("time") or ""), False),
    "size_desc": (lambda r: r["size"], True),
    "size_asc":  (lambda r: r["size"], False),
    "candidate": (lambda r: r["candidate"].lower(), False),
}


def _category_filter(file_type):
    """The set of categories a file-type filter selects, from a single value, a
    comma-separated string or a list — so the multi-select UI, an older
    single-value client and a direct caller all share one code path.

    Unrecognised values are deliberately KEPT: an unknown category then matches
    nothing, exactly as a single bogus value always did. Dropping it would silently
    turn a typo'd filter into "no filter at all" and return the whole corpus."""
    if isinstance(file_type, str):
        raw = file_type.split(",")
    elif isinstance(file_type, (list, tuple, set)):
        raw = [piece for item in file_type if isinstance(item, str) for piece in item.split(",")]
    else:
        raw = []
    out = []
    for value in raw:
        value = value.strip().lower()
        if value and value not in out:
            out.append(value)
    return out


def _clean_iso_date(value):
    """A YYYY-MM-DD bound for the date-range filter, or "" for anything else.
    Partial input ("2026-06") is not a bound — it belongs in the free-text `date`
    filter, which already matches a whole month as a substring."""
    value = (value or "").strip()
    return value if _DATE_RE.match(value) else ""


def search(candidate="", company="", date="", meeting_id="", file_type="", host="",
           department="", date_from="", date_to="", allowed_departments=None,
           allowed_hosts=None, allowed_meetings=None, limit=None, offset=0, sort=""):
    """Filter the index. Returns (rows, total, total_size) where rows is the
    `offset:offset+limit` page (limit defaults to RESULT_LIMIT) of the sorted
    match set, while total/total_size reflect the FULL match set.

    `allowed_departments` is the access mask for the signed-in user: records outside
    it are dropped BEFORE any other filter, so a user can never reach a department
    they were not granted (admins pass the full list). `allowed_hosts` narrows a
    granted department further to specific hosts ({dept: [host, …]}).
    `allowed_meetings` ({meeting_id: can_download}) ADDS individually shared
    meetings on top, wherever they live. All three masks are applied before the
    user's own filters, so a crafted host/department query param can never widen
    access. `department` is an optional user-chosen narrowing, and it narrows the
    visible set only — it can never reveal a record the masks excluded.

    `file_type` accepts SEVERAL categories at once ("video,audio" or a list), and
    `date_from`/`date_to` are an inclusive YYYY-MM-DD range — either bound alone is
    open-ended, and both set to the same day is a single-day filter. The range
    combines with the free-text `date` (which still matches a partial value such as
    a whole month) rather than replacing it.

    Empty query short-circuits to ([], 0, 0) WITHOUT touching S3 — so landing the
    page (or a blank submit) never scans or serialises the whole bucket. The access
    mask is NOT counted as a query, so a blank submit still returns nothing."""
    candidate  = (candidate or "").strip()
    company    = (company or "").strip().lower()
    date       = (date or "").strip().lower()
    meeting_id = (meeting_id or "").strip().lower()
    host       = (host or "").strip().lower()
    department = (department or "").strip()
    categories = set(_category_filter(file_type))    # category keys (video/audio/…)
    date_from  = _clean_iso_date(date_from)
    date_to    = _clean_iso_date(date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from      # a reversed range is still a range
    # Tokenise up front: separator-only input ('-', '_') yields no tokens and must
    # count as NO query, or it would slip past the blank-submit guard and dump the
    # caller's whole allowed corpus.
    cand_toks = _cand_tokens(candidate)

    if not any([cand_toks, company, date, date_from, date_to, meeting_id,
                categories, host, department]):
        return [], 0, 0

    allowed = set(allowed_departments) if allowed_departments is not None else None
    recs = get_records()
    out = []
    for r in recs:
        # Access mask first, always: department+host grant OR a shared meeting.
        if not _visible(r, allowed, allowed_hosts, allowed_meetings):
            continue
        if department and department != r["department"]:
            continue
        if not _match_candidate(cand_toks, r):
            continue
        if company and company not in r["company"].lower():     # substring, free-text
            continue
        if date and date not in r["date"].lower():              # "2026-06" matches a month
            continue
        if date_from or date_to:
            # ISO dates compare correctly as strings. An undated recording cannot
            # be placed inside a range, so it is excluded rather than assumed in.
            rec_date = r["date"]
            if not rec_date:
                continue
            if date_from and rec_date < date_from:
                continue
            if date_to and rec_date > date_to:
                continue
        if meeting_id and meeting_id not in r["meeting_id"].lower():
            continue
        if categories and r["category"] not in categories:      # canonical categories
            continue
        if host and host != r["host"].lower():
            continue
        out.append(r)

    out.sort(key=lambda r: (r["department"].lower(), r["candidate"].lower(), r["date"],
                            r.get("time") or "", r["meeting_id"], r["file_type"]))
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
# Meeting lookup (admin-only — powers the "share a single meeting" picker)
# ─────────────────────────────────────────────────────────────────────────────
MEETING_LOOKUP_LIMIT = 25


def _collect_meeting(groups, r):
    """Fold one record into the {meeting_id: aggregate} accumulator."""
    g = groups.get(r["meeting_id"])
    if g is None:
        g = groups[r["meeting_id"]] = {
            "meeting_id": r["meeting_id"], "files": 0, "size": 0,
            "departments": set(), "hosts": set(), "candidates": [],
            "dates": set(), "times": set(),
        }
    g["files"] += 1
    g["size"] += r["size"]
    g["departments"].add(r["department"])
    g["hosts"].add(r["host"])
    if r["date"]:
        g["dates"].add(r["date"])
    if r["time"]:
        g["times"].add(r["time"])
    for name in (r.get("candidates") or [r["candidate"]]):
        if name not in g["candidates"] and len(g["candidates"]) < 12:
            g["candidates"].append(name)


def _finish_meeting(g):
    """The JSON-friendly summary of one accumulated meeting."""
    dates = sorted(g["dates"])
    return {
        "meeting_id": g["meeting_id"],
        "files": g["files"],
        "size": g["size"],
        "departments": sorted(g["departments"], key=str.lower),
        "hosts": sorted(g["hosts"], key=str.lower),
        "candidates": g["candidates"],
        "dates": dates,
        "time": sorted(g["times"])[0] if g["times"] else "",
        # >1 means a RECURRING id: granting it shares every one of those sessions.
        "occurrences": len(dates),
    }


def meeting_summaries(query="", limit=MEETING_LOOKUP_LIMIT):
    """Meetings matching a free-text query, collapsed to one row per meeting id.

    Feeds the admin's meeting picker, so it deliberately spans EVERY department —
    an admin already sees everything, and picking a meeting to share is exactly
    the moment they need to look outside one department. Never call this on
    behalf of a normal user."""
    q = (query or "").strip().lower()
    if not q:
        return []
    toks = _cand_tokens(q)
    groups = {}
    for r in get_records():
        if not r["meeting_id"]:
            continue
        if not (q in r["meeting_id"].lower()
                or q in r["department"].lower()
                or q in r["host"].lower()
                or q in (r["company"] or "").lower()
                or q in r["date"]
                or (toks and _match_candidate(toks, r))):
            continue
        _collect_meeting(groups, r)

    out = [_finish_meeting(g) for g in groups.values()]
    # Most recent first: an admin is nearly always sharing something just recorded.
    out.sort(key=lambda m: (m["dates"][-1] if m["dates"] else "", m["meeting_id"]),
             reverse=True)
    return out[:max(1, int(limit or MEETING_LOOKUP_LIMIT))]


def meeting_details(meeting_ids):
    """{meeting_id: summary} for specific ids — what an ALREADY granted meeting
    actually is, so the admin page can show "96355112813 · Akhilendra · HR" rather
    than a bare number nobody can verify. One pass for the whole set.

    An id with no summary (recording deleted, index still warming) is simply
    absent; the grant itself is never dropped on that basis."""
    wanted = {str(m).strip() for m in (meeting_ids or []) if str(m).strip()}
    if not wanted:
        return {}
    groups = {}
    for r in get_records():
        if r["meeting_id"] in wanted:
            _collect_meeting(groups, r)
    return {mid: _finish_meeting(g) for mid, g in groups.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Downloads
# ─────────────────────────────────────────────────────────────────────────────
def record_for_key(key: str):
    """The parsed record for an exact key, or None. For logging/context only —
    callers that gate access must still use key_allowed()."""
    if not key:
        return None
    return _records_by_key().get(key)


# Subtitle formats a browser <track> can render. Zoom writes WebVTT for both the
# TRANSCRIPT and the CC (closed captions) folder, which is what makes captions on
# the video preview possible without converting anything.
CAPTION_EXTS = ("vtt",)


def caption_records(key: str):
    """Caption files that belong to the SAME meeting as `key` — the transcript /
    closed-caption track Zoom writes next to the recording.

    Lookup only: the returned records still have to be authorized by the caller,
    exactly like any other key. They always share the meeting folder of `key`, so
    they can only ever be in the same department, but re-checking keeps the access
    decision in one place instead of relying on that."""
    prefix = _meeting_prefix(key)
    if not prefix:
        return []
    siblings = [r for r in _records_by_meeting().get(prefix, [])
                if r["ext"] in CAPTION_EXTS and r["key"] != key]
    return sorted(siblings, key=lambda r: (r["file_type"].lower(), r["filename"].lower()))


def key_allowed(key: str, allowed_departments, allowed_hosts=None, allowed_meetings=None) -> bool:
    """Server-side gate for download/view: the key must exist in the index and be
    reachable either through the caller's department grant (respecting any host
    restriction) or through an individually shared meeting. Never trust a key
    from the client alone."""
    return authorized_record(key, allowed_departments, allowed_hosts, allowed_meetings) is not None


def authorized_record(key: str, allowed_departments, allowed_hosts=None, allowed_meetings=None):
    """Return the indexed record when ``key`` is inside the caller's access mask.

    Routes that need recording metadata (for example the audit log) use this
    helper so authorization and metadata lookup are one atomic decision against
    the same cached index.  ``None`` deliberately covers both an unknown key and
    a known-but-forbidden key, preventing callers from learning which one it was.

    Note the department list is treated as an explicit allow-list here (an empty
    list grants nothing), unlike search()'s ``None`` = "no mask" — every caller of
    this function passes a real user's grant.
    """
    if not key:
        return None
    rec = _records_by_key().get(key)
    if rec is None:
        return None
    if not _visible(rec, set(allowed_departments or []), allowed_hosts, allowed_meetings):
        return None
    return rec


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
DEMO_RECORDS += _mk("Customer-Success", "Ishita_Aggarwal", "Dharani_Katta", "Microsoft",
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
# Training's nested sub-departments: one 1:1 trainee session and one group session,
# both written exactly as the bucket does (department = TWO leading segments).
DEMO_RECORDS += _mk_c("Training/Resume-Based", "Vivek_Parmar", "Khushali_Prasad",
                      "2026-04-01", "Time-11-00-AM-IST", "8898177914",
                      [("M4A", "a1ac24ce-7bad-4b0e-9663-906dc2bdf0c9.m4a", 197_000),
                       ("TRANSCRIPT", "transcript_8898177914.vtt", 44_800)])
DEMO_RECORDS += _mk_c("Training/Advanced", "Rahul_Verma",
                      "700758300_Nandini_K-Ram_Reddy-Syed_Faraaz",
                      "2026-04-03", "Time-6-30-PM-IST", "700758300",
                      [("MP4", "rec_700758300.mp4", 312_000_000),
                       ("TRANSCRIPT", "transcript_700758300.vtt", 96_500)])

# A group session in the CURRENT shape: the folder is literally "Group" and the
# attendees come from a participants.json, which is what _attach_rosters does in
# production. Names mirror a real roster, including the "563-" id prefix.
_DEMO_GROUP = _mk_c("Training/Advanced", "Sneha_Chaudhary", "Group",
                    "2026-07-09", "Time-1-27-AM-IST", "97609808470",
                    [("MP4", "adbc738d-efe4-4fc8-8993-76bb38751025.mp4", 281_000_000),
                     ("M4A", "audio_97609808470.m4a", 18_400_000),
                     ("TRANSCRIPT", "transcript_97609808470.vtt", 121_000),
                     ("CHAT", "chat_97609808470.txt", 4_120)])
_DEMO_ROSTER = ["563-bhanu_Varshini", "Khaja_Faizan", "Mani", "Mohammed_Farhan_Wajid",
                "Mohammed_Obaid_Ahmed", "Pavithran_Gnanasekaran", "Ruthura_Meedimale",
                "Vidya_Nomula"]
for _rec in _DEMO_GROUP:
    _rec["candidates"] = [sys.intern(n) for n in _DEMO_ROSTER]
DEMO_RECORDS += _DEMO_GROUP


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
