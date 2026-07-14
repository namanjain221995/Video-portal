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

  // key -> record, for the rows currently rendered
  let currentRows = new Map();
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
    if (cache.demo) { el.textContent = `${cache.count} demo records`; return; }
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
      // On a cold boot the index is still warming; hosts arrive on a quiet retry.
      if (data.cache && data.cache.ready === false) setTimeout(loadFilters, 4000);
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

    const params = new URLSearchParams({
      candidate: $("f-candidate").value,
      company: $("f-company").value,
      host: $("f-host").value,
      date: $("f-date").value,
      meeting_id: $("f-meeting").value,
      file_type: $("f-filetype").value,
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
    const otherFilters = ["f-candidate", "f-company", "f-host", "f-date", "f-meeting", "f-filetype"]
      .some((id) => $(id).value.trim() !== "");
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
      const checkCell = canDownload
        ? `<td class="col-check"><input type="checkbox" class="row-check"${selected.has(r.key) ? " checked" : ""} aria-label="Select"></td>` : "";
      const actionCell = `<td class="col-actions">` +
        `<button class="btn btn-ghost btn-sm view-btn" type="button" title="View in browser">▶ View</button>` +
        (canDownload ? ` <a class="btn btn-ghost btn-sm" href="${dl}" title="Download">⬇</a>` : "") +
        `</td>`;
      return `<tr data-key="${esc(r.key)}">
        ${checkCell}
        <td>${esc(r.department)}</td>
        <td>${esc(r.host)}</td>
        <td class="candidate">${candidateCell(r)}</td>
        <td>${esc(r.company)}</td>
        <td>${esc(r.date)}</td>
        <td>${esc(r.round)}</td>
        <td>${esc(r.meeting_id)}</td>
        <td><span class="ft-tag ft-${esc(cat)}">${esc(CATEGORY_SHORT[cat] || cat)}</span></td>
        <td class="wrap">${esc(r.filename)}</td>
        <td>${fmtSize(r.size)}</td>
        ${actionCell}
      </tr>`;
    }).join("");

    const checkHead = canDownload
      ? `<th class="col-check"><input type="checkbox" id="check-all" aria-label="Select all"></th>` : "";

    resultsArea.innerHTML = `
      <div class="table-wrap">
        <table class="results">
          <thead>
            <tr>
              ${checkHead}
              <th>Department</th><th>Host</th><th>Candidate</th><th>Company</th><th>Date</th><th>Round</th>
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

  function pageFullySelected() {
    if (currentRows.size === 0) return false;
    let allIn = true;
    currentRows.forEach((r, k) => { if (!selected.has(k)) allIn = false; });
    return allIn;
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
    ["f-candidate", "f-company", "f-date", "f-meeting", "f-host", "f-filetype"]
      .forEach((id) => ($(id).value = ""));
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

    if (cat === "video") {
      previewBody.innerHTML =
        `<video class="preview-media" controls autoplay playsinline controlslist="nodownload noplaybackrate" ` +
        `disablepictureinpicture oncontextmenu="return false" src="${esc(url)}"></video>`;
    } else if (cat === "audio") {
      previewBody.innerHTML =
        `<audio class="preview-media" controls autoplay controlslist="nodownload" src="${esc(url)}"></audio>`;
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
