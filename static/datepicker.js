/* datepicker.js — an interactive calendar that selects a single day OR a range.
 *
 * Selection rule (the one people already know from booking sites): the first
 * click sets the start, the second sets the end, and a third starts over. Apply
 * with only a start picked means that ONE day — so a single date needs no mode
 * switch, it is just a range you stopped short of extending.
 *
 * The field stays typeable: "2026-06-10", "2026-06-10 to 2026-06-20" and the
 * partial "2026-06" (a whole month) all still work, and anything the calendar
 * cannot express is handed back verbatim as free text for the server's substring
 * date filter.
 *
 * Exposes window.DateRangePicker(options) -> { get, set, clear, close }.
 */
(function () {
  "use strict";

  var ISO_RE = /^\d{4}-\d{2}-\d{2}$/;
  // Two full ISO dates with almost any separator between them. Both sides are
  // anchored, so the "-" separator can never be confused with a date's own hyphens.
  var RANGE_RE =
    /^(\d{4}-\d{2}-\d{2})\s*(?:to|\.\.+|→|–|—|~|,|\/|-)\s*(\d{4}-\d{2}-\d{2})$/i;
  var MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"];
  var WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
  // Recordings are filed in S3 under their IST date, so every preset ("Today",
  // "Last 7 days") is anchored to today IN IST. Anchoring to the device clock
  // would ask a US-based user for yesterday's folder for half of their day.
  var FILING_ZONE = "Asia/Kolkata";

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function iso(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function fromIso(s) {
    var p = String(s).split("-");
    return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
  }
  function shift(isoDate, days) {
    var d = fromIso(isoDate);
    d.setDate(d.getDate() + days);
    return iso(d);
  }

  function todayIso() {
    try {
      return new Intl.DateTimeFormat("en-CA", {
        timeZone: FILING_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
      }).format(new Date());
    } catch (e) {
      return iso(new Date());          // no Intl/tz data — the device date will do
    }
  }

  function monthStart(isoDate) { return isoDate.slice(0, 8) + "01"; }
  function monthEnd(isoDate) {
    var d = fromIso(isoDate);
    return iso(new Date(d.getFullYear(), d.getMonth() + 1, 0));
  }

  function label(from, to) {
    if (!from && !to) return "";
    if (from && to) return from === to ? from : from + " → " + to;
    return from ? "from " + from : "until " + to;
  }

  /**
   * opts:
   *   input     the text input the picker is attached to (required)
   *   onApply   called with { from, to, raw } when the user applies or clears
   */
  window.DateRangePicker = function (opts) {
    var input = opts.input;
    var onApply = opts.onApply || function () {};
    var state = { from: "", to: "", raw: input.value || "" };
    var pending = "";                 // start of a range whose end is not chosen yet
    var hover = "";
    var view = fromIso(todayIso());   // month currently on screen

    var pop = document.createElement("div");
    pop.className = "dp-pop";
    pop.hidden = true;
    pop.innerHTML =
      '<div class="dp-presets">' +
      '<button type="button" class="dp-preset" data-preset="today">Today</button>' +
      '<button type="button" class="dp-preset" data-preset="yesterday">Yesterday</button>' +
      '<button type="button" class="dp-preset" data-preset="7">Last 7 days</button>' +
      '<button type="button" class="dp-preset" data-preset="30">Last 30 days</button>' +
      '<button type="button" class="dp-preset" data-preset="month">This month</button>' +
      '<button type="button" class="dp-preset" data-preset="lastmonth">Last month</button>' +
      '<button type="button" class="dp-preset" data-preset="year">This year</button>' +
      "</div>" +
      '<div class="dp-main">' +
      '<div class="dp-head">' +
      '<button type="button" class="dp-nav" data-nav="-1" aria-label="Previous month">‹</button>' +
      '<select class="dp-month" aria-label="Month"></select>' +
      '<select class="dp-year" aria-label="Year"></select>' +
      '<button type="button" class="dp-nav" data-nav="1" aria-label="Next month">›</button>' +
      "</div>" +
      '<div class="dp-week">' + WEEKDAYS.map(function (w) {
        return "<span>" + w + "</span>";
      }).join("") + "</div>" +
      '<div class="dp-days"></div>' +
      '<div class="dp-foot">' +
      '<span class="dp-sel"></span><span class="dp-spacer"></span>' +
      '<button type="button" class="btn btn-ghost btn-sm" data-act="clear">Clear</button>' +
      '<button type="button" class="btn btn-primary btn-sm" data-act="apply">Apply</button>' +
      "</div></div>";

    var host = input.parentNode;
    host.classList.add("dp-field");
    host.appendChild(pop);

    var monthSel = pop.querySelector(".dp-month");
    var yearSel = pop.querySelector(".dp-year");
    var daysBox = pop.querySelector(".dp-days");
    var selText = pop.querySelector(".dp-sel");

    monthSel.innerHTML = MONTHS.map(function (m, i) {
      return '<option value="' + i + '">' + m + "</option>";
    }).join("");
    var thisYear = fromIso(todayIso()).getFullYear();
    var years = [];
    for (var y = thisYear + 1; y >= thisYear - 8; y--) years.push(y);
    yearSel.innerHTML = years.map(function (v) {
      return '<option value="' + v + '">' + v + "</option>";
    }).join("");

    // ── what the calendar is currently proposing ────────────────────────────
    function draftRange() {
      if (pending) {
        var other = hover || pending;
        return pending <= other ? { from: pending, to: other } : { from: other, to: pending };
      }
      return { from: state.from, to: state.to };
    }

    function renderDays() {
      var today = todayIso();
      var draft = draftRange();
      var first = new Date(view.getFullYear(), view.getMonth(), 1);
      var cursor = new Date(first);
      cursor.setDate(1 - first.getDay());          // back up to the Sunday of week 1
      var html = "";
      for (var i = 0; i < 42; i++) {
        var value = iso(cursor);
        var cls = ["dp-day"];
        if (cursor.getMonth() !== view.getMonth()) cls.push("is-out");
        if (value === today) cls.push("is-today");
        if (draft.from && draft.to && value > draft.from && value < draft.to) cls.push("is-in");
        if (value === draft.from) cls.push("is-start");
        if (value === draft.to) cls.push("is-end");
        if (pending && !hover && value === pending) cls.push("is-pending");
        html += '<button type="button" class="' + cls.join(" ") + '" data-date="' + value +
          '">' + cursor.getDate() + "</button>";
        cursor.setDate(cursor.getDate() + 1);
      }
      daysBox.innerHTML = html;
    }

    function renderFoot() {
      if (pending && !state.to) {
        selText.textContent = pending + " → pick an end date, or Apply for that single day";
        return;
      }
      var text = label(state.from, state.to);
      selText.textContent = text || "No date selected";
    }

    function render() {
      // A year outside the fixed dropdown range (an old recording) is added on the
      // fly, so navigating there never silently snaps the selection somewhere else.
      if (!yearSel.querySelector('option[value="' + view.getFullYear() + '"]')) {
        var extra = document.createElement("option");
        extra.value = String(view.getFullYear());
        extra.textContent = String(view.getFullYear());
        yearSel.appendChild(extra);
      }
      monthSel.value = String(view.getMonth());
      yearSel.value = String(view.getFullYear());
      renderDays();
      renderFoot();
    }

    function setSelection(from, to) {
      state.from = from || "";
      state.to = to || "";
      pending = "";
      hover = "";
      if (state.from) view = fromIso(state.from);
      render();
    }

    function commit(from, to) {
      state.from = from || "";
      state.to = to || "";
      state.raw = label(state.from, state.to);
      input.value = state.raw;
      input.classList.toggle("has-value", !!state.raw);
      pending = "";
      hover = "";
      close();
      onApply({ from: state.from, to: state.to, raw: state.raw });
    }

    // ── typed text ──────────────────────────────────────────────────────────
    function parseTyped() {
      var text = (input.value || "").trim();
      state.raw = text;
      var m = text.match(RANGE_RE);
      if (m) {
        var a = m[1], b = m[2];
        state.from = a <= b ? a : b;
        state.to = a <= b ? b : a;
      } else if (ISO_RE.test(text)) {
        state.from = state.to = text;
      } else {
        // "2026-06", "June", anything else: not a range the calendar can show —
        // it goes to the server as a free-text date filter, untouched.
        state.from = state.to = "";
      }
      input.classList.toggle("has-value", !!text);
      if (state.from) view = fromIso(state.from);
      pending = "";
      hover = "";
      if (!pop.hidden) render();
    }

    // ── open / close ────────────────────────────────────────────────────────
    function open() {
      if (!pop.hidden) return;
      pending = "";
      hover = "";
      if (state.from) view = fromIso(state.from);
      pop.hidden = false;
      host.classList.add("dp-open");
      render();
      // The calendar is wider than its field, so a field near the right edge would
      // push it off screen. Measure once it is laid out, and flip it if needed.
      pop.style.left = "0";
      pop.style.right = "auto";
      if (pop.getBoundingClientRect().right > document.documentElement.clientWidth - 8) {
        pop.style.left = "auto";
        pop.style.right = "0";
      }
    }
    function close() {
      pop.hidden = true;
      host.classList.remove("dp-open");
    }

    // ── events ──────────────────────────────────────────────────────────────
    input.addEventListener("focus", open);
    input.addEventListener("click", open);
    input.addEventListener("input", parseTyped);
    input.addEventListener("change", parseTyped);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !pop.hidden) { e.stopPropagation(); close(); }
      // Enter inside an open calendar means "use what I picked", not "submit the
      // form with the half-finished range still on screen".
      if (e.key === "Enter" && !pop.hidden && (pending || state.from)) {
        e.preventDefault();
        applyDraft();
      }
    });

    daysBox.addEventListener("click", function (e) {
      var cell = e.target.closest(".dp-day");
      if (!cell) return;
      var value = cell.dataset.date;
      // `pending` is set only between the two clicks of a range, so its absence
      // means this click starts a fresh selection (first click, or a third one).
      if (!pending) {
        pending = value;
        state.from = value;
        state.to = "";
      } else {
        var start = pending;
        state.from = start <= value ? start : value;
        state.to = start <= value ? value : start;
        pending = "";
      }
      hover = "";
      render();
    });
    daysBox.addEventListener("mouseover", function (e) {
      var cell = e.target.closest(".dp-day");
      if (!cell || !pending || state.to) return;
      hover = cell.dataset.date;
      renderDays();
    });
    daysBox.addEventListener("mouseleave", function () {
      if (!hover) return;
      hover = "";
      renderDays();
    });

    pop.querySelectorAll(".dp-nav").forEach(function (b) {
      b.addEventListener("click", function () {
        view = new Date(view.getFullYear(), view.getMonth() + Number(b.dataset.nav), 1);
        render();
      });
    });
    monthSel.addEventListener("change", function () {
      view = new Date(view.getFullYear(), Number(monthSel.value), 1);
      render();
    });
    yearSel.addEventListener("change", function () {
      view = new Date(Number(yearSel.value), view.getMonth(), 1);
      render();
    });

    pop.querySelectorAll(".dp-preset").forEach(function (b) {
      b.addEventListener("click", function () {
        var today = todayIso();
        var p = b.dataset.preset;
        if (p === "today") return commit(today, today);
        if (p === "yesterday") return commit(shift(today, -1), shift(today, -1));
        if (p === "7") return commit(shift(today, -6), today);
        if (p === "30") return commit(shift(today, -29), today);
        if (p === "month") return commit(monthStart(today), monthEnd(today));
        if (p === "lastmonth") {
          var prev = shift(monthStart(today), -1);
          return commit(monthStart(prev), monthEnd(prev));
        }
        if (p === "year") return commit(today.slice(0, 4) + "-01-01", today);
      });
    });

    function applyDraft() {
      // A start with no end is a single day — that is what "Apply" means here.
      var from = state.from || pending;
      var to = state.to || from;
      if (!from) return commit("", "");
      commit(from, to);
    }

    pop.querySelector('[data-act="apply"]').addEventListener("click", applyDraft);
    pop.querySelector('[data-act="clear"]').addEventListener("click", function () {
      setSelection("", "");
      commit("", "");
    });

    document.addEventListener("click", function (e) {
      if (!pop.hidden && !host.contains(e.target)) close();
    });

    parseTyped();

    return {
      element: pop,
      get: function () { return { from: state.from, to: state.to, raw: state.raw }; },
      set: function (from, to) {
        setSelection(from, to);
        state.raw = label(from, to);
        input.value = state.raw;
        input.classList.toggle("has-value", !!state.raw);
      },
      clear: function () {
        setSelection("", "");
        state.raw = "";
        input.value = "";
        input.classList.remove("has-value");
        close();
      },
      close: close,
    };
  };
})();
