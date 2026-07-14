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
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-anti-capture]");
  if (!root) return;
  if (root.getAttribute("data-is-admin") === "true") return;   // admins exempt

  var username = root.getAttribute("data-username") || "user";
  var webcamCapture = root.getAttribute("data-webcam-capture") === "true";
  var cameraEnrolled = root.getAttribute("data-camera-ok") === "true";
  var previewModal = document.getElementById("preview-modal");
  var watermarkLayer = document.getElementById("watermark-layer");

  // ── webcam snapshot (on-demand: light blinks on, one frame, released) ───────
  function ensureShot() {
    return new Promise(function (resolve) {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        resolve(null); return;                 // no camera / insecure origin (http)
      }
      navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false })
        .then(function (stream) {
          var video = document.createElement("video");
          video.muted = true;
          video.playsInline = true;
          video.srcObject = stream;
          var done = false;
          function finish(dataUrl) {
            if (done) return;
            done = true;
            try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
            resolve(dataUrl);
          }
          video.play().catch(function () {});
          // Give the sensor a moment to expose, then grab a single frame.
          setTimeout(function () {
            try {
              var w = video.videoWidth || 640, h = video.videoHeight || 480;
              var canvas = document.createElement("canvas");
              canvas.width = w; canvas.height = h;
              canvas.getContext("2d").drawImage(video, 0, 0, w, h);
              finish(canvas.toDataURL("image/jpeg", 0.7));
            } catch (e) { finish(null); }
          }, 350);
        })
        .catch(function () { resolve(null); });   // denied / unavailable
    });
  }

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
  function send(kind, method, photo) {
    try {
      fetch("/api/log/capture", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        keepalive: true,
        body: JSON.stringify({ kind: kind, method: method, key: currentPreviewKey(), photo: photo || null })
      }).catch(function () {});
    } catch (e) { /* never let logging break the page */ }
  }
  function report(kind, method) {
    // Grab a webcam photo of whoever triggered it (when the feature is on), then log.
    if (webcamCapture) {
      ensureShot().then(function (photo) { send(kind, method, photo); });
    } else {
      send(kind, method, null);
    }
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

  // ── first-use camera enrolment gate ────────────────────────────────────────
  // Blocks the page until the user allows the camera. The server also refuses
  // recording bytes until enrolment succeeds, so this is UX on top of enforcement.
  if (webcamCapture && !cameraEnrolled) {
    var gate = document.getElementById("camera-gate");
    var allowBtn = document.getElementById("camera-gate-allow");
    var statusEl = document.getElementById("camera-gate-status");
    if (gate && allowBtn) {
      gate.style.display = "flex";
      document.body.style.overflow = "hidden";

      function unlock() {
        gate.style.display = "none";
        document.body.style.overflow = "";
        cameraEnrolled = true;
      }
      function setStatus(msg, bad) {
        if (statusEl) {
          statusEl.textContent = msg || "";
          statusEl.className = "camera-gate-status" + (bad ? " bad" : "");
        }
      }
      function enrolDenied(reason) {
        try {
          fetch("/api/camera/enroll", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            cache: "no-store",
            body: JSON.stringify({ denied: true, reason: reason || "" })
          }).catch(function () {});
        } catch (e) {}
      }

      allowBtn.addEventListener("click", function () {
        allowBtn.disabled = true;
        allowBtn.innerHTML = '<span class="spinner"></span> Checking camera…';
        setStatus("");
        ensureShot().then(function (photo) {
          if (!photo) {
            enrolDenied("no_camera_or_denied");
            setStatus("Camera access was blocked or unavailable. Allow the camera in your browser, then try again — recordings stay locked until you do.", true);
            allowBtn.disabled = false;
            allowBtn.textContent = "Allow camera & continue";
            return;
          }
          fetch("/api/camera/enroll", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            cache: "no-store",
            body: JSON.stringify({ photo: photo })
          }).then(function (r) { return r.json().catch(function () { return {}; }); })
            .then(function (d) {
              if (d && d.ok) { unlock(); }
              else {
                setStatus((d && d.error) || "Could not verify the camera. Please try again.", true);
                allowBtn.disabled = false;
                allowBtn.textContent = "Allow camera & continue";
              }
            })
            .catch(function () {
              setStatus("Network error verifying the camera. Please try again.", true);
              allowBtn.disabled = false;
              allowBtn.textContent = "Allow camera & continue";
            });
        });
      });
    }
  }
})();
