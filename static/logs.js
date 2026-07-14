/* logs.js — admin-only audit activity browser. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const notice = $("notice");
  const logsArea = $("logs-area");
  const summary = $("logs-summary");
  const pagination = $("logs-pagination");
  let page = 1;
  let requestSequence = 0;

  const ACTION_LABELS = {
    login: "Login",
    logout: "Logout",
    search: "Search",
    view: "Opened preview",
    preview: "Opened preview",
    recording_view: "Opened preview",
    download: "Started download",
    bulk_download: "Prepared ZIP",
    refresh: "Refreshed index",
    user_create: "Created user",
    user_update: "Updated user",
    user_delete: "Deleted user",
    logs_cleared: "Cleared all logs",
    screenshot: "Screenshot",
    screen_capture_suspected: "Possible screen capture",
    camera_enrolled: "Camera enrolled",
    camera_unavailable: "Camera denied / unavailable",
  };

  const DETAIL_LABELS = {
    attempted_username: "Attempted username",
    can_download: "Download access",
    company: "Company",
    deleted_events: "Deleted entries",
    date: "Search date",
    error: "Error",
    failure_reason: "Failure reason",
    file_count: "Files",
    filename: "File",
    ip: "IP address",
    ip_address: "IP address",
    q: "Query",
    query: "Query",
    reason: "Reason",
    result_count: "Results",
    results_count: "Results",
    round: "Round",
    sort: "Sort",
    total_size: "Total size",
    user_agent: "Browser / device",
  };

  const REPEATED_DETAIL_KEYS = new Set([
    "action", "candidate", "candidate_name", "category", "department", "event",
    "file_type", "host", "meeting", "meeting_id", "occurred_at", "recording_date",
    "role", "status", "success", "username",
  ]);

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
    ));
  }

  function isSensitiveKey(key) {
    const normalized = String(key || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    return [
      "password", "passwd", "passphrase", "passcode", "pwd", "secret", "token",
      "authorization", "cookie", "credential", "session", "apikey", "accesskey",
    ].some((part) => normalized.includes(part));
  }

  // Details are untrusted and may come from older log formats. Do not let a
  // password-like key/value pair leak even when details arrived as plain text.
  function redactDetailText(value) {
    return String(value == null ? "" : value)
      .replace(/((?:password|passwd|passphrase|passcode|pwd|secret|token|authorization|cookie|credential|api[_ -]?key|access[_ -]?key)\s*[=:]\s*)[^,;\n&]+/gi, "$1[redacted]")
      .replace(/((?:password|passwd|passphrase|passcode|pwd|secret|token|authorization|cookie|credential|api[_ -]?key|access[_ -]?key)=)[^&\s]+/gi, "$1[redacted]");
  }

  function showNotice(message) {
    notice.textContent = message;
    notice.className = "notice show notice-error";
  }

  function showOk(message) {
    notice.textContent = message;
    notice.className = "notice show notice-ok";
  }

  function clearNotice() {
    notice.textContent = "";
    notice.className = "notice";
  }

  function humanize(value) {
    return String(value == null ? "" : value)
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }

  function actionLabel(action) {
    const key = String(action || "").toLowerCase();
    return ACTION_LABELS[key] || humanize(key) || "Activity";
  }

  function actionClass(action) {
    const key = String(action || "").toLowerCase();
    if (key === "login" || key === "logout") return "auth";
    if (key === "search") return "search";
    if (key === "view" || key.includes("preview") || key.includes("view")) return "view";
    if (key.includes("download")) return "download";
    if (key === "screenshot" || key === "screen_capture_suspected" ||
        key === "camera_enrolled" || key === "camera_unavailable") return "capture";
    if (key.startsWith("user_") || key === "logs_cleared") return "admin";
    if (key === "refresh") return "refresh";
    return "other";
  }

  function parseDetails(value) {
    if (value && typeof value === "object") return value;
    if (typeof value !== "string") return value;
    const trimmed = value.trim();
    if (!trimmed || !/^[{[]/.test(trimmed)) return value;
    try {
      return JSON.parse(trimmed);
    } catch (error) {
      return value;
    }
  }

  function eventSources(event) {
    const details = parseDetails(event.details);
    const sources = [event, event.recording, details];
    if (details && typeof details === "object" && !Array.isArray(details)) {
      sources.push(details.recording, details.file, details.filters, details.search);
    }
    return sources.filter((source) => source && typeof source === "object" && !Array.isArray(source));
  }

  function eventValue(event, keys) {
    const sources = eventSources(event);
    for (const source of sources) {
      for (const key of keys) {
        if (Object.prototype.hasOwnProperty.call(source, key) && source[key] != null && source[key] !== "") {
          return source[key];
        }
      }
    }
    return "";
  }

  function normalizedTimestamp(value) {
    const raw = String(value || "");
    // Audit timestamps should include an offset. Interpret legacy offset-less
    // ISO timestamps as UTC rather than silently treating them as browser time.
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(raw)) return raw + "Z";
    return raw;
  }

  function formatActivityTime(value) {
    const date = new Date(normalizedTimestamp(value));
    if (Number.isNaN(date.getTime())) {
      return { date: value ? String(value) : "Unknown time", time: "", iso: String(value || "") };
    }
    return {
      date: new Intl.DateTimeFormat(undefined, {
        year: "numeric", month: "short", day: "2-digit",
      }).format(date),
      time: new Intl.DateTimeFormat(undefined, {
        hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short",
      }).format(date),
      iso: date.toISOString(),
    };
  }

  function statusInfo(event) {
    const success = event.success;
    if (success === false || success === 0 || String(success).toLowerCase() === "false") {
      return { label: "Failed", className: "failed" };
    }
    if (success === true || success === 1 || String(success).toLowerCase() === "true") {
      return { label: "Success", className: "success" };
    }
    const raw = String(event.status || "").toLowerCase();
    if (["failed", "failure", "error", "denied"].includes(raw)) {
      return { label: humanize(raw), className: "failed" };
    }
    if (["ok", "success", "successful", "complete", "completed"].includes(raw)) {
      return { label: humanize(raw), className: "success" };
    }
    return { label: raw ? humanize(raw) : "Recorded", className: "neutral" };
  }

  function displayScalar(value) {
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString() : String(value);
    return redactDetailText(value);
  }

  function detailLabel(path) {
    const segments = String(path).split(".").filter(Boolean);
    const key = segments.pop() || "details";
    const base = DETAIL_LABELS[key] || humanize(key.replace(/\[\d+\]/g, ""));
    const usefulParents = segments.filter((part) => !["details", "filters", "search"].includes(part));
    return usefulParents.length ? usefulParents.map(humanize).join(" · ") + " · " + base : base;
  }

  function flattenDetails(value, path, entries, depth) {
    if (entries.length >= 40 || depth > 4 || value == null || value === "") return;
    if (Array.isArray(value)) {
      if (!value.length) return;
      if (value.every((item) => item == null || typeof item !== "object")) {
        entries.push({ label: detailLabel(path), value: value.map(displayScalar).join(", ") });
        return;
      }
      value.forEach((item, index) => flattenDetails(item, `${path}[${index + 1}]`, entries, depth + 1));
      return;
    }
    if (typeof value !== "object") {
      entries.push({ label: detailLabel(path), value: displayScalar(value) });
      return;
    }
    Object.keys(value).forEach((key) => {
      if (entries.length >= 40 || isSensitiveKey(key) || key === "items" || key === "recordings") return;
      if (key === "capture_photo") return;   // rendered as a thumbnail, not text
      if (REPEATED_DETAIL_KEYS.has(String(key).toLowerCase())) return;
      const nextPath = path ? `${path}.${key}` : key;
      flattenDetails(value[key], nextPath, entries, depth + 1);
    });
  }

  function bulkItems(details) {
    if (!details || typeof details !== "object" || Array.isArray(details)) return [];
    if (Array.isArray(details.items)) return details.items;
    if (Array.isArray(details.recordings)) return details.recordings;
    return [];
  }

  function renderBulkItems(details) {
    const items = bulkItems(details);
    if (!items.length) return "";
    const rows = items.map((item, index) => {
      if (!item || typeof item !== "object") {
        return `<li>${esc(redactDetailText(item) || `Recording ${index + 1}`)}</li>`;
      }
      const safeParts = [
        eventValue(item, ["candidate", "candidate_name"]),
        eventValue(item, ["host"]),
        eventValue(item, ["meeting_id", "meeting"]),
        eventValue(item, ["recording_date", "meeting_date"]),
        eventValue(item, ["department", "dept"]),
        eventValue(item, ["file_type", "category", "file_category"]),
        eventValue(item, ["filename", "file_name"]),
      ].filter((part) => part != null && part !== "").map((part) => redactDetailText(part));
      return `<li>${esc(safeParts.join(" · ") || `Recording ${index + 1}`)}</li>`;
    }).join("");
    return `<details class="log-bulk-items"><summary>${items.length.toLocaleString()} recording${items.length === 1 ? "" : "s"}</summary><ol>${rows}</ol></details>`;
  }

  function capturePhotoHtml(details) {
    if (!details || typeof details !== "object" || Array.isArray(details)) return "";
    const name = details.capture_photo;
    if (!name || typeof name !== "string") return "";
    const src = "/api/admin/capture/" + encodeURIComponent(name);
    return `<a class="capture-photo-link" href="${esc(src)}" target="_blank" rel="noopener" ` +
      `title="Open full-size capture photo"><img class="capture-thumb" src="${esc(src)}" ` +
      `alt="Webcam capture" loading="lazy"></a>`;
  }

  function renderDetails(event) {
    const details = parseDetails(event.details);
    const entries = [];
    if (typeof details === "string") {
      if (details.trim()) entries.push({ label: "Details", value: redactDetailText(details) });
    } else {
      flattenDetails(details, "", entries, 0);
    }

    // A few legacy formats stored useful audit context at the top level.
    [
      "attempted_username", "ip_address", "ip", "user_agent", "q", "query",
      "result_count", "results_count", "reason", "failure_reason", "error",
    ].forEach((key) => {
      if (!Object.prototype.hasOwnProperty.call(event, key) || event[key] == null || event[key] === "") return;
      if (isSensitiveKey(key) || entries.some((entry) => entry.label === detailLabel(key))) return;
      entries.push({ label: detailLabel(key), value: displayScalar(event[key]) });
    });

    const renderedEntries = entries.map((entry) =>
      `<div class="log-detail"><span>${esc(entry.label)}</span>${esc(entry.value)}</div>`
    );
    let html = capturePhotoHtml(details);
    html += renderedEntries.slice(0, 3).join("");
    if (renderedEntries.length > 3) {
      html += `<details class="log-more-details"><summary>+${(renderedEntries.length - 3).toLocaleString()} more</summary>${renderedEntries.slice(3).join("")}</details>`;
    }
    html += renderBulkItems(details);
    return html || '<span class="log-none">—</span>';
  }

  function renderEvent(event) {
    event = event && typeof event === "object" ? event : {};
    const action = String(event.action || event.event || "activity");
    const status = statusInfo(event);
    const activity = formatActivityTime(event.occurred_at || event.timestamp || event.created_at);
    const username = event.username || event.actor || "";
    const failedLogin = action.toLowerCase() === "login" && status.className === "failed" && !username;
    const role = event.role || event.actor_role || "";
    const candidate = eventValue(event, ["candidate", "candidate_name"]);
    const host = eventValue(event, ["host", "host_name"]);
    const meetingId = eventValue(event, ["meeting_id", "meeting"]);
    const recordingDate = eventValue(event, ["recording_date", "meeting_date"]);
    const department = eventValue(event, ["department", "dept"]);
    const fileType = eventValue(event, ["file_type", "category", "file_category"]);

    return `<tr>
      <td data-label="Activity">
        <time class="log-time" datetime="${esc(activity.iso)}" title="${esc(activity.iso)}">
          <strong>${esc(activity.date)}</strong><span>${esc(activity.time)}</span>
        </time>
      </td>
      <td data-label="User">
        <div class="log-person"><strong>${esc(username || (failedLogin ? "Unverified login" : "Unknown user"))}</strong>${role ? `<span>${esc(humanize(role))}</span>` : ""}</div>
      </td>
      <td data-label="Action / status">
        <div class="log-action"><span class="log-action-badge log-action-${actionClass(action)}">${esc(actionLabel(action))}</span><span class="log-status log-status-${status.className}">${esc(status.label)}</span></div>
      </td>
      <td data-label="Candidate" class="candidate">${candidate ? esc(candidate) : '<span class="log-none">—</span>'}</td>
      <td data-label="Host">${host ? esc(host) : '<span class="log-none">—</span>'}</td>
      <td data-label="Meeting ID" class="log-mono">${meetingId ? esc(meetingId) : '<span class="log-none">—</span>'}</td>
      <td data-label="Recording / search date">${recordingDate ? esc(recordingDate) : '<span class="log-none">—</span>'}</td>
      <td data-label="Department / type">
        <div class="log-recording-meta">${department ? `<strong>${esc(department)}</strong>` : '<span class="log-none">—</span>'}${fileType ? `<span>${esc(humanize(fileType))}</span>` : ""}</div>
      </td>
      <td data-label="Details" class="log-details-cell">${renderDetails(event)}</td>
      <td data-label="" class="col-actions">${event.id != null
        ? `<button type="button" class="btn btn-danger btn-sm log-del-btn" data-id="${esc(event.id)}" title="Delete this log entry">Delete</button>`
        : ""}</td>
    </tr>`;
  }

  function normalizedOption(item, kind) {
    if (item == null) return null;
    if (typeof item !== "object") {
      const value = String(item);
      return value ? { value, label: kind === "action" ? actionLabel(value) : value } : null;
    }
    const value = String(item.value || item[kind] || (kind === "user" ? item.username : "") || "");
    if (!value) return null;
    return { value, label: String(item.label || (kind === "action" ? actionLabel(value) : value)) };
  }

  function populateSelect(select, values, kind, emptyLabel) {
    const current = select.value;
    const unique = new Map();
    (values || []).forEach((item) => {
      const normalized = normalizedOption(item, kind);
      if (normalized && !unique.has(normalized.value)) unique.set(normalized.value, normalized);
    });
    if (current && !unique.has(current)) {
      unique.set(current, { value: current, label: kind === "action" ? actionLabel(current) : current });
    }
    select.innerHTML = `<option value="">${esc(emptyLabel)}</option>` + Array.from(unique.values()).map((option) =>
      `<option value="${esc(option.value)}">${esc(option.label)}</option>`
    ).join("");
    select.value = current;
  }

  function positiveInteger(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? Math.floor(number) : fallback;
  }

  function renderLogs(data) {
    const events = Array.isArray(data.events) ? data.events : [];
    const total = Math.max(0, Number(data.total) || 0);
    const currentPage = positiveInteger(data.page, page);
    const perPage = positiveInteger(data.per_page, positiveInteger($("logs-per-page").value, 50));
    const start = total ? (currentPage - 1) * perPage + 1 : 0;
    const end = total ? Math.min(total, start + events.length - 1) : 0;
    summary.innerHTML = total
      ? `<strong>${total.toLocaleString()}</strong> event${total === 1 ? "" : "s"} · showing ${start.toLocaleString()}–${end.toLocaleString()}`
      : "0 events";

    if (!events.length) {
      logsArea.innerHTML = '<div class="empty"><div class="big">🧾</div>No activity matches these filters.</div>';
      return;
    }

    logsArea.innerHTML = `<div class="table-wrap log-table-wrap">
      <table class="results log-table">
        <thead><tr>
          <th>Activity</th><th>User</th><th>Action / status</th><th>Candidate</th><th>Host</th>
          <th>Meeting ID</th><th>Recording / search date</th><th>Department / type</th><th>Details</th><th></th>
        </tr></thead>
        <tbody>${events.map(renderEvent).join("")}</tbody>
      </table>
    </div>`;

    logsArea.querySelectorAll(".log-del-btn").forEach((button) => {
      button.addEventListener("click", () => deleteEntry(button.dataset.id, button));
    });
  }

  function hidePagination() {
    pagination.innerHTML = "";
    pagination.style.display = "none";
  }

  function renderPagination(data) {
    const total = Math.max(0, Number(data.total) || 0);
    const perPage = positiveInteger(data.per_page, positiveInteger($("logs-per-page").value, 50));
    const pages = positiveInteger(data.pages, Math.max(1, Math.ceil(total / perPage)));
    const current = Math.min(pages, positiveInteger(data.page, page));
    if (!total || pages <= 1) {
      hidePagination();
      return;
    }

    const button = (target, label, disabled, active) =>
      `<button type="button" class="btn btn-ghost btn-sm page-btn${active ? " active" : ""}" data-page="${target}"${disabled ? " disabled" : ""}>${label}</button>`;
    const windowPages = [];
    const add = (value) => {
      if (value >= 1 && value <= pages && !windowPages.includes(value)) windowPages.push(value);
    };
    add(1); add(2);
    for (let value = current - 2; value <= current + 2; value += 1) add(value);
    add(pages - 1); add(pages);
    windowPages.sort((a, b) => a - b);

    const parts = [button(current - 1, "‹ Prev", current <= 1, false)];
    let previous = 0;
    windowPages.forEach((value) => {
      if (value - previous > 1) parts.push('<span class="page-ellipsis">…</span>');
      parts.push(button(value, String(value), false, value === current));
      previous = value;
    });
    parts.push(button(current + 1, "Next ›", current >= pages, false));
    parts.push(`<span class="page-info">Page ${current.toLocaleString()} of ${pages.toLocaleString()}</span>`);

    pagination.innerHTML = parts.join("");
    pagination.style.display = "flex";
    pagination.querySelectorAll(".page-btn").forEach((item) => {
      item.addEventListener("click", () => {
        const target = positiveInteger(item.dataset.page, page);
        if (target === page || target < 1 || target > pages) return;
        page = target;
        loadLogs(true);
      });
    });
  }

  function localDateBoundary(value, endOfDay) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return "";
    const year = Number(match[1]);
    const month = Number(match[2]) - 1;
    const day = Number(match[3]);
    const date = endOfDay
      ? new Date(year, month, day, 23, 59, 59, 999)
      : new Date(year, month, day, 0, 0, 0, 0);
    if (Number.isNaN(date.getTime())) return "";
    return date.toISOString();
  }

  function requestParams() {
    const params = new URLSearchParams({
      page: String(page),
      per_page: $("logs-per-page").value,
    });
    const action = $("f-action").value.trim();
    const username = $("f-username").value.trim();
    const query = $("f-query").value.trim();
    const dateFrom = localDateBoundary($("f-date-from").value, false);
    const dateTo = localDateBoundary($("f-date-to").value, true);
    if (action) params.set("action", action);
    if (username) params.set("username", username);
    if (query) params.set("q", query);
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    return params;
  }

  async function loadLogs(scrollToResults) {
    clearNotice();
    const rawFrom = $("f-date-from").value;
    const rawTo = $("f-date-to").value;
    if (rawFrom && rawTo && rawFrom > rawTo) {
      showNotice("The activity start date must be on or before the end date.");
      return;
    }

    const sequence = ++requestSequence;
    const button = $("btn-apply");
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Loading…';
    logsArea.innerHTML = '<div class="empty"><div class="spinner spinner-lg"></div>Loading activity…</div>';
    summary.textContent = "";
    hidePagination();

    try {
      const response = await fetch("/api/admin/logs?" + requestParams().toString(), {
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });
      if (sequence !== requestSequence) return;
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      if (response.status === 403) {
        location.href = "/search";
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not load activity logs.");
      if (Array.isArray(data.actions)) populateSelect($("f-action"), data.actions, "action", "All actions");
      if (Array.isArray(data.users)) populateSelect($("f-username"), data.users, "user", "All users");

      const events = Array.isArray(data.events) ? data.events : [];
      const pages = positiveInteger(data.pages, 1);
      if (!events.length && Number(data.total) > 0 && page > pages) {
        page = pages;
        await loadLogs(scrollToResults);
        return;
      }
      page = positiveInteger(data.page, page);
      renderLogs(data);
      renderPagination(data);
      if (scrollToResults) logsArea.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      if (sequence !== requestSequence) return;
      showNotice(error && error.message ? error.message : "Network error loading activity logs.");
      logsArea.innerHTML = '<div class="empty"><div class="big">⚠️</div>Could not load activity logs.</div>';
    } finally {
      if (sequence === requestSequence) {
        button.disabled = false;
        button.textContent = "Apply filters";
      }
    }
  }

  function clearFilters() {
    $("logs-form").reset();
    page = 1;
    loadLogs();
  }

  async function deleteEntry(id, button) {
    if (id == null || id === "") return;
    if (!window.confirm("Delete this single log entry?\n\nThis permanently removes just this one event and cannot be undone.")) return;

    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span>';
    clearNotice();
    try {
      const response = await fetch("/api/admin/logs/" + encodeURIComponent(id), {
        method: "DELETE",
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });
      if (response.status === 401) { location.href = "/login"; return; }
      if (response.status === 403) { location.href = "/search"; return; }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not delete the log entry.");
      // Reload the current page so totals + pagination stay correct.
      await loadLogs();
      showOk("Log entry deleted.");
    } catch (error) {
      showNotice(error && error.message ? error.message : "Network error deleting the log entry.");
      button.disabled = false;
      button.textContent = "Delete";
    }
  }

  async function clearAllLogs() {
    const button = $("btn-clear-all-logs");
    if (!window.confirm(
      "Delete ALL activity logs permanently?\n\n" +
      "This removes every recorded event for every user and cannot be undone. " +
      "A single entry noting that you cleared the logs will be recorded."
    )) return;

    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Clearing…';
    clearNotice();
    try {
      const response = await fetch("/api/admin/logs", {
        method: "DELETE",
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });
      if (response.status === 401) { location.href = "/login"; return; }
      if (response.status === 403) { location.href = "/search"; return; }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not clear activity logs.");
      const deleted = Number(data.deleted || 0);
      // Reset filters + page so the single "cleared" entry is visible, then reload.
      $("logs-form").reset();
      page = 1;
      await loadLogs(true);
      showOk(`Cleared ${deleted.toLocaleString()} log entr${deleted === 1 ? "y" : "ies"}.`);
    } catch (error) {
      showNotice(error && error.message ? error.message : "Network error clearing activity logs.");
    } finally {
      button.disabled = false;
      button.innerHTML = "🗑 Clear all logs";
    }
  }

  $("logs-form").addEventListener("submit", (event) => {
    event.preventDefault();
    page = 1;
    loadLogs();
  });
  $("btn-clear-logs").addEventListener("click", clearFilters);
  $("btn-clear-all-logs").addEventListener("click", clearAllLogs);
  $("logs-per-page").addEventListener("change", () => {
    page = 1;
    loadLogs(true);
  });

  loadLogs();
})();
