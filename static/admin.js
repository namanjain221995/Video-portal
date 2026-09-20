/* admin.js — lists/creates/deletes users and manages per-user department +
   download access via /api/admin/users. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const notice = $("notice");

  // All departments the bucket exposes (from the server). Used to render the
  // checkbox grids both on the create form and on every user row.
  let allDepartments = [];
  // department -> [hosts] from the S3 index; drives the per-department host
  // pickers (empty while the index is still warming).
  let hostsByDept = {};

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showNotice(msg, kind) {
    notice.textContent = msg;
    notice.className = "notice show " + (kind === "ok" ? "notice-ok" : "notice-error");
  }

  // Build a set of department checkboxes inside `container`, ticking `selected`.
  function renderDeptChecks(container, selected) {
    const chosen = new Set(selected || []);
    if (!allDepartments.length) {
      container.innerHTML = '<span class="user-meta">No departments configured.</span>';
      return;
    }
    container.innerHTML = allDepartments.map((d) => `
      <label class="dept-check">
        <input type="checkbox" value="${esc(d)}" ${chosen.has(d) ? "checked" : ""}>
        <span>${esc(d)}</span>
      </label>`).join("");
  }

  function readDeptChecks(container) {
    return Array.from(container.querySelectorAll('input[type="checkbox"]:checked'))
      .map((cb) => cb.value);
  }

  // Per-department host pickers: one collapsible box per SELECTED department.
  // Ticking hosts LIMITS the user to them; all unticked = the whole department.
  function renderHostBoxes(container, depts, selectedHosts) {
    selectedHosts = selectedHosts || {};
    if (!depts || !depts.length) {
      container.innerHTML =
        '<span class="user-meta">Select a department above to restrict its hosts.</span>';
      return;
    }
    container.innerHTML = depts.map((d) => {
      const hosts = hostsByDept[d] || [];
      const chosen = new Set(selectedHosts[d] || []);
      // Union: granted hosts missing from the (possibly still-warming) index stay
      // visible and ticked, so saving never silently drops an existing restriction.
      const display = hosts.slice();
      chosen.forEach((h) => { if (display.indexOf(h) === -1) display.push(h); });
      const inner = display.length
        ? display.map((h) => `
            <label class="dept-check">
              <input type="checkbox" value="${esc(h)}"${chosen.has(h) ? " checked" : ""}>
              <span>${esc(h)}</span>
            </label>`).join("")
        : '<span class="user-meta">No hosts indexed yet for this department.</span>';
      const n = chosen.size;
      const sum = n ? `${n} host${n === 1 ? "" : "s"} only` : "All hosts";
      return `
        <details class="host-box" data-dept="${esc(d)}"${n ? " open" : ""}>
          <summary>${esc(d)} — <span class="host-sum">${esc(sum)}</span></summary>
          <p class="user-meta host-hint">Tick hosts to limit this user to them; leave all unticked for the whole department.</p>
          <div class="host-list dept-checks">${inner}</div>
        </details>`;
    }).join("");
    // Live "N hosts only / All hosts" summary as boxes are ticked.
    container.querySelectorAll(".host-box").forEach((det) => {
      det.addEventListener("change", () => {
        const n = det.querySelectorAll("input:checked").length;
        det.querySelector(".host-sum").textContent =
          n ? `${n} host${n === 1 ? "" : "s"} only` : "All hosts";
      });
    });
  }

  function readHostChecks(container) {
    const out = {};
    container.querySelectorAll(".host-box").forEach((det) => {
      const picked = Array.from(det.querySelectorAll("input:checked")).map((cb) => cb.value);
      if (picked.length) out[det.dataset.dept] = picked;
    });
    return out;
  }

  // ── Individually shared meetings ───────────────────────────────────────────
  // A department grant is all-or-nothing; this shares ONE meeting (every file of
  // it) with someone who should not get the whole department, with its own
  // view/download choice. Grants are stored as { meeting_id, can_download }.
  //
  // The box takes EITHER a free-text search ("akhilendra", "2026-07") or a whole
  // pasted LIST of meeting ids. It has to take the list: admins copy a column of
  // ids out of a spreadsheet and paste the lot, and before this the widget sent
  // the entire "9460…, 9349…, 9145…" string to the substring search, which can
  // never match one record — so the panel said "No meetings match that" and the
  // account was saved with no shared meetings at all.

  // Split on SEPARATORS ONLY (see _MEETING_ID_SPLIT_RE in app.py, which must stay
  // in step with this): newlines and tabs from a spreadsheet, commas and spaces
  // from a message, semicolons from a mail client, and the fullwidth comma /
  // ideographic space a copy made in some locales carries. Tokens are kept whole
  // so a typo'd id comes back as one rejected token instead of being cut into two
  // valid-looking ones that would share the wrong meetings.
  // \s misses the zero-width space, word joiner and BOM a copy out of Slack,
  // Notion or Word carries, and those would fuse two ids into one dead token.
  const MG_SPLIT_RE = /[\s,;|\uff0c\u3000\u200b\ufeff\u2060]+/;
  // Wrapping punctuation a copied cell brings with it ("94601720227"). Only the
  // OUTSIDE of a token is trimmed, never its interior, so a typo'd id still comes
  // back whole and rejected instead of being quietly repaired into a real one.
  const MG_TRIM_RE = /^["'(\[<]+|["')\]>]+$/g;
  // Mirrors _MEETING_ID_RE in app.py: an S3 meeting folder is always all-digit.
  const MG_ID_RE = /^[0-9]{1,32}$/;

  function parseMeetingIds(text) {
    const ids = [], invalid = [], seen = new Set();
    String(text || "").trim().split(MG_SPLIT_RE).forEach((raw) => {
      const token = raw.replace(MG_TRIM_RE, "");
      if (!token) return;
      if (!MG_ID_RE.test(token)) {
        if (invalid.indexOf(token) === -1) invalid.push(token);
        return;
      }
      if (seen.has(token)) return;
      seen.add(token);
      ids.push(token);
    });
    return { ids, invalid };
  }

  // A LIST is anything carrying a separator — that is the paste this widget
  // exists for. A single word stays a free-text search, so "akhilendra", "2026-07"
  // and a lone meeting id all keep working exactly as they did before.
  function looksLikeList(text) {
    const trimmed = String(text || "").trim();
    if (MG_SPLIT_RE.test(trimmed)) return true;
    const parsed = parseMeetingIds(trimmed);
    return parsed.ids.length + parsed.invalid.length > 1;
  }

  function meetingLabel(detail, meetingId) {
    if (!detail) return "not in the current index";
    const who = (detail.candidates || []).slice(0, 2).join(", ");
    const when = (detail.dates || [])[0] || "";
    const dept = (detail.departments || []).join(", ");
    const bits = [who, when, dept].filter(Boolean);
    const files = `${detail.files} file${detail.files === 1 ? "" : "s"}`;
    return bits.join(" · ") + " · " + files;
  }

  // The per-user row shows its count in the <details> summary; keep that honest as
  // chips come and go instead of only at page load, or a row can sit there saying
  // "none" with six meetings visibly listed inside it.
  function syncMeetingSummary(box) {
    const details = box.closest(".mg-box");
    const sum = details && details.querySelector("summary .host-sum");
    if (!sum) return;                    // the create form has no <details>
    const n = (box._grants || []).length;
    sum.textContent = n ? `${n} meeting${n === 1 ? "" : "s"}` : "none";
  }

  // Add a batch of grants, skipping ids already shared, and report what happened.
  // Returning the counts rather than swallowing them is the point: an admin who
  // adds 8 and sees 6 chips needs to be told the other two were already there.
  function addGrants(box, entries) {
    const have = new Set((box._grants || []).map((g) => g.meeting_id));
    let added = 0, dupes = 0;
    entries.forEach((entry) => {
      if (have.has(entry.meeting_id)) { dupes++; return; }
      have.add(entry.meeting_id);
      // Default to view-only: sharing a meeting is usually about letting someone
      // WATCH it, and download is the choice that needs a deliberate tick.
      box._grants.push({ meeting_id: entry.meeting_id, can_download: false,
                         detail: entry.detail || null });
      added++;
    });
    if (added) renderChips(box);
    return { added, dupes };
  }

  function renderChips(box) {
    const grants = box._grants || [];
    syncMeetingSummary(box);
    if (!grants.length) {
      box.innerHTML = '<span class="user-meta">No individual meetings shared.</span>';
      return;
    }
    // A pasted batch makes this list long, so the per-meeting download choice
    // needs to be settable in one go rather than in forty separate clicks.
    const bulk = grants.length > 1 ? `
      <div class="mg-bulk-actions">
        <span class="user-meta"><strong>${grants.length}</strong> meetings shared</span>
        <button type="button" class="mg-bulk-btn" data-all-dl>Allow download on all</button>
        <button type="button" class="mg-bulk-btn" data-no-dl>Make all view only</button>
        <button type="button" class="mg-bulk-btn mg-bulk-danger" data-clear-all>Remove all</button>
      </div>` : "";
    box.innerHTML = bulk + grants.map((g, i) => {
      const detail = g.detail;
      // A recurring Zoom id is reused by every session booked under it, so
      // granting it shares them all. Say so rather than let it surprise anyone.
      const repeat = detail && detail.occurrences > 1
        ? `<span class="mg-warn" title="This meeting ID was reused across ${detail.occurrences} sessions — all of them are shared">⚠ ${detail.occurrences} sessions</span>`
        : "";
      return `
        <div class="mg-chip" data-i="${i}">
          <div class="mg-chip-main">
            <strong>${esc(g.meeting_id)}</strong> ${repeat}
            <span class="user-meta">${esc(meetingLabel(detail, g.meeting_id))}</span>
          </div>
          <label class="mg-dl" title="Allow downloading this meeting's files">
            <input type="checkbox" data-dl ${g.can_download ? "checked" : ""}><span>Download</span>
          </label>
          <button type="button" class="mg-remove" data-remove title="Stop sharing this meeting">✕</button>
        </div>`;
    }).join("");

    box.querySelectorAll(".mg-chip").forEach((chip) => {
      const i = Number(chip.dataset.i);
      chip.querySelector("[data-dl]").addEventListener("change", (e) => {
        box._grants[i].can_download = e.target.checked;
      });
      chip.querySelector("[data-remove]").addEventListener("click", () => {
        box._grants.splice(i, 1);
        renderChips(box);
      });
    });

    if (grants.length > 1) {
      const setAll = (value) => {
        box._grants.forEach((g) => { g.can_download = value; });
        renderChips(box);
      };
      box.querySelector("[data-all-dl]").addEventListener("click", () => setAll(true));
      box.querySelector("[data-no-dl]").addEventListener("click", () => setAll(false));
      box.querySelector("[data-clear-all]").addEventListener("click", () => {
        // Confirmed: this throws away a list that may have taken a paste and a
        // round of ticking to build, and there is no undo before the next Save.
        if (!confirm(`Stop sharing all ${box._grants.length} meetings with this user?`)) return;
        box._grants = [];
        renderChips(box);
      });
    }
  }

  // Chips are only in the DOM until the row is saved, which is exactly the
  // misunderstanding that loses a grant — so every add says which button applies it.
  function applyHintFor(root) {
    return root.id === "new-meetings" ? "Create user to apply" : "Save to apply";
  }

  function renderMeetingResults(panel, box, meetings, ready, applyHint) {
    if (!meetings.length) {
      panel.innerHTML = ready
        ? '<div class="mg-empty">No meetings match that. To share several at once, paste their meeting IDs separated by commas, spaces or new lines.</div>'
        : '<div class="mg-empty">The bucket index is still building — try again shortly.</div>';
      panel.hidden = false;
      return;
    }
    panel.innerHTML = meetings.map((m, i) => `
      <button type="button" class="mg-result" data-i="${i}">
        <span class="mg-result-id">${esc(m.meeting_id)}</span>
        <span class="mg-result-meta">${esc(meetingLabel(m, m.meeting_id))}</span>
      </button>`).join("");
    panel.hidden = false;
    panel.querySelectorAll(".mg-result").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = meetings[Number(btn.dataset.i)];
        const counts = addGrants(box, [{ meeting_id: m.meeting_id, detail: m }]);
        showNotice(counts.added
          ? `Sharing meeting ${m.meeting_id}. ${applyHint}.`
          : `Meeting ${m.meeting_id} is already shared with this user.`,
          counts.added ? "ok" : "error");
        panel.hidden = true;
      });
    });
  }

  async function lookupMeetingIds(text) {
    // POST, not GET: a few hundred pasted ids overflow the 4094-byte request line
    // gunicorn accepts by default, and the browser would surface that as a bare
    // 414 with nothing in it. /api/download/bulk posts its key list for the same
    // reason. The raw text goes up as-is so the server splits it with the same
    // rules, rather than two parsers drifting apart.
    try {
      const resp = await fetch("/api/admin/meetings/lookup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: text }),
      });
      if (resp.status === 401) { location.href = "/login"; return null; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        showNotice(data.error || "Could not look up those meeting IDs.", "error");
        return null;
      }
      return data;
    } catch (e) {
      showNotice("Network error looking up those meeting IDs.", "error");
      return null;
    }
  }

  // Every pasted id is added, including ones the index does not know yet — the
  // server accepts those on purpose (a recording uploaded minutes ago has not been
  // indexed), and the chip labels itself "not in the current index" so the state is
  // visible rather than assumed. Only tokens that could never BE an id are skipped,
  // and those are named in the notice instead of vanishing.
  function applyBulk(box, input, panel, data, applyHint) {
    const found = data.meetings || [];
    const missing = data.missing || [];
    const entries = found.map((m) => ({ meeting_id: m.meeting_id, detail: m }))
      .concat(missing.map((mid) => ({ meeting_id: mid, detail: null })));
    const counts = addGrants(box, entries);

    const bits = [`Added ${counts.added} meeting${counts.added === 1 ? "" : "s"}`];
    if (missing.length) {
      bits.push(`${missing.length} not in the index yet (shared anyway)`);
    }
    if (counts.dupes) bits.push(`${counts.dupes} already shared`);
    if ((data.invalid || []).length) {
      bits.push(`skipped ${data.invalid.length} that ${data.invalid.length === 1 ? "is" : "are"} not a meeting ID: ${data.invalid.join(", ")}`);
    }
    if (data.truncated) bits.push(`only the first ${data.limit} were taken`);
    bits.push(applyHint);
    showNotice(bits.join(" · ") + ".", counts.added ? "ok" : "error");

    input.value = "";
    panel.hidden = true;
  }

  function renderBulkResults(panel, box, input, data, applyHint) {
    const found = data.meetings || [];
    const missing = data.missing || [];
    const invalid = data.invalid || [];
    const total = found.length + missing.length;
    if (!total && !invalid.length) { panel.hidden = true; return; }

    const notes = [];
    if (missing.length) {
      notes.push(`${missing.length} of these are not in the current index. Sharing them still works — their files appear once the index catches up.`);
    }
    if (invalid.length) {
      notes.push(`Not a meeting ID, will be skipped: ${esc(invalid.join(", "))}`);
    }
    if (data.truncated) {
      notes.push(`Only the first ${data.limit} IDs are shown — that is the most one account can hold.`);
    }
    if (data.ready === false) {
      notes.push("The bucket index is still building, so some of these may resolve later.");
    }

    const rows = found.map((m, i) => `
      <button type="button" class="mg-result" data-found="${i}">
        <span class="mg-result-id">${esc(m.meeting_id)}</span>
        <span class="mg-result-meta">${esc(meetingLabel(m, m.meeting_id))}</span>
      </button>`).join("")
      + missing.map((mid, i) => `
      <button type="button" class="mg-result mg-result-missing" data-missing="${i}">
        <span class="mg-result-id">${esc(mid)}</span>
        <span class="mg-result-meta">Not in the current index — it will still be shared</span>
      </button>`).join("");

    panel.innerHTML = `
      <div class="mg-bulk-head">
        <span class="mg-bulk-count"><strong>${total}</strong> meeting ID${total === 1 ? "" : "s"} pasted${found.length ? ` · ${found.length} found` : ""}${missing.length ? ` · ${missing.length} not indexed` : ""}</span>
        ${total ? `<button type="button" class="btn btn-primary btn-sm" data-add-all>Add all ${total}</button>` : ""}
      </div>
      ${notes.length ? `<div class="mg-bulk-note">${notes.join("<br>")}</div>` : ""}
      <div class="mg-bulk-list">${rows}</div>`;
    panel.hidden = false;

    const addAll = panel.querySelector("[data-add-all]");
    if (addAll) {
      addAll.addEventListener("click", () => applyBulk(box, input, panel, data, applyHint));
    }
    // Each row still adds just itself, for the case where the paste was right but
    // one or two of them should not be shared after all.
    panel.querySelectorAll("[data-found]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = found[Number(btn.dataset.found)];
        const counts = addGrants(box, [{ meeting_id: m.meeting_id, detail: m }]);
        showNotice(counts.added
          ? `Sharing meeting ${m.meeting_id}. ${applyHint}.`
          : `Meeting ${m.meeting_id} is already shared with this user.`,
          counts.added ? "ok" : "error");
      });
    });
    panel.querySelectorAll("[data-missing]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mid = missing[Number(btn.dataset.missing)];
        const counts = addGrants(box, [{ meeting_id: mid, detail: null }]);
        showNotice(counts.added
          ? `Sharing meeting ${mid} — not in the index yet. ${applyHint}.`
          : `Meeting ${mid} is already shared with this user.`,
          counts.added ? "ok" : "error");
      });
    });
  }

  function wireMeetingPicker(root, grants) {
    const input = root.querySelector(".mg-q");
    const panel = root.querySelector(".mg-results");
    const box = root.querySelector(".mg-chips");
    const applyHint = applyHintFor(root);
    box._grants = (grants || []).map((g) => ({
      meeting_id: g.meeting_id, can_download: !!g.can_download, detail: g.detail,
    }));
    renderChips(box);

    let timer = 0;
    let seq = 0;

    async function refresh(text) {
      const mine = ++seq;
      if (looksLikeList(text)) {
        const data = await lookupMeetingIds(text);
        if (mine !== seq || !data) return;    // a newer keystroke won
        renderBulkResults(panel, box, input, data, applyHint);
        return;
      }
      try {
        const resp = await fetch("/api/admin/meetings?q=" + encodeURIComponent(text));
        if (mine !== seq) return;
        const data = await resp.json();
        if (mine !== seq) return;
        if (!resp.ok) {
          // A 502 from an expired S3 token must not be shown as "no meetings
          // match that" — the admin would go on retyping a query that was never
          // the problem. Say what the server actually said.
          showNotice(data.error || "Could not search meetings.", "error");
          panel.hidden = true;
          return;
        }
        renderMeetingResults(panel, box, data.meetings || [], data.ready !== false, applyHint);
      } catch (e) { panel.hidden = true; }
    }

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { panel.hidden = true; return; }
      // Debounced: each lookup walks the whole index server-side, so a keystroke
      // per request would put real load on the box for no benefit. A paste fires
      // `input` too, so it lands here like anything else.
      timer = setTimeout(() => refresh(q), 300);
    });

    // Enter shares the whole paste without a trip to the dropdown, and MUST
    // preventDefault: inside the create <form> it would otherwise submit the form
    // and create the user before a single meeting had been added — the exact
    // silent loss this widget is being fixed for.
    input.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      const parsed = parseMeetingIds(text);
      if (!parsed.ids.length) {
        showNotice(parsed.invalid.length
          ? `Not a meeting ID: ${parsed.invalid.join(", ")}. Meeting IDs are digits only — pick a result from the list instead.`
          : "Type or paste at least one meeting ID.", "error");
        return;
      }
      clearTimeout(timer);
      seq++;                                  // cancel any dropdown still in flight
      const data = await lookupMeetingIds(text);
      if (!data) return;
      applyBulk(box, input, panel, data, applyHint);
    });
  }

  function readMeetingGrants(root) {
    const box = root.querySelector(".mg-chips");
    return (box._grants || []).map((g) => ({
      meeting_id: g.meeting_id, can_download: !!g.can_download,
    }));
  }

  // The results panel is anchored to the INPUT, not to the whole widget, or it
  // would drop below the chip list instead of under what was typed.
  const MEETING_PICKER_HTML = `
    <div class="meeting-grant">
      <div class="mg-search">
        <input class="input mg-q" type="search" autocomplete="off"
               placeholder="Paste one or many meeting IDs — or search by candidate, company or date">
        <div class="mg-results" hidden></div>
      </div>
      <div class="mg-chips"></div>
    </div>`;

  // ONE document-level listener for every picker, registered once. Wiring this
  // inside wireMeetingPicker instead added a fresh listener per user row on every
  // load() — and load() runs after each save — so the handlers grew without bound
  // for as long as the admin page stayed open.
  // Capture phase for the same reason datepicker.js uses it: removing a chip
  // re-renders .mg-chips, which detaches the ✕ that was clicked, and a
  // bubble-phase listener would then read that inside click as an outside one.
  document.addEventListener("click", (e) => {
    document.querySelectorAll(".meeting-grant").forEach((root) => {
      const panel = root.querySelector(".mg-results");
      if (panel && !panel.hidden && !root.contains(e.target)) panel.hidden = true;
    });
  }, true);

  // Keep a host container in sync with its department checkboxes (preserving
  // any host ticks already made for departments that stay selected).
  function wireHostSync(deptBox, hostBox) {
    deptBox.addEventListener("change", () => {
      renderHostBoxes(hostBox, readDeptChecks(deptBox), readHostChecks(hostBox));
    });
  }

  async function load() {
    try {
      const resp = await fetch("/api/admin/users");
      if (resp.status === 401) { location.href = "/login"; return; }
      if (resp.status === 403) { location.href = "/search"; return; }
      const data = await resp.json();
      if (!resp.ok) { showNotice(data.error || "Could not load users.", "error"); return; }
      allDepartments = data.departments || [];
      hostsByDept = data.hosts_by_department || {};
      renderDeptChecks($("new-depts"), []);     // create-form checkboxes
      renderHostBoxes($("new-hosts"), [], {});
      renderUsers(data.users || []);
      renderAdmins(data.admins || []);
    } catch (e) {
      showNotice("Network error loading users.", "error");
    }
  }

  function renderAdmins(admins) {
    $("admins-list").innerHTML = admins.length
      ? admins.map((a) => `<span class="badge badge-admin" style="margin:0 6px 6px 0;">${esc(a)}</span>`).join("")
      : '<p class="user-meta">No admins found in .env.</p>';
  }

  function renderUsers(users) {
    const box = $("users-table");
    if (users.length === 0) {
      box.innerHTML = '<div class="empty"><div class="big">👤</div>No users yet — add one on the right.</div>';
      return;
    }
    box.innerHTML = users.map((u) => {
      const depts = u.departments || [];
      const access = u.can_download ? "Can download" : "View only";
      const accessClass = u.can_download ? "badge-admin" : "badge-muted";
      return `
        <div class="user-row" data-user="${esc(u.username)}">
          <div class="user-row-head">
            <strong>${esc(u.username)}</strong>
            <span class="badge ${accessClass}">${access}</span>
            <span class="spacer"></span>
            <span class="user-meta">${esc(u.created_at || "—")}${u.created_by ? " · by " + esc(u.created_by) : ""}</span>
          </div>
          <div class="user-access">
            <div class="dept-checks user-depts"></div>
            <div class="host-restrict user-hosts"></div>
            <!-- Opened only for a short list: a bulk-granted user with 40 meetings
                 would otherwise expand ~1,800px inside the Users card and push
                 every other user off the screen. -->
            <details class="mg-box user-meetings"${(u.meetings || []).length && (u.meetings || []).length <= 5 ? " open" : ""}>
              <summary>Shared meetings — <span class="host-sum">${
                (u.meetings || []).length
                  ? `${u.meetings.length} meeting${u.meetings.length === 1 ? "" : "s"}`
                  : "none"}</span></summary>
              <p class="user-meta host-hint">Give access to individual meetings from any department — even ones this user cannot otherwise browse. Paste several meeting IDs at once, separated by commas, spaces or new lines.</p>
              ${MEETING_PICKER_HTML}
            </details>
            <div class="user-access-actions">
              <select class="input perm-select">
                <option value="view"${u.can_download ? "" : " selected"}>View only</option>
                <option value="download"${u.can_download ? " selected" : ""}>Can download</option>
              </select>
              <button class="btn btn-ghost btn-sm" data-save>Save</button>
              <button class="btn btn-danger btn-sm" data-del>Delete</button>
            </div>
          </div>
        </div>`;
    }).join("");

    // Fill each row's checkbox grid with that user's current departments + host
    // restriction, then wire buttons.
    box.querySelectorAll(".user-row").forEach((row, i) => {
      const deptBox = row.querySelector(".user-depts");
      const hostBox = row.querySelector(".user-hosts");
      renderDeptChecks(deptBox, users[i].departments || []);
      renderHostBoxes(hostBox, users[i].departments || [], users[i].hosts || {});
      wireHostSync(deptBox, hostBox);
      wireMeetingPicker(row.querySelector(".user-meetings"), users[i].meetings || []);
      row.querySelector("[data-save]").addEventListener("click", (e) => saveAccess(row, e.currentTarget));
      row.querySelector("[data-del]").addEventListener("click", (e) =>
        deleteUser(row.dataset.user, e.currentTarget));
    });
  }

  async function saveAccess(row, btn) {
    const username = row.dataset.user;
    const departments = readDeptChecks(row.querySelector(".user-depts"));
    const hosts = readHostChecks(row.querySelector(".user-hosts"));
    const meetings = readMeetingGrants(row.querySelector(".user-meetings"));
    const can_download = row.querySelector(".perm-select").value === "download";
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    try {
      const resp = await fetch("/api/admin/users/" + encodeURIComponent(username), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ departments, hosts, meetings, can_download }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Could not save access.", "error"); return; }
      showNotice(`Saved access for "${username}".`, "ok");
      load();
    } catch (e) {
      showNotice("Network error saving access.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Save";
    }
  }

  async function deleteUser(username, btn) {
    if (!confirm(`Delete user "${username}"? They will no longer be able to sign in.`)) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    try {
      const resp = await fetch("/api/admin/users/" + encodeURIComponent(username), { method: "DELETE" });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Delete failed.", "error"); btn.disabled = false; btn.textContent = "Delete"; return; }
      showNotice(`Deleted user "${username}".`, "ok");
      load();
    } catch (e) {
      showNotice("Network error during delete.", "error");
      btn.disabled = false; btn.textContent = "Delete";
    }
  }

  async function createUser(e) {
    e.preventDefault();
    const btn = $("btn-create");
    const username = $("new-username").value;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Creating…';
    try {
      const resp = await fetch("/api/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          password: $("new-password").value,
          departments: readDeptChecks($("new-depts")),
          hosts: readHostChecks($("new-hosts")),
          meetings: readMeetingGrants($("new-meetings")),
          can_download: $("new-perm").value === "download",
        }),
      });
      if (resp.status === 401) { location.href = "/login"; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { showNotice(data.error || "Could not create user.", "error"); return; }
      showNotice(`Created user "${username}".`, "ok");
      $("create-form").reset();
      renderDeptChecks($("new-depts"), []);
      renderHostBoxes($("new-hosts"), [], {});
      resetMeetingPicker();      // form.reset() cannot clear chips it never owned
      load();
    } catch (e2) {
      showNotice("Network error creating user.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Create user";
    }
  }

  // The create form's picker is built from the same markup as the per-user ones,
  // so the two can never drift apart.
  function resetMeetingPicker() {
    $("new-meetings").innerHTML = MEETING_PICKER_HTML;
    wireMeetingPicker($("new-meetings"), []);
  }

  $("create-form").addEventListener("submit", createUser);
  wireHostSync($("new-depts"), $("new-hosts"));
  resetMeetingPicker();
  load();
})();
