/* search.js — drives the search page against the /api/* endpoints in app.py */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const notice = $("notice");
  const resultsArea = $("results-area");
  const summary = $("summary");
  const bulkBar = $("bulk-bar");
  const bulkCount = $("bulk-count");
  const bulkSize = $("bulk-size");

  // Friendly file-type categories (mirrors CATEGORY_LABELS in s3_service.py).
  const CATEGORY_SHORT = {
    video: "Video", audio: "Audio", transcript: "Transcript", chat: "Chat",
    questions: "Questions", summary: "Summary", notes: "Notes", other: "Other",
  };
  // The multi-select file-type options — same keys and order as CATEGORY_LABELS.
  const FILE_TYPES = [
    { value: "video", label: "Video (.mp4)" },
    { value: "audio", label: "Audio (.m4a)" },
    { value: "transcript", label: "Transcript (.vtt)" },
    { value: "chat", label: "Chat (.txt)" },
    { value: "questions", label: "Questions (.html)" },
    { value: "summary", label: "AI summary (.txt)" },
    { value: "notes", label: "Notes (.txt)" },
  ];

  // key -> record, for the rows currently rendered
  let currentRows = new Map();
  // the last search response, so a timezone switch can re-render without re-querying
  let lastData = null;
  // selection persists across pages of the SAME search: key -> size (bytes),
  // so the bulk bar can total files that are no longer on the visible page.
  const selected = new Map();
  // monotonic id so a slow/old response can never clobber a newer search
  let searchSeq = 0;
  // current page (resets to 1 on a new query / sort / per-page change)
  let page = 1;

  // Access, learned from /api/filters (and reconfirmed on every search). The
  // server enforces both for real — these only shape what the UI offers.
  let canDownload = true;
  let allowedDepartments = [];
  let allHosts = [];               // union across allowed departments
  let hostsByDepartment = {};      // { department: [hosts] }

  // ── stored preferences (timezone, playback speed, captions) ──────────────
  // localStorage throws in some privacy modes; a lost preference must never take
  // the page down with it.
  function readPref(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function writePref(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* preference is optional */ }
  }

  // ── time zones ───────────────────────────────────────────────────────────
  // Recordings are FILED under their IST date/time (that is what the S3 folders
  // say). The browser converts that instant for display; the date FILTER still
  // works on the filed IST date, which is why the filter label says so.
  const TZ_KEY = "portal.timezone";
  const ZONES = {
    IST: { id: "Asia/Kolkata",     label: "IST", note: "India Standard Time" },
    EST: { id: "America/New_York", label: "EST", note: "US Eastern (EST/EDT)" },
    UTC: { id: "UTC",              label: "UTC", note: "Coordinated Universal Time" },
  };
  // Offset of the zone a recording's folder time is written in. IST observes no
  // DST, so one fixed offset is exact all year.
  const SOURCE_OFFSETS = { IST: "+05:30", UTC: "+00:00" };
  const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  let tzKey = ZONES[readPref(TZ_KEY)] ? readPref(TZ_KEY) : "IST";
  let fmtCache = { id: "", date: null, time: null };

  function zone() { return ZONES[tzKey] || ZONES.IST; }

  function formatters() {
    const z = zone();
    if (fmtCache.id !== z.id) {
      try {
        fmtCache = {
          id: z.id,
          date: new Intl.DateTimeFormat("en-CA",
            { timeZone: z.id, year: "numeric", month: "2-digit", day: "2-digit" }),
          time: new Intl.DateTimeFormat("en-US",
            { timeZone: z.id, hour: "numeric", minute: "2-digit", hour12: true }),
        };
      } catch (e) {
        fmtCache = { id: z.id, date: null, time: null };   // no tz data — show as filed
      }
    }
    return fmtCache;
  }

  // The instant a recording started, or null when its folder carries no time
  // (Interview-Success layout A). A missing time is shown as "—" rather than
  // guessed at midnight, which would then convert into the wrong day.
  function recordMoment(r) {
    if (!r || !r.date || !ISO_DATE_RE.test(r.date) || !r.time) return null;
    const offset = SOURCE_OFFSETS[r.time_zone || "IST"] || SOURCE_OFFSETS.IST;
    const d = new Date(r.date + "T" + r.time + ":00" + offset);
    return isNaN(d.getTime()) ? null : d;
  }

  function dateCell(r) {
    const at = recordMoment(r);
    const f = formatters();
    if (!at || !f.date) {
      return `<span title="Filed as ${esc(r.date || "no date")} (IST) — no start time recorded">` +
        `${esc(r.date || "—")}</span>`;
    }
    return esc(f.date.format(at));
  }

  function timeCell(r) {
    const at = recordMoment(r);
    const f = formatters();
    if (!at || !f.time) {
      return `<span class="cell-muted" title="This recording's folder carries no start time">—</span>`;
    }
    return esc(f.time.format(at));
  }

  function updateZoneHint() {
    const el = $("zone-hint");
    if (!el) return;
    if (tzKey === "IST") { el.textContent = ""; el.style.display = "none"; return; }
    el.style.display = "";
    el.textContent = `Times converted to ${zone().label} · recordings are filed by IST date`;
  }

  // ── helpers ──────────────────────────────────────────────────────────────
  function fmtSize(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes < 1024) return bytes + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let n = bytes / 1024, i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + u[i];
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // Candidate cell: group sessions (e.g. Advanced-Training) carry every attendee
  // in r.candidates. Show the attendee(s) that matched the search — or the first
  // two — plus a clickable "👥" chip that opens the full who-joined list.
  function candidateCell(r) {
    const cands = (r.candidates && r.candidates.length) ? r.candidates : [r.candidate];
    if (cands.length === 1) return esc(cands[0]);
    const matched = (r.matched_candidates || []).filter((c) => cands.indexOf(c) !== -1);
    const shown = matched.length ? matched : cands.slice(0, 2);
    const extra = cands.length - shown.length;
    const label = extra > 0 ? `+${extra} more` : `${cands.length} joined`;
    return `<span>${shown.map(esc).join(", ")}</span> ` +
      `<button type="button" class="group-more" title="Show all ${cands.length} attendees">👥 ${esc(label)}</button>`;
  }

  // Attendee popup: click the 👥 chip to see everyone who joined the session
  // (reuses the preview modal — close button / backdrop / Esc already wired).
  function openAttendees(rec) {
    if (!rec) return;
    const cands = (rec.candidates && rec.candidates.length) ? rec.candidates : [rec.candidate];
    const matched = new Set(rec.matched_candidates || []);
    previewTitle.textContent =
      `${cands.length} attendees joined` + (rec.meeting_id ? ` · Meeting ${rec.meeting_id}` : "");
    previewBody.innerHTML = `<ol class="attendee-list">` + cands.map((c) =>
      `<li${matched.has(c) ? ' class="matched"' : ""}>${esc(c)}` +
      (matched.has(c) ? ' <span class="attendee-hit">matched your search</span>' : "") +
      `</li>`).join("") + `</ol>`;
    previewModal.style.display = "flex";
  }

  function showNotice(msg, kind) {
    notice.textContent = msg;
    notice.className = "notice show " + (kind === "ok" ? "notice-ok" : "notice-error");
  }
  function clearNotice() { notice.className = "notice"; }

  function showEmpty(msg, icon) {
    resultsArea.innerHTML =
      `<div class="empty"><div class="big">${icon || "🔍"}</div>${esc(msg)}</div>`;
  }

  function showLoading() {
    resultsArea.innerHTML =
      '<div class="empty"><div class="spinner spinner-lg"></div>Searching…</div>';
  }

  function setCacheInfo(cache) {
    if (!cache) return;
    const el = $("cache-info");
    el.className = "cache-info";
    if (cache.demo) { el.textContent = `${cache.count} demo records`; return; }
    // A failed build must not masquerade as a slow one: without this the page
    // shows "indexing bucket…" and an empty Host list indefinitely.
    if (cache.error) {
      el.className = "cache-info cache-error";
      el.textContent = cache.ready ? "index may be stale" : "index unavailable";
      return;
    }
    if (!cache.ready) { el.textContent = "indexing bucket…"; return; }
    const age = cache.age_sec == null ? "—" : `${cache.age_sec}s ago`;
    el.textContent = `${cache.count} files · indexed ${age}`;
  }

  function fillSelect(sel, values, allLabel) {
    sel.innerHTML = `<option value="">${allLabel}</option>` +
      values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  }

  // ── load filter dropdowns (hosts + the user's allowed departments)
  async function loadFilters() {
    try {
      const resp = await fetch("/api/filters");
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Could not load filters.", "error"); return; }
      canDownload = data.can_download !== false;
      allowedDepartments = data.departments || [];
      allHosts = data.hosts || [];
      hostsByDepartment = data.hosts_by_department || {};
      populateDepartments(allowedDepartments);
      refreshHostOptions();   // scope Host to the (possibly preselected) department

      setCacheInfo(data.cache);
      const cache = data.cache || {};
      if (cache.error) {
        // The Host dropdown is empty because the index could not be built — say
        // so loudly, and stop the quiet retry loop: this needs a human (usually
        // refreshed AWS credentials), not another poll.
        showNotice("Recordings can't be listed: " + cache.error +
                   " Hosts and search results stay empty until this is fixed — " +
                   "then use ↻ Refresh index.", "error");
      } else if (cache.ready === false) {
        // On a cold boot the index is still warming; hosts arrive on a quiet retry.
        setTimeout(loadFilters, 4000);
      }
    } catch (e) {
      showNotice("Network error loading filters — is the server running?", "error");
    }
  }

  // The Department control reflects access: hidden if none, locked to the single
  // department a user is scoped to, or an "All / pick one" dropdown otherwise.
  function populateDepartments(depts) {
    const field = $("dept-field");
    const sel = $("f-dept");
    if (!depts || depts.length === 0) { field.style.display = "none"; return; }
    field.style.display = "";
    if (depts.length === 1) {
      sel.innerHTML = `<option value="${esc(depts[0])}">${esc(depts[0])}</option>`;
      sel.value = depts[0];
      sel.disabled = true;
    } else {
      sel.disabled = false;
      sel.innerHTML = `<option value="">All departments</option>` +
        depts.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join("");
    }
  }

  // Host options follow the chosen department: a specific department shows only
  // its hosts; "All departments" shows the union across the user's departments.
  function refreshHostOptions() {
    const deptSel = $("f-dept");
    const dept = deptSel ? deptSel.value : "";
    const prev = $("f-host").value;
    const hosts = (dept && hostsByDepartment[dept]) ? hostsByDepartment[dept] : allHosts;
    fillSelect($("f-host"), hosts, "All hosts");
    $("f-host").value = (prev && hosts.indexOf(prev) !== -1) ? prev : "";  // keep if still valid
  }

  // ── search ───────────────────────────────────────────────────────────────
  async function runSearch(e) {
    if (e) e.preventDefault();
    page = 1;
    selected.clear();          // a NEW query starts a fresh selection
    await executeSearch();
  }

  function gotoPage(p) {
    page = p;
    executeSearch(true);       // paging keeps the cross-page selection
  }

  async function executeSearch(keepSelection) {
    clearNotice();

    const deptSel = $("f-dept");
    const deptVal = deptSel ? deptSel.value : "";
    // A calendar selection travels as an explicit from/to range; anything else the
    // user typed ("2026-06" for a whole month) stays a free-text date filter.
    const picked = datePicker.get();
    const hasRange = !!(picked.from || picked.to);
    const types = fileTypes.get();

    const params = new URLSearchParams({
      candidate: $("f-candidate").value,
      company: $("f-company").value,
      host: $("f-host").value,
      date: hasRange ? "" : picked.raw,
      date_from: picked.from,
      date_to: picked.to,
      meeting_id: $("f-meeting").value,
      file_type: types.join(","),
      department: deptVal,
      page: String(page),
      per_page: $("per-page").value,
      sort: $("sort-by").value,
    });

    // Empty-query guard: a blank search never hits S3 or serialises the bucket.
    // A forced single-department (disabled select) is the user's access mask, not
    // a query, so it does NOT count — otherwise a blank submit would dump the whole
    // department. A user who actively picks a department (multi-dept) does count.
    const deptIsQuery = deptSel && !deptSel.disabled && deptVal.trim() !== "";
    const otherFilters =
      ["f-candidate", "f-company", "f-host", "f-meeting"].some((id) => $(id).value.trim() !== "") ||
      hasRange || picked.raw.trim() !== "" || types.length > 0;
    if (!otherFilters && !deptIsQuery) {
      summary.innerHTML = "";
      selected.clear(); currentRows = new Map(); updateBulkBar();
      hidePagination();
      showEmpty("Enter a candidate, company, date, meeting ID, or pick a type, then Search.");
      return;
    }

    const seq = ++searchSeq;
    const btn = $("btn-search");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Searching…';
    showLoading();

    try {
      const resp = await fetch("/api/search?" + params.toString());
      if (seq !== searchSeq) return;                 // a newer search superseded this one
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json();
      if (seq !== searchSeq) return;
      if (!resp.ok) {
        showNotice(data.error || "Search failed.", "error");
        showEmpty("Search failed — see the message above.", "⚠️");
        hidePagination();
        return;
      }
      // Out-of-range page (e.g. per-page grew, or the index shrank) — snap back.
      if ((data.results || []).length === 0 && data.total > 0 && page > 1) {
        page = 1;
        return executeSearch(keepSelection);
      }
      renderResults(data, keepSelection);
      renderPagination(data);
    } catch (err) {
      if (seq !== searchSeq) return;
      showNotice("Network error during search.", "error");
      showEmpty("Could not reach the server.", "⚠️");
      hidePagination();
    } finally {
      if (seq === searchSeq) {
        btn.disabled = false;
        btn.textContent = "Search";
      }
    }
  }

  function renderResults(data, keepSelection) {
    const rows = data.results || [];
    lastData = data;                  // a timezone switch re-renders from this
    // The server is the source of truth for permission on every response.
    if (typeof data.can_download === "boolean") canDownload = data.can_download;
    if (!keepSelection) selected.clear();
    currentRows = new Map(rows.map((r) => [r.key, r]));
    updateBulkBar();

    const total = data.total != null ? data.total : data.count;
    const cur = data.page || 1;
    const per = data.per_page || rows.length || 1;
    const start = total === 0 ? 0 : (cur - 1) * per + 1;
    const end = total === 0 ? 0 : start + rows.length - 1;
    summary.innerHTML =
      `<strong>${total.toLocaleString()}</strong> file${total === 1 ? "" : "s"} · ${fmtSize(data.total_size)} total` +
      (total > rows.length ? ` <span class="trunc-note">showing ${start.toLocaleString()}–${end.toLocaleString()}</span>` : "") +
      (canDownload ? "" : ` <span class="badge badge-muted">View-only</span>`);

    if (rows.length === 0) {
      showEmpty("No recordings match those filters.", "📭");
      return;
    }

    const body = rows.map((r) => {
      const cat = r.category || "other";
      const dl = "/api/download?key=" + encodeURIComponent(r.key);
      // Download is per RECORD: a meeting shared with this account view-only sits
      // next to departments it may download from. The server decides; this only
      // stops the UI from offering a button that would 403.
      const rowDl = rowDownloadable(r);
      const checkCell = canDownload
        ? (rowDl
            ? `<td class="col-check"><input type="checkbox" class="row-check"${selected.has(r.key) ? " checked" : ""} aria-label="Select"></td>`
            : `<td class="col-check"><span class="cell-muted" title="Shared with you as view-only — cannot be added to a zip">–</span></td>`)
        : "";
      const actionCell = `<td class="col-actions">` +
        `<button class="btn btn-ghost btn-sm view-btn" type="button" title="View in browser">▶ View</button>` +
        (canDownload
          ? (rowDl
              ? ` <a class="btn btn-ghost btn-sm" href="${dl}" title="Download">⬇</a>`
              : ` <span class="badge badge-muted" title="This recording was shared with you as view-only">view only</span>`)
          : "") +
        `</td>`;
      return `<tr data-key="${esc(r.key)}">
        ${checkCell}
        <td class="col-clip" title="${esc(r.department)}">${esc(r.department)}</td>
        <td class="col-clip" title="${esc(r.host)}">${esc(r.host)}</td>
        <td class="candidate">${candidateCell(r)}</td>
        <td class="col-clip" title="${esc(r.company)}">${esc(r.company)}</td>
        <td class="col-date">${dateCell(r)}</td>
        <td class="col-time">${timeCell(r)}</td>
        <td class="col-clip" title="${esc(r.round)}">${esc(r.round)}</td>
        <td>${esc(r.meeting_id)}</td>
        <td><span class="ft-tag ft-${esc(cat)}">${esc(CATEGORY_SHORT[cat] || cat)}</span></td>
        <td class="wrap">${esc(r.filename)}</td>
        <td>${fmtSize(r.size)}</td>
        ${actionCell}
      </tr>`;
    }).join("");

    const checkHead = canDownload
      ? `<th class="col-check"><input type="checkbox" id="check-all" aria-label="Select all"></th>` : "";
    // The zone lives in the header so a converted column is never mistaken for
    // the raw IST value stored in S3.
    const zoneTag = `<span class="th-zone">${esc(zone().label)}</span>`;

    resultsArea.innerHTML = `
      <div class="table-wrap">
        <table class="results">
          <thead>
            <tr>
              ${checkHead}
              <th>Department</th><th>Host</th><th>Candidate</th><th>Company</th>
              <th>Date ${zoneTag}</th><th>Time ${zoneTag}</th><th>Round</th>
              <th>Meeting ID</th><th>Type</th><th>File</th><th>Size</th><th></th>
            </tr>
          </thead>
          <tbody>${body}</tbody>
        </table>
      </div>`;

    resultsArea.querySelectorAll(".view-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.closest("tr").dataset.key;
        openPreview(currentRows.get(key));
      });
    });
    resultsArea.querySelectorAll(".group-more").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.closest("tr").dataset.key;
        openAttendees(currentRows.get(key));
      });
    });
    if (canDownload) {
      resultsArea.querySelectorAll(".row-check").forEach((cb) => {
        cb.addEventListener("change", onRowToggle);
      });
      const all = $("check-all");
      if (all) {
        all.addEventListener("change", onCheckAll);
        all.checked = pageFullySelected();
      }
    }
  }

  // A row the server marked view-only carries no checkbox, so "select all" and
  // the all-selected state must both ignore it — otherwise the header checkbox
  // could never settle on checked.
  function rowDownloadable(r) {
    return canDownload && r && r.can_download !== false;
  }

  function selectableKeys() {
    const keys = [];
    currentRows.forEach((r, k) => { if (rowDownloadable(r)) keys.push(k); });
    return keys;
  }

  function pageFullySelected() {
    const keys = selectableKeys();
    return keys.length > 0 && keys.every((k) => selected.has(k));
  }

  function onRowToggle(e) {
    const key = e.target.closest("tr").dataset.key;
    const r = currentRows.get(key);
    if (e.target.checked) selected.set(key, r ? Number(r.size) || 0 : 0);
    else selected.delete(key);
    const all = $("check-all");
    if (all) all.checked = pageFullySelected();
    updateBulkBar();
  }

  function onCheckAll(e) {
    const on = e.target.checked;
    resultsArea.querySelectorAll("tr[data-key]").forEach((tr) => {
      const cb = tr.querySelector(".row-check");
      if (!cb) return;                    // view-only row: nothing to select
      cb.checked = on;
      const r = currentRows.get(tr.dataset.key);
      if (on) selected.set(tr.dataset.key, r ? Number(r.size) || 0 : 0);
      else selected.delete(tr.dataset.key);
    });
    updateBulkBar();
  }

  function updateBulkBar() {
    if (selected.size === 0) { bulkBar.style.display = "none"; return; }
    bulkBar.style.display = "flex";
    bulkCount.textContent = `${selected.size} selected`;
    let total = 0;
    selected.forEach((size) => { total += size; });   // sizes cached at select time
    bulkSize.textContent = fmtSize(total);
  }

  // ── bulk zip download ────────────────────────────────────────────────────
  async function downloadZip() {
    if (selected.size === 0) return;
    const btn = $("btn-zip");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Building zip…';
    clearNotice();
    try {
      const resp = await fetch("/api/download/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: Array.from(selected.keys()) }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        showNotice(data.error || "Could not build the zip.", "error");
        return;
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "interview-recordings.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showNotice(`Downloaded ${selected.size} file(s) as a zip.`, "ok");
    } catch (err) {
      showNotice("Network error while downloading the zip.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "⬇ Download selected (.zip)";
    }
  }

  // ── refresh index ────────────────────────────────────────────────────────
  async function refreshIndex() {
    const btn = $("btn-refresh");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Refreshing…';
    try {
      const resp = await fetch("/api/refresh", { method: "POST" });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Refresh failed.", "error"); return; }
      setCacheInfo(data.cache);
      await loadFilters();
      showNotice("Index refreshed.", "ok");
    } catch (e) {
      showNotice("Network error during refresh.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "↻ Refresh index";
    }
  }

  function clearFilters() {
    ["f-candidate", "f-company", "f-meeting", "f-host"].forEach((id) => ($(id).value = ""));
    datePicker.clear();
    fileTypes.clear();
    // Only reset the department when the user can actually change it (multi-dept);
    // a single-department user stays scoped to their one department.
    const deptSel = $("f-dept");
    if (deptSel && !deptSel.disabled) deptSel.value = "";
    refreshHostOptions();   // department reset -> Host back to the full union
    page = 1;
    selected.clear();
    currentRows = new Map();
    summary.innerHTML = "";
    clearNotice();
    updateBulkBar();
    hidePagination();
    showEmpty("Run a search to see recordings.");
  }

  // ── pagination bar ───────────────────────────────────────────────────────
  function hidePagination() {
    const el = $("pagination");
    el.style.display = "none";
    el.innerHTML = "";
  }

  function renderPagination(data) {
    const el = $("pagination");
    const pages = data.pages || 1;
    const cur = data.page || 1;
    if (!data.total || pages <= 1) { hidePagination(); return; }

    const btn = (p, label, disabled, active) =>
      `<button type="button" class="btn btn-ghost btn-sm page-btn${active ? " active" : ""}"` +
      ` data-page="${p}"${disabled ? " disabled" : ""}>${label}</button>`;

    // Window of page numbers: 1, 2 … cur±2 … last-1, last (deduped, in order).
    const win = [];
    const add = (p) => { if (p >= 1 && p <= pages && win.indexOf(p) === -1) win.push(p); };
    add(1); add(2);
    for (let p = cur - 2; p <= cur + 2; p++) add(p);
    add(pages - 1); add(pages);
    win.sort((a, b) => a - b);

    const parts = [btn(cur - 1, "‹ Prev", cur <= 1, false)];
    let prev = 0;
    win.forEach((p) => {
      if (p - prev > 1) parts.push('<span class="page-ellipsis">…</span>');
      parts.push(btn(p, String(p), false, p === cur));
      prev = p;
    });
    parts.push(btn(cur + 1, "Next ›", cur >= pages, false));
    parts.push(`<span class="page-info">Page ${cur.toLocaleString()} of ${pages.toLocaleString()}</span>`);

    el.innerHTML = parts.join("");
    el.style.display = "flex";
    el.querySelectorAll(".page-btn").forEach((b) => {
      b.addEventListener("click", () => {
        const p = parseInt(b.dataset.page, 10);
        if (p >= 1 && p <= pages && p !== cur) gotoPage(p);
      });
    });
  }

  // ── preview (view-in-browser, no download) ─────────────────────────────────
  const previewModal = $("preview-modal");
  const previewBody = $("preview-body");
  const previewTitle = $("preview-title");

  function viewUrl(key) { return "/api/view?key=" + encodeURIComponent(key); }

  function openPreview(rec) {
    if (!rec) return;
    const url = viewUrl(rec.key);
    const cat = rec.category || "other";
    previewTitle.textContent = rec.filename || "Preview";
    previewBody.innerHTML = '<div class="empty"><div class="spinner spinner-lg"></div>Loading…</div>';
    previewModal.dataset.previewKey = rec.key || "";   // anti-capture.js reads this
    previewModal.style.display = "flex";

    if (cat === "video" || cat === "audio") {
      renderPlayer(rec, cat, url);
    } else if (cat === "questions" || rec.ext === "html") {
      // Render HTML in a sandboxed iframe (no scripts) so it can't touch the page.
      fetch(url).then((r) => r.text()).then((html) => {
        const f = document.createElement("iframe");
        f.className = "preview-frame";
        f.setAttribute("sandbox", "");
        f.srcdoc = html;
        previewBody.innerHTML = "";
        previewBody.appendChild(f);
      }).catch(() => { previewBody.innerHTML = previewError(); });
    } else if (["transcript", "chat", "summary", "notes"].includes(cat) ||
               ["vtt", "txt"].includes(rec.ext)) {
      fetch(url).then((r) => r.text()).then((txt) => {
        previewBody.innerHTML = `<pre class="preview-text"></pre>`;
        previewBody.querySelector("pre").textContent = txt;
      }).catch(() => { previewBody.innerHTML = previewError(); });
    } else {
      previewBody.innerHTML =
        `<div class="empty"><div class="big">📄</div>This file type can’t be previewed in the browser.</div>`;
    }
  }

  function previewError() {
    return `<div class="empty"><div class="big">⚠️</div>Couldn’t load this file for preview.</div>`;
  }

  // ── player: playback speed + captions ─────────────────────────────────────
  // Both preferences are remembered, because someone reviewing a stack of
  // interviews at 1.5× with captions on wants that on every recording, not once.
  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
  const RATE_KEY = "portal.playbackRate";
  const CC_KEY = "portal.captions";

  function storedRate() {
    const value = parseFloat(readPref(RATE_KEY));
    return SPEEDS.indexOf(value) !== -1 ? value : 1;
  }

  function renderPlayer(rec, cat, url) {
    const media = cat === "video"
      // "noplaybackrate" is deliberately NOT set: the native speed menu and the
      // buttons below stay in sync, so either way of changing speed works.
      ? `<video id="preview-player" class="preview-media" controls autoplay playsinline ` +
        `controlslist="nodownload" disablepictureinpicture oncontextmenu="return false" ` +
        `src="${esc(url)}"></video>`
      : `<audio id="preview-player" class="preview-media" controls autoplay ` +
        `controlslist="nodownload" src="${esc(url)}"></audio>`;

    previewBody.innerHTML = media +
      `<div class="player-bar">
         <div class="player-group">
           <span class="player-label">Speed</span>
           <div class="player-btns" id="speed-btns">` +
             SPEEDS.map((s) =>
               `<button type="button" class="player-btn" data-rate="${s}">${s}×</button>`).join("") +
           `</div>
         </div>
         <div class="player-group" id="cc-group" hidden>
           <span class="player-label">Captions</span>
           <div class="player-btns" id="cc-btns"></div>
         </div>
       </div>`;

    const player = $("preview-player");
    wireSpeed(player);
    if (cat === "video") loadCaptions(player, rec);
  }

  function syncSpeedButtons(rate) {
    const box = $("speed-btns");
    if (!box) return;
    box.querySelectorAll(".player-btn").forEach((b) => {
      b.classList.toggle("active", Math.abs(parseFloat(b.dataset.rate) - rate) < 0.001);
    });
  }

  function wireSpeed(player) {
    const box = $("speed-btns");
    box.querySelectorAll(".player-btn").forEach((b) => {
      b.addEventListener("click", () => {
        const rate = parseFloat(b.dataset.rate);
        player.playbackRate = rate;
        writePref(RATE_KEY, String(rate));
      });
    });
    player.playbackRate = storedRate();
    syncSpeedButtons(player.playbackRate);
    // Browsers reset the rate when new media metadata arrives, and the native
    // controls can change it behind our back — re-apply, then follow along.
    player.addEventListener("loadedmetadata", () => { player.playbackRate = storedRate(); });
    player.addEventListener("ratechange", () => {
      writePref(RATE_KEY, String(player.playbackRate));
      syncSpeedButtons(player.playbackRate);
    });
  }

  async function loadCaptions(player, rec) {
    let tracks = [];
    try {
      const resp = await fetch("/api/captions?key=" + encodeURIComponent(rec.key));
      if (!resp.ok) return;                        // no captions is not an error worth showing
      tracks = (await resp.json()).tracks || [];
    } catch (e) {
      return;
    }
    // The modal may have been closed or moved on to another file while the
    // lookup was in flight — never attach tracks to a player that is gone.
    if (!tracks.length || $("preview-player") !== player) return;

    const name = (t, i) => (tracks.length > 1 ? `${t.label} ${i + 1}` : t.label);
    const box = $("cc-btns");
    box.innerHTML = `<button type="button" class="player-btn" data-track="-1">Off</button>` +
      tracks.map((t, i) =>
        `<button type="button" class="player-btn" data-track="${i}">${esc(name(t, i))}</button>`).join("");
    $("cc-group").hidden = false;

    // textTracks is indexed in the order the <track> elements are appended, so a
    // button's data-track index addresses the track it names.
    const select = (index) => {
      for (let i = 0; i < player.textTracks.length; i++) {
        player.textTracks[i].mode = i === index ? "showing" : "disabled";
      }
      writePref(CC_KEY, index >= 0 ? "on" : "off");
      box.querySelectorAll(".player-btn").forEach((b) => {
        b.classList.toggle("active", parseInt(b.dataset.track, 10) === index);
      });
    };

    tracks.forEach((t, i) => {
      const el = document.createElement("track");
      el.kind = "subtitles";
      el.label = name(t, i);
      el.srclang = "en";
      el.src = t.src;
      // A caption file that will not load (unreadable object, oversized transcript
      // served straight from S3) must not leave behind a button that does nothing.
      el.addEventListener("error", () => {
        const btn = box.querySelector(`.player-btn[data-track="${i}"]`);
        if (!btn) return;
        const wasActive = btn.classList.contains("active");
        btn.disabled = true;
        btn.title = "This caption file could not be loaded.";
        if (wasActive) select(-1);
      });
      player.appendChild(el);
    });

    box.querySelectorAll(".player-btn").forEach((b) => {
      b.addEventListener("click", () => select(parseInt(b.dataset.track, 10)));
    });
    select(readPref(CC_KEY) === "on" ? 0 : -1);
  }

  function closePreview() {
    previewModal.style.display = "none";
    previewModal.dataset.previewKey = "";
    previewBody.innerHTML = "";   // stop any playing media
  }

  $("preview-close").addEventListener("click", closePreview);
  previewModal.addEventListener("click", (e) => { if (e.target === previewModal) closePreview(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && previewModal.style.display !== "none") closePreview();
  });

  // ── wire up ──────────────────────────────────────────────────────────────
  // Multi-select file type: several categories in one search ("video,audio").
  const fileTypes = window.MultiSelect({
    mount: $("filetype-mount"),
    id: "f-filetype",
    options: FILE_TYPES,
    allLabel: "All types",
    noun: "types",
  });
  // Interactive calendar: one day, a range, or free text like "2026-06".
  const datePicker = window.DateRangePicker({ input: $("f-date") });

  const tzSelect = $("tz-select");
  if (tzSelect) {
    tzSelect.value = tzKey;
    tzSelect.addEventListener("change", () => {
      tzKey = ZONES[tzSelect.value] ? tzSelect.value : "IST";
      writePref(TZ_KEY, tzKey);
      updateZoneHint();
      // Purely a display change — re-render the rows we already have instead of
      // asking the server for the same page again.
      if (lastData) renderResults(lastData, true);
    });
  }
  updateZoneHint();

  $("search-form").addEventListener("submit", runSearch);
  $("f-dept").addEventListener("change", refreshHostOptions);
  // Re-run from page 1 when sort/page-size changes; selection survives (same set).
  $("sort-by").addEventListener("change", () => { page = 1; executeSearch(true); });
  $("per-page").addEventListener("change", () => { page = 1; executeSearch(true); });
  $("btn-clear").addEventListener("click", clearFilters);
  $("btn-refresh").addEventListener("click", refreshIndex);
  $("btn-zip").addEventListener("click", downloadZip);
  $("btn-deselect").addEventListener("click", () => {
    selected.clear();
    resultsArea.querySelectorAll(".row-check, #check-all").forEach((cb) => (cb.checked = false));
    updateBulkBar();
  });

  // Populate the host dropdown (non-blocking on the server). NO auto-search:
  // the page stays on its empty state until the user submits a query.
  loadFilters();
})();
