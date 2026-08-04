/* anti-capture.js — best-effort screenshot/recording DETERRENCE for non-admins.
 *
 * IMPORTANT / honest limits: a web page CANNOT truly detect or block OS-level
 * screenshots or screen recording (PrintScreen, Win+Shift+S, Snipping Tool,
 * Xbox Game Bar, OBS, macOS/phone capture, or a second camera all run below the
 * browser). This layer only:
 *   1. deters (disables right-click / drag on the page),
 *   2. WATERMARKS every recording preview with the viewer's name + time so any
 *      leaked screenshot/recording is traceable to that user (the real control),
 *   3. detects the PrintScreen key and a focus-loss heuristic while a recording
 *      is open, warns the user, and reports the signal to the audit log,
 *   4. best-effort clears the clipboard after PrintScreen.
 * Admins are fully exempt: no watermark, no warnings, nothing logged.
 *
 * This layer NEVER accesses the camera and never captures an image of anyone.
 * A capture signal produces an audit-log row only.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-anti-capture]");
  if (!root) return;
  if (root.getAttribute("data-is-admin") === "true") return;   // admins exempt

  var username = root.getAttribute("data-username") || "user";
  var previewModal = document.getElementById("preview-modal");
  var watermarkLayer = document.getElementById("watermark-layer");

  // ── on-screen warning toast ────────────────────────────────────────────────
  var toastTimer = 0;
  function warn(message) {
    var toast = document.getElementById("capture-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "capture-toast";
      toast.className = "capture-toast";
      toast.setAttribute("role", "alert");
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    // force reflow so re-triggering restarts the animation
    void toast.offsetWidth;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.classList.remove("show"); }, 5000);
  }

  // ── report a signal to the audit log ───────────────────────────────────────
  function currentPreviewKey() {
    if (previewModal && previewModal.style.display !== "none" && previewModal.dataset.previewKey) {
      return previewModal.dataset.previewKey;
    }
    return "";
  }
  function report(kind, method) {
    try {
      fetch("/api/log/capture", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        keepalive: true,
        body: JSON.stringify({ kind: kind, method: method, key: currentPreviewKey() })
      }).catch(function () {});
    } catch (e) { /* never let logging break the page */ }
  }

  // ── best-effort clipboard overwrite ────────────────────────────────────────
  function clearClipboard() {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText("").catch(function () {});
      }
    } catch (e) { /* ignore — permission/focus dependent */ }
  }

  // ── PrintScreen key ────────────────────────────────────────────────────────
  document.addEventListener("keydown", function (e) {
    if (e.key === "PrintScreen" || e.code === "PrintScreen") e.preventDefault();
  });
  document.addEventListener("keyup", function (e) {
    if (e.key === "PrintScreen" || e.code === "PrintScreen") {
      clearClipboard();
      warn("⚠ Screenshots are not allowed. This was recorded and linked to your account (" + username + ").");
      report("screenshot", "printscreen");
    }
  });

  // ── focus-loss heuristic, ONLY while a recording preview is open ───────────
  // Win+Shift+S / Snipping Tool steal focus; alt-tab does too, so we restrict
  // this to the moment a recording is actually on screen to cut false alarms.
  var lastSignal = 0;
  function maybeCapture(method) {
    if (currentPreviewKey() === "") return;
    var now = new Date().getTime();
    if (now - lastSignal < 4000) return;              // debounce bursts
    lastSignal = now;
    warn("⚠ Possible screen capture detected while viewing a recording. This was recorded and linked to your account.");
    report("screen_capture_suspected", method);
  }
  window.addEventListener("blur", function () { maybeCapture("window_blur"); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) maybeCapture("visibility_hidden");
  });

  // ── deterrents: block right-click + drag (page-wide for non-admins) ────────
  document.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  document.addEventListener("dragstart", function (e) { e.preventDefault(); });

  // ── traceable watermark over the recording preview ─────────────────────────
  function xmlEsc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function watermarkUrl(text) {
    var svg =
      "<svg xmlns='http://www.w3.org/2000/svg' width='330' height='168'>" +
      "<text x='12' y='96' fill='rgba(255,255,255,0.16)' font-size='15' " +
      "font-family='sans-serif' transform='rotate(-22 12 96)'>" + xmlEsc(text) + "</text></svg>";
    return "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svg)));
  }
  function refreshWatermark() {
    if (!watermarkLayer) return;
    var stamp = new Date().toLocaleString();
    watermarkLayer.style.backgroundImage = "url(\"" + watermarkUrl(username + " • " + stamp) + "\")";
  }

  var wmTimer = 0;
  function syncModal() {
    if (!previewModal) return;
    var open = previewModal.style.display !== "none";
    if (open) {
      if (watermarkLayer) watermarkLayer.style.display = "block";
      refreshWatermark();
      if (!wmTimer) wmTimer = setInterval(refreshWatermark, 15000);   // keep time fresh
    } else {
      if (watermarkLayer) watermarkLayer.style.display = "none";
      if (wmTimer) { clearInterval(wmTimer); wmTimer = 0; }
    }
  }
  if (previewModal && window.MutationObserver) {
    new MutationObserver(syncModal).observe(previewModal, {
      attributes: true, attributeFilter: ["style"]
    });
    syncModal();
  }
})();
