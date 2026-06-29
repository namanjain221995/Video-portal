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
  // set of selected keys
  const selected = new Set();
  // monotonic id so a slow/old response can never clobber a newer search
  let searchSeq = 0;

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
        return;
      }
      renderResults(data);
    } catch (err) {
      if (seq !== searchSeq) return;
      showNotice("Network error during search.", "error");
      showEmpty("Could not reach the server.", "⚠️");
    } finally {
      if (seq === searchSeq) {
        btn.disabled = false;
        btn.textContent = "Search";
      }
    }
  }

  function renderResults(data) {
    const rows = data.results || [];
    // The server is the source of truth for permission on every response.
    if (typeof data.can_download === "boolean") canDownload = data.can_download;
    selected.clear();
    currentRows = new Map(rows.map((r) => [r.key, r]));
    updateBulkBar();

    const total = data.total != null ? data.total : data.count;
    summary.innerHTML =
      `<strong>${total}</strong> file${total === 1 ? "" : "s"} · ${fmtSize(data.total_size)} total` +
      (data.truncated ? ` <span class="trunc-note">(showing first ${data.count})</span>` : "") +
      (canDownload ? "" : ` <span class="badge badge-muted">View-only</span>`);

    if (rows.length === 0) {
      showEmpty("No recordings match those filters.", "📭");
      return;
    }

    const body = rows.map((r) => {
      const cat = r.category || "other";
      const dl = "/api/download?key=" + encodeURIComponent(r.key);
      const checkCell = canDownload
        ? `<td class="col-check"><input type="checkbox" class="row-check" aria-label="Select"></td>` : "";
      const actionCell = `<td class="col-actions">` +
        `<button class="btn btn-ghost btn-sm view-btn" type="button" title="View in browser">▶ View</button>` +
        (canDownload ? ` <a class="btn btn-ghost btn-sm" href="${dl}" title="Download">⬇</a>` : "") +
        `</td>`;
      return `<tr data-key="${esc(r.key)}">
        ${checkCell}
        <td>${esc(r.department)}</td>
        <td class="candidate">${esc(r.candidate)}</td>
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
              <th>Department</th><th>Candidate</th><th>Company</th><th>Date</th><th>Round</th>
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
    if (canDownload) {
      resultsArea.querySelectorAll(".row-check").forEach((cb) => {
        cb.addEventListener("change", onRowToggle);
      });
      const all = $("check-all");
      if (all) all.addEventListener("change", onCheckAll);
    }
  }

  function onRowToggle(e) {
    const key = e.target.closest("tr").dataset.key;
    if (e.target.checked) selected.add(key); else selected.delete(key);
    const all = $("check-all");
    if (all) all.checked = selected.size === currentRows.size && currentRows.size > 0;
    updateBulkBar();
  }

  function onCheckAll(e) {
    const on = e.target.checked;
    resultsArea.querySelectorAll("tr[data-key]").forEach((tr) => {
      const cb = tr.querySelector(".row-check");
      cb.checked = on;
      if (on) selected.add(tr.dataset.key); else selected.delete(tr.dataset.key);
    });
    updateBulkBar();
  }

  function updateBulkBar() {
    if (selected.size === 0) { bulkBar.style.display = "none"; return; }
    bulkBar.style.display = "flex";
    bulkCount.textContent = `${selected.size} selected`;
    let total = 0;
    selected.forEach((k) => { const r = currentRows.get(k); if (r) total += Number(r.size) || 0; });
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
        body: JSON.stringify({ keys: Array.from(selected) }),
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
    selected.clear();
    currentRows = new Map();
    summary.innerHTML = "";
    clearNotice();
    updateBulkBar();
    showEmpty("Run a search to see recordings.");
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
