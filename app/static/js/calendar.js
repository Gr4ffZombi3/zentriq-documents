// Gemeinsamer Buerokalender. Alle Zeiten sind Berliner Wanduhrzeit ("YYYY-MM-DDTHH:MM" vom
// Server) und werden als Zeichenketten/Minuten gerechnet - unabhaengig von der Zeitzone des
// Geraets. Datumsarithmetik ueber Date.UTC (keine Sommerzeit-Spruenge im Browser).
// Inhalte werden nur per textContent eingesetzt.
(function () {
  var root = document.querySelector("[data-cal]");
  if (!root) return;
  var cfg = JSON.parse(document.getElementById("cal-data").textContent);
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  // HOUR_PX folgt --cal-hour aus dem CSS (Desktop 48px, Handy 56px) - sonst stehen Termine falsch.
  var HOUR_PX = 48, SNAP = 15, DAY_START_SCROLL = 7, RESIZE_MIN_PX = 36;
  var WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
  var WEEKDAYS_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
  var MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];

  var els = {
    body: root.querySelector("[data-cal-body]"), period: root.querySelector("[data-cal-period]"),
    mini: root.querySelector("[data-cal-mini]"), filter: root.querySelector("[data-cal-filter]"),
    side: root.querySelector("[data-cal-side]"), sideToggle: root.querySelector("[data-cal-side-toggle]"),
    status: root.querySelector("[data-cal-status]"), pop: document.querySelector("[data-cal-pop]"),
    dialog: document.querySelector("[data-cal-dialog]"), form: document.querySelector("[data-cal-form]"),
  };
  var small = window.matchMedia("(max-width: 47.99rem)");
  var state = {
    view: localStorage.getItem("zentriq-cal-view") || (small.matches ? "list" : "workweek"),
    anchor: cfg.today, events: [], members: cfg.members, stamp: null,
    hidden: JSON.parse(localStorage.getItem("zentriq-cal-hidden") || "[]"),
    busy: false, editing: null, dragging: false,
  };
  if (small.matches && (state.view === "week" || state.view === "workweek")) state.view = "day";

  // --- Datum/Zeit-Helfer ---------------------------------------------------------------------
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function parseDay(s) { var p = s.split("-"); return Date.UTC(+p[0], +p[1] - 1, +p[2]); }
  function fmtDay(t) { var d = new Date(t); return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()); }
  function addDays(s, n) { return fmtDay(parseDay(s) + n * 86400000); }
  function weekday(s) { return (new Date(parseDay(s)).getUTCDay() + 6) % 7; } // 0 = Montag
  function dayOf(iso) { return iso.slice(0, 10); }
  function minutesOf(iso) { return +iso.slice(11, 13) * 60 + +iso.slice(14, 16); }
  function isoAt(day, minutes) {
    var extra = Math.floor(minutes / 1440); minutes -= extra * 1440;
    return addDays(day, extra) + "T" + pad(Math.floor(minutes / 60)) + ":" + pad(minutes % 60);
  }
  function diffMinutes(a, b) { // b - a, beides Wanduhrzeit
    return (parseDay(dayOf(b)) - parseDay(dayOf(a))) / 60000 + minutesOf(b) - minutesOf(a);
  }
  function addMinutes(iso, n) { return isoAt(dayOf(iso), minutesOf(iso) + n); }
  function hm(iso) { return iso.slice(11, 16); }
  function dayLabel(s, long) {
    var d = new Date(parseDay(s));
    return (long ? WEEKDAYS_LONG : WEEKDAYS)[weekday(s)] + ", " + d.getUTCDate() + "." + (long ? " " + MONTHS[d.getUTCMonth()] : (d.getUTCMonth() + 1) + ".");
  }
  function berlinNow() {
    var parts = {};
    new Intl.DateTimeFormat("de-DE", { timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(new Date()).forEach(function (p) { parts[p.type] = p.value; });
    return parts.year + "-" + parts.month + "-" + parts.day + "T" + parts.hour + ":" + parts.minute;
  }

  // --- HTTP ----------------------------------------------------------------------------------
  function request(method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.headers["X-CSRFToken"] = csrfMeta ? csrfMeta.content : ""; opts.body = JSON.stringify(body); }
    return fetch(cfg.api + path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { var e = new Error(d.error || "Aktion nicht möglich."); e.data = d; e.status = r.status; throw e; }
        return d;
      });
    }, function () { throw new Error("Keine Verbindung zum Server. Bitte erneut versuchen."); });
  }

  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function status(msg, tone) {
    els.status.textContent = msg || ""; els.status.hidden = !msg;
    els.status.className = "cal-status" + (tone ? " is-" + tone : "");
    if (msg && tone !== "error") setTimeout(function () { if (els.status.textContent === msg) status(""); }, 5000);
  }
  function member(id) { for (var i = 0; i < state.members.length; i++) if (state.members[i].id === id) return state.members[i]; return null; }
  function colorOf(id) { var m = member(id); return m ? m.color : "slate"; }

  // --- Zeitraum ------------------------------------------------------------------------------
  function range() {
    var a = state.anchor, v = state.view;
    if (v === "day") return [a, a];
    if (v === "workweek") { var s = addDays(a, -weekday(a)); return [s, addDays(s, 4)]; }
    if (v === "week") { var w = addDays(a, -weekday(a)); return [w, addDays(w, 6)]; }
    if (v === "list") return [a, addDays(a, 13)];
    var first = a.slice(0, 8) + "01", start = addDays(first, -weekday(first));
    return [start, addDays(start, 41)];
  }
  function days() { var r = range(), out = [], d = r[0]; while (d <= r[1]) { out.push(d); d = addDays(d, 1); } return out; }
  function periodLabel() {
    var r = range(), a = new Date(parseDay(state.anchor));
    if (state.view === "month") return MONTHS[a.getUTCMonth()] + " " + a.getUTCFullYear();
    if (state.view === "day") return dayLabel(r[0], true) + " " + a.getUTCFullYear();
    var s = new Date(parseDay(r[0])), e = new Date(parseDay(r[1]));
    return s.getUTCDate() + "." + (s.getUTCMonth() + 1) + ". – " + e.getUTCDate() + "." + (e.getUTCMonth() + 1) + "." + e.getUTCFullYear();
  }
  function step(dir) {
    var v = state.view, a = state.anchor;
    if (v === "day") state.anchor = addDays(a, dir);
    else if (v === "month") { var d = new Date(parseDay(a)); d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() + dir); state.anchor = fmtDay(d.getTime()); }
    else if (v === "list") state.anchor = addDays(a, 14 * dir);
    else state.anchor = addDays(a, 7 * dir);
    load();
  }

  // --- Laden / Aktualisieren -----------------------------------------------------------------
  function load() {
    var r = range();
    els.body.setAttribute("aria-busy", "true");
    return request("GET", "/termine?von=" + r[0] + "&bis=" + r[1]).then(function (d) {
      state.events = d.events; state.members = d.members; state.stamp = d.stamp;
      render();
    }, function (err) { status(err.message, "error"); }).finally(function () { els.body.setAttribute("aria-busy", "false"); });
  }
  function poll() {
    if (document.hidden || state.dragging || state.busy) return;
    request("GET", "/stand").then(function (d) {
      if (state.stamp && d.stamp !== state.stamp && !els.dialog.open) { load(); }
    }, function () {});
  }
  setInterval(poll, 20000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

  function visibleEvents() {
    return state.events.filter(function (e) { return state.hidden.indexOf(e.assignee_id) === -1; });
  }

  // --- Darstellung ---------------------------------------------------------------------------
  function render() {
    els.period.textContent = periodLabel();
    Array.prototype.forEach.call(root.querySelectorAll("[data-cal-view]"), function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.calView === state.view));
    });
    renderMini(); renderFilter();
    els.body.textContent = "";
    if (state.view === "month") renderMonth();
    else if (state.view === "list") renderList();
    else renderGrid();
  }

  function renderFilter() {
    els.filter.textContent = "";
    state.members.forEach(function (m) {
      var label = el("label", "cal-filter-item");
      var cb = el("input"); cb.type = "checkbox"; cb.checked = state.hidden.indexOf(m.id) === -1;
      cb.addEventListener("change", function () {
        state.hidden = cb.checked ? state.hidden.filter(function (x) { return x !== m.id; }) : state.hidden.concat([m.id]);
        localStorage.setItem("zentriq-cal-hidden", JSON.stringify(state.hidden));
        render();
      });
      label.appendChild(cb);
      label.appendChild(el("span", "cal-swatch cal-c-" + m.color));
      label.appendChild(el("span", "cal-filter-name", m.name + (m.id === cfg.me ? " (ich)" : "")));
      els.filter.appendChild(label);
    });
  }

  function renderMini() {
    var a = state.anchor, first = a.slice(0, 8) + "01", start = addDays(first, -weekday(first));
    var d = new Date(parseDay(a));
    els.mini.textContent = "";
    var head = el("div", "cal-mini-head");
    var prev = el("button", "zs-icon-btn", "‹"); prev.type = "button"; prev.setAttribute("aria-label", "Vorheriger Monat");
    var next = el("button", "zs-icon-btn", "›"); next.type = "button"; next.setAttribute("aria-label", "Nächster Monat");
    prev.onclick = function () { var x = new Date(parseDay(first)); x.setUTCMonth(x.getUTCMonth() - 1); state.anchor = fmtDay(x.getTime()); load(); };
    next.onclick = function () { var x = new Date(parseDay(first)); x.setUTCMonth(x.getUTCMonth() + 1); state.anchor = fmtDay(x.getTime()); load(); };
    head.appendChild(prev); head.appendChild(el("span", null, MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear())); head.appendChild(next);
    els.mini.appendChild(head);
    var grid = el("div", "cal-mini-grid"); grid.setAttribute("role", "grid");
    WEEKDAYS.forEach(function (w) { grid.appendChild(el("span", "cal-mini-wd", w)); });
    var r = range();
    for (var i = 0; i < 42; i++) {
      var day = addDays(start, i);
      var b = el("button", "cal-mini-day", String(new Date(parseDay(day)).getUTCDate()));
      b.type = "button";
      if (day.slice(0, 7) !== first.slice(0, 7)) b.classList.add("is-other");
      if (day === cfg.today) b.classList.add("is-today");
      if (day >= r[0] && day <= r[1] && state.view !== "month") b.classList.add("is-range");
      b.setAttribute("aria-label", dayLabel(day, true));
      (function (target) { b.onclick = function () { state.anchor = target; if (small.matches) toggleSide(false); load(); }; })(day);
      grid.appendChild(b);
    }
    els.mini.appendChild(grid);
  }

  // Ueberlappende Termine nebeneinander: Cluster bilden, Spalten gierig verteilen.
  function layoutDay(items) {
    items.sort(function (x, y) { return x.s - y.s || y.e - x.e; });
    var clusters = [], cur = null, curEnd = -1;
    items.forEach(function (it) {
      if (!cur || it.s >= curEnd) { cur = []; clusters.push(cur); curEnd = it.e; }
      cur.push(it); curEnd = Math.max(curEnd, it.e);
    });
    clusters.forEach(function (cl) {
      var cols = [];
      cl.forEach(function (it) {
        for (var c = 0; c < cols.length; c++) if (cols[c] <= it.s) break;
        cols[c] = it.e; it.col = c;
      });
      cl.forEach(function (it) { it.cols = cols.length; });
    });
    return items;
  }

  function segmentsFor(day) {
    var out = [];
    visibleEvents().forEach(function (e) {
      var sDay = dayOf(e.start), eDay = dayOf(e.end);
      if (day < sDay || day > eDay || (day === eDay && minutesOf(e.end) === 0 && sDay !== eDay)) return;
      var s = day === sDay ? minutesOf(e.start) : 0;
      var en = day === eDay ? minutesOf(e.end) : 1440;
      if (en <= s) en = Math.min(1440, s + 15);
      out.push({ ev: e, s: s, e: en, startsHere: day === sDay, endsHere: day === eDay });
    });
    return layoutDay(out);
  }

  function eventLabel(e) {
    return e.title + ", " + hm(e.start) + " bis " + hm(e.end) + " Uhr, " + e.assignee + (e.status === "cancelled" ? ", abgesagt" : "");
  }

  function renderGrid() {
    var list = days(), now = berlinNow();
    var wrap = el("div", "cal-grid"); wrap.style.setProperty("--cal-days", list.length);
    els.body.appendChild(wrap);
    HOUR_PX = parseFloat(getComputedStyle(wrap).getPropertyValue("--cal-hour")) || 48;
    els.body.removeChild(wrap);
    var head = el("div", "cal-grid-head");
    head.appendChild(el("div", "cal-gutter"));
    list.forEach(function (day) {
      var h = el("button", "cal-day-head" + (day === cfg.today ? " is-today" : ""));
      h.type = "button";
      h.appendChild(el("span", "cal-day-wd", WEEKDAYS[weekday(day)]));
      h.appendChild(el("span", "cal-day-num", String(new Date(parseDay(day)).getUTCDate())));
      h.setAttribute("aria-label", dayLabel(day, true) + " – Tagesansicht");
      h.onclick = function () { state.view = "day"; state.anchor = day; saveView(); load(); };
      head.appendChild(h);
    });
    wrap.appendChild(head);
    var scroll = el("div", "cal-grid-scroll");
    var body = el("div", "cal-grid-body");
    var gutter = el("div", "cal-gutter");
    for (var hr = 0; hr < 24; hr++) gutter.appendChild(el("span", "cal-hour", hr === 0 ? "" : pad(hr) + ":00"));
    body.appendChild(gutter);
    list.forEach(function (day) {
      var col = el("div", "cal-col" + (day === cfg.today ? " is-today" : "") + (weekday(day) > 4 ? " is-weekend" : ""));
      col.dataset.day = day;
      col.setAttribute("aria-label", dayLabel(day, true));
      col.addEventListener("click", function (ev) {
        if (ev.target !== col) return;
        var rect = col.getBoundingClientRect();
        var minutes = Math.floor(((ev.clientY - rect.top) / HOUR_PX * 60) / 30) * 30;
        openForm(null, { start: isoAt(day, minutes), end: isoAt(day, minutes + 30) });
      });
      segmentsFor(day).forEach(function (seg) { col.appendChild(eventBlock(seg, day)); });
      if (day === dayOf(now)) {
        var line = el("div", "cal-now"); line.style.top = (minutesOf(now) / 60 * HOUR_PX) + "px";
        line.setAttribute("aria-hidden", "true"); col.appendChild(line);
      }
      body.appendChild(col);
    });
    scroll.appendChild(body); wrap.appendChild(scroll); els.body.appendChild(wrap);
    scroll.scrollTop = DAY_START_SCROLL * HOUR_PX - 8;
  }

  function eventBlock(seg, day) {
    var e = seg.ev;
    var b = el("button", "cal-ev cal-c-" + colorOf(e.assignee_id) + (e.status === "cancelled" ? " is-cancelled" : ""));
    b.type = "button";
    b.style.top = (seg.s / 60 * HOUR_PX) + "px";
    b.style.height = Math.max(18, (seg.e - seg.s) / 60 * HOUR_PX - 2) + "px";
    b.style.left = "calc(" + (seg.col / seg.cols * 100) + "% + 2px)";
    b.style.width = "calc(" + (100 / seg.cols) + "% - 4px)";
    b.setAttribute("aria-label", eventLabel(e));
    b.dataset.id = e.id;
    // Kurze Termine einzeilig (Titel zuerst), laengere mit Zeit und Zustaendigkeit darunter.
    if (seg.e - seg.s < 45) b.classList.add("is-short");
    b.appendChild(el("span", "cal-ev-title", e.title));
    b.appendChild(el("span", "cal-ev-time", hm(e.start) + "–" + hm(e.end)));
    if (seg.e - seg.s >= 60) b.appendChild(el("span", "cal-ev-meta", e.assignee + (e.customer ? " · " + e.customer.name : "")));
    // Dauer per Maus nur bei ausreichend hohen Bloecken: bei kurzen Terminen (30 Min. = 22px) belegte der
    // Griff ein Drittel des Blocks - Ziehen am unteren Rand verlaengerte den Termin, statt ihn zu verschieben.
    if (seg.endsHere && e.status !== "cancelled" && (seg.e - seg.s) / 60 * HOUR_PX - 2 >= RESIZE_MIN_PX) {
      var handle = el("span", "cal-ev-resize"); handle.setAttribute("aria-hidden", "true"); b.appendChild(handle);
    }
    if (e.status !== "cancelled" && !small.matches) b.title = "Ziehen zum Verschieben, Klick für Details";
    b.addEventListener("pointerdown", function (ev) { startDrag(ev, b, e, ev.target.classList.contains("cal-ev-resize") ? "resize" : "move", day); });
    b.addEventListener("click", function (ev) { if (b.dataset.dragged) { delete b.dataset.dragged; return; } showDetails(e, b); ev.stopPropagation(); });
    b.addEventListener("keydown", function (ev) { keyMove(ev, e); });
    return b;
  }

  // --- Maus: verschieben / Dauer -------------------------------------------------------------
  function startDrag(ev, block, e, mode, day) {
    if (ev.button !== 0 || e.status === "cancelled" || small.matches) return;
    var startX = ev.clientX, startY = ev.clientY, moved = false;
    var cols = Array.prototype.slice.call(els.body.querySelectorAll(".cal-col"));
    var origTop = parseFloat(block.style.top), origH = parseFloat(block.style.height);
    var deltaMin = 0, deltaDay = 0;
    var timeEl = block.querySelector(".cal-ev-time"), origTime = timeEl ? timeEl.textContent : "";
    var duration = diffMinutes(e.start, e.end), startMin = minutesOf(e.start);
    block.setPointerCapture(ev.pointerId);
    function target() {
      if (mode === "resize") return { start: e.start, end: addMinutes(e.end, deltaMin) };
      var start = addMinutes(isoAt(addDays(dayOf(e.start), deltaDay), startMin), deltaMin);
      return { start: start, end: addMinutes(start, duration) };
    }
    function onMove(m) {
      var dy = m.clientY - startY, dx = m.clientX - startX;
      if (!moved && Math.abs(dy) < 5 && Math.abs(dx) < 5) return;
      moved = true; state.dragging = true; block.classList.add("is-dragging");
      deltaMin = Math.round(dy / HOUR_PX * 60 / SNAP) * SNAP;
      if (mode === "resize") {
        deltaMin = Math.max(deltaMin, SNAP - duration);
        block.style.height = Math.max(HOUR_PX / 4 - 2, origH + deltaMin / 60 * HOUR_PX) + "px";
      } else {
        // innerhalb des Tages bleiben (0:00 bis 24:00), Tag ueber die Spalte unter dem Mauszeiger
        deltaMin = Math.min(Math.max(deltaMin, -startMin), 1440 - duration - startMin);
        block.style.top = (origTop + deltaMin / 60 * HOUR_PX) + "px";
        var idx = cols.findIndex(function (c) { var r = c.getBoundingClientRect(); return m.clientX >= r.left && m.clientX < r.right; });
        var from = cols.findIndex(function (c) { return c.dataset.day === day; });
        if (idx >= 0 && from >= 0) { deltaDay = idx - from; block.style.transform = "translateX(" + (cols[idx].getBoundingClientRect().left - cols[from].getBoundingClientRect().left) + "px)"; }
      }
      var t = target();
      var label = (deltaDay ? dayLabel(dayOf(t.start)) + " " : "") + hm(t.start) + "–" + hm(t.end);
      if (timeEl) timeEl.textContent = label;
      block.setAttribute("aria-label", e.title + ", neu: " + label + " Uhr");
    }
    function onUp() {
      block.removeEventListener("pointermove", onMove); block.removeEventListener("pointerup", onUp); block.removeEventListener("pointercancel", onUp);
      state.dragging = false;
      if (!moved) return;
      block.dataset.dragged = "1";
      // Nach dem Loslassen wird der Klick auf den Elternknoten gelenkt, wenn die Maus ueber einer anderen
      // Spalte war - die Markierung darf dann nicht haengen bleiben (naechster Klick soll Details zeigen).
      setTimeout(function () { delete block.dataset.dragged; }, 400);
      var t = target(), start = t.start, end = t.end;
      if (diffMinutes(start, end) < SNAP) end = addMinutes(start, SNAP);
      if (start === e.start && end === e.end) { if (timeEl) timeEl.textContent = origTime; render(); return; }
      block.classList.add("is-saving");
      saveTimes(e, start, end);
    }
    block.addEventListener("pointermove", onMove); block.addEventListener("pointerup", onUp); block.addEventListener("pointercancel", onUp);
  }

  // --- Tastatur: Alt+Pfeile verschieben, Alt+Umschalt+Pfeile Dauer ----------------------------
  function keyMove(ev, e) {
    if (!ev.altKey || e.status === "cancelled") return;
    var k = ev.key, start = e.start, end = e.end;
    if (ev.shiftKey && (k === "ArrowUp" || k === "ArrowDown")) {
      end = addMinutes(end, k === "ArrowUp" ? -SNAP : SNAP);
      if (diffMinutes(start, end) < SNAP) return;
    } else if (k === "ArrowUp" || k === "ArrowDown") {
      var d = k === "ArrowUp" ? -SNAP : SNAP; start = addMinutes(start, d); end = addMinutes(end, d);
    } else if (k === "ArrowLeft" || k === "ArrowRight") {
      var n = k === "ArrowLeft" ? -1 : 1; start = isoAt(addDays(dayOf(start), n), minutesOf(start)); end = isoAt(addDays(dayOf(end), n), minutesOf(end));
    } else return;
    ev.preventDefault();
    saveTimes(e, start, end, true);
  }

  function payloadOf(e) {
    return {
      title: e.title, start: e.start, end: e.end, assignee_id: e.assignee_id,
      customer_id: e.customer ? e.customer.id : null, location_kind: e.location_kind || "",
      location: e.location || "", note: e.note || "", version: e.version,
    };
  }

  function saveTimes(e, start, end, refocus) {
    var body = payloadOf(e); body.start = start; body.end = end;
    state.busy = true;
    request("PATCH", "/termine/" + e.id, body).then(function (d) {
      replaceEvent(d.event); render();
      status("Verschoben: " + d.event.title + ", " + dayLabel(dayOf(d.event.start)) + " " + hm(d.event.start) + "–" + hm(d.event.end) + " Uhr" + conflictText(d.conflicts), d.conflicts.length ? "warn" : "ok");
      if (refocus) { var b = els.body.querySelector('.cal-ev[data-id="' + d.event.id + '"]'); if (b) b.focus(); }
    }, function (err) {
      // Nicht gespeichert: Termin steht wieder an der bisherigen (bzw. vom Server gemeldeten aktuellen) Stelle.
      status("Nicht verschoben: " + err.message, "error"); if (err.data && err.data.event) replaceEvent(err.data.event); render();
    }).finally(function () { state.busy = false; });
  }

  function conflictText(list) {
    if (!list || !list.length) return "";
    return " – Achtung: überschneidet sich mit " + list.map(function (c) { return "„" + c.title + "“ (" + hm(c.start) + "–" + hm(c.end) + ")"; }).join(", ");
  }

  function replaceEvent(ev) {
    var i = state.events.findIndex(function (x) { return x.id === ev.id; });
    if (i >= 0) state.events[i] = ev; else state.events.push(ev);
  }

  // --- Monat / Liste -------------------------------------------------------------------------
  function renderMonth() {
    var list = days(), month = state.anchor.slice(0, 7);
    var grid = el("div", "cal-month");
    WEEKDAYS.forEach(function (w) { grid.appendChild(el("div", "cal-month-wd", w)); });
    list.forEach(function (day) {
      var cell = el("div", "cal-month-cell" + (day.slice(0, 7) !== month ? " is-other" : "") + (day === cfg.today ? " is-today" : ""));
      var num = el("button", "cal-month-num", String(new Date(parseDay(day)).getUTCDate()));
      num.type = "button"; num.setAttribute("aria-label", dayLabel(day, true) + " – Tagesansicht");
      num.onclick = function () { state.view = "day"; state.anchor = day; saveView(); load(); };
      cell.appendChild(num);
      var segs = segmentsFor(day).sort(function (a, b) { return a.s - b.s; });
      segs.slice(0, 3).forEach(function (seg) {
        var chip = el("button", "cal-chip cal-c-" + colorOf(seg.ev.assignee_id) + (seg.ev.status === "cancelled" ? " is-cancelled" : ""), (seg.startsHere ? hm(seg.ev.start) + " " : "") + seg.ev.title);
        chip.type = "button"; chip.setAttribute("aria-label", eventLabel(seg.ev));
        chip.onclick = function (ev) { ev.stopPropagation(); showDetails(seg.ev, chip); };
        cell.appendChild(chip);
      });
      if (segs.length > 3) {
        var more = el("button", "cal-more", "+" + (segs.length - 3) + " weitere"); more.type = "button";
        more.onclick = function () { state.view = "day"; state.anchor = day; saveView(); load(); };
        cell.appendChild(more);
      }
      cell.addEventListener("click", function (ev) { if (ev.target === cell) openForm(null, { start: day + "T09:00", end: day + "T09:30" }); });
      grid.appendChild(cell);
    });
    els.body.appendChild(grid);
  }

  function renderList() {
    var wrap = el("div", "cal-list"), any = false;
    days().forEach(function (day) {
      var segs = segmentsFor(day).sort(function (a, b) { return a.s - b.s; });
      if (!segs.length && day !== state.anchor) return;
      any = any || segs.length > 0;
      var section = el("section", "cal-list-day" + (day === cfg.today ? " is-today" : ""));
      var h = el("h2", "cal-list-head", dayLabel(day, true) + (day === cfg.today ? " · Heute" : ""));
      section.appendChild(h);
      if (!segs.length) section.appendChild(el("p", "cal-empty", "Keine Termine."));
      segs.forEach(function (seg) {
        var e = seg.ev;
        var item = el("button", "cal-list-item cal-c-" + colorOf(e.assignee_id) + (e.status === "cancelled" ? " is-cancelled" : ""));
        item.type = "button"; item.setAttribute("aria-label", eventLabel(e));
        item.appendChild(el("span", "cal-list-time", seg.startsHere ? hm(e.start) + "–" + hm(e.end) : "ganztägig"));
        var txt = el("span", "cal-list-text");
        txt.appendChild(el("strong", null, e.title));
        txt.appendChild(el("span", null, e.assignee + (e.customer ? " · " + e.customer.name : "") + (e.location_label ? " · " + e.location_label : "")));
        item.appendChild(txt);
        item.onclick = function () { showDetails(e, item); };
        section.appendChild(item);
      });
      var add = el("button", "cal-list-add", "+ Termin am " + dayLabel(day)); add.type = "button";
      add.onclick = function () { openForm(null, { start: day + "T09:00", end: day + "T09:30" }); };
      section.appendChild(add);
      wrap.appendChild(section);
    });
    if (!any) wrap.appendChild(el("p", "cal-empty", "In den nächsten 14 Tagen sind keine Termine eingetragen."));
    els.body.appendChild(wrap);
  }

  // --- Details -------------------------------------------------------------------------------
  function hidePop() { els.pop.hidden = true; els.pop.textContent = ""; }
  document.addEventListener("click", function (ev) { if (!els.pop.hidden && !els.pop.contains(ev.target) && !ev.target.closest(".cal-ev, .cal-chip, .cal-list-item")) hidePop(); });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape" && !els.pop.hidden) { var back = els.pop._origin; hidePop(); if (back) back.focus(); } });

  function showDetails(e, origin) {
    var p = els.pop; p.textContent = ""; p._origin = origin;
    var head = el("div", "cal-pop-head");
    var title = el("h2", null, e.title); title.id = "cal-pop-title"; head.appendChild(title);
    var close = el("button", "zs-icon-btn", "×"); close.type = "button"; close.setAttribute("aria-label", "Schließen");
    close.onclick = function () { hidePop(); origin.focus(); }; head.appendChild(close);
    p.appendChild(head);
    if (e.status === "cancelled") p.appendChild(el("p", "cal-pop-cancelled", "Abgesagt"));
    var dl = el("dl", "cal-pop-facts");
    function fact(k, v, node) { if (!v && !node) return; dl.appendChild(el("dt", null, k)); var dd = el("dd", null, node ? null : v); if (node) dd.appendChild(node); dl.appendChild(dd); }
    var same = dayOf(e.start) === dayOf(e.end);
    fact("Wann", dayLabel(dayOf(e.start), true) + ", " + hm(e.start) + "–" + (same ? "" : dayLabel(dayOf(e.end)) + " ") + hm(e.end) + " Uhr");
    var who = el("span", "cal-pop-who"); who.appendChild(el("span", "cal-swatch cal-c-" + colorOf(e.assignee_id))); who.appendChild(document.createTextNode(e.assignee));
    fact("Zuständig", null, who);
    if (e.customer) { var a = el("a", null, e.customer.name); a.href = cfg.customerUrl.replace(/0$/, String(e.customer.id)); fact("Kunde", null, a); }
    fact("Art", e.location_label); fact("Ort", e.location); fact("Notiz", e.note);
    fact("Geändert", (e.updated_by || "–") + ", " + (e.updated_at || ""));
    p.appendChild(dl);
    var actions = el("div", "cal-pop-actions");
    function btn(label, cls, fn) { var b = el("button", cls, label); b.type = "button"; b.onclick = fn; actions.appendChild(b); return b; }
    btn("Bearbeiten", "btn btn-primary btn-sm", function () { hidePop(); openForm(e); });
    if (e.status === "cancelled") btn("Wieder aktivieren", "btn btn-secondary btn-sm", function () { lifecycle(e, "reaktivieren", "Termin wieder aktiviert."); });
    else btn("Absagen", "btn btn-secondary btn-sm", function () { if (confirm("Termin „" + e.title + "“ absagen? Er bleibt sichtbar (durchgestrichen).")) lifecycle(e, "absagen", "Termin abgesagt."); });
    if (e.can_delete) btn("Löschen", "btn btn-ghost btn-sm cal-danger", function () { if (confirm("Termin endgültig löschen? Der Verlauf bleibt protokolliert.")) remove(e); });
    p.appendChild(actions);
    var hist = el("details", "cal-pop-history"); hist.appendChild(el("summary", null, "Verlauf"));
    hist.addEventListener("toggle", function () {
      if (!hist.open || hist.dataset.loaded) return;
      hist.dataset.loaded = "1";
      request("GET", "/termine/" + e.id).then(function (d) {
        var ul = el("ul");
        d.history.forEach(function (h) { ul.appendChild(el("li", null, h.when + " · " + h.action + " von " + h.by + (h.fields.length ? " (" + h.fields.map(fieldLabel).join(", ") + ")" : ""))); });
        hist.appendChild(ul);
      });
    });
    p.appendChild(hist);
    p.hidden = false;
    // Position neben dem Termin (Desktop) bzw. unten als Blatt (Handy, per CSS).
    if (!small.matches) {
      var r = origin.getBoundingClientRect(), w = Math.min(340, window.innerWidth - 24);
      var left = r.right + 8 + w > window.innerWidth ? Math.max(12, r.left - w - 8) : r.right + 8;
      p.style.left = left + "px"; p.style.top = Math.max(12, Math.min(r.top, window.innerHeight - p.offsetHeight - 12)) + "px";
    } else { p.style.left = ""; p.style.top = ""; }
    close.focus();
  }
  function fieldLabel(f) {
    return { title: "Titel", starts_at: "Beginn", ends_at: "Ende", assignee_user_id: "Zuständig", customer_id: "Kunde", location_kind: "Art", location: "Ort", note: "Notiz" }[f] || f;
  }
  function lifecycle(e, action, msg) {
    request("POST", "/termine/" + e.id + "/" + action, { version: e.version }).then(function (d) { replaceEvent(d.event); hidePop(); render(); status(msg, "ok"); },
      function (err) { status(err.message, "error"); if (err.status === 409) load(); });
  }
  function remove(e) {
    request("DELETE", "/termine/" + e.id, { version: e.version }).then(function () {
      state.events = state.events.filter(function (x) { return x.id !== e.id; }); hidePop(); render(); status("Termin gelöscht.", "ok");
    }, function (err) { status(err.message, "error"); if (err.status === 409) load(); });
  }

  // --- Formular ------------------------------------------------------------------------------
  var f = els.form, saveBtn = f.querySelector("[data-cal-save]"), errEl = f.querySelector("[data-cal-form-error]");
  var conflictEl = f.querySelector("[data-cal-conflicts]"), custInput = f.querySelector("[data-cal-customer-input]");
  var custList = f.querySelector("[data-cal-customer-list]"), assigneeSel = f.querySelector("[data-cal-assignee]");
  var clientRef = null, conflictTimer = null, custTimer = null;

  function newRef() { return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random(); }

  function openForm(e, preset) {
    hidePop();
    state.editing = e || null;
    clientRef = e ? null : newRef();
    f.reset(); errEl.hidden = true; conflictEl.hidden = true; custList.hidden = true;
    assigneeSel.textContent = "";
    state.members.forEach(function (m) { var o = el("option", null, m.name); o.value = m.id; assigneeSel.appendChild(o); });
    document.querySelector("[data-cal-form-title]").textContent = e ? "Termin bearbeiten" : "Neuer Termin";
    var v = e ? payloadOf(e) : { title: "", start: preset.start, end: preset.end, assignee_id: preset.assignee_id || cfg.me, location_kind: "", location: "", note: preset.note || "" };
    f.title.value = e ? v.title : (preset.title || "");
    f.start.value = v.start; f.end.value = v.end; f.assignee_id.value = String(v.assignee_id);
    f.location_kind.value = v.location_kind || ""; f.location.value = v.location || ""; f.note.value = v.note || "";
    f.customer_id.value = e && e.customer ? e.customer.id : (preset && preset.customer_id) || "";
    custInput.value = e && e.customer ? e.customer.name : (preset && preset.customer_name) || "";
    saveBtn.disabled = false;
    if (typeof els.dialog.showModal === "function") els.dialog.showModal(); else els.dialog.setAttribute("open", "");
    setTimeout(function () { (e ? f.start : f.title).focus(); }, 30);
    checkConflicts();
  }
  function closeForm() { els.dialog.close ? els.dialog.close() : els.dialog.removeAttribute("open"); state.editing = null; }
  Array.prototype.forEach.call(document.querySelectorAll("[data-cal-close]"), function (b) { b.addEventListener("click", closeForm); });
  els.dialog.addEventListener("cancel", function () { state.editing = null; });

  Array.prototype.forEach.call(f.querySelectorAll("[data-cal-dur]"), function (b) {
    b.addEventListener("click", function () { if (f.start.value) { f.end.value = addMinutes(f.start.value.slice(0, 16), +b.dataset.calDur); checkConflicts(); } });
  });
  f.start.addEventListener("change", function () {
    // Dauer beibehalten, wenn der Beginn verschoben wird.
    var prev = f.start.dataset.prev, s = f.start.value.slice(0, 16);
    if (prev && f.end.value) f.end.value = addMinutes(s, diffMinutes(prev, f.end.value.slice(0, 16)));
    else if (!f.end.value) f.end.value = addMinutes(s, 30);
    f.start.dataset.prev = s; checkConflicts();
  });
  f.start.addEventListener("focus", function () { f.start.dataset.prev = f.start.value.slice(0, 16); });
  f.end.addEventListener("change", checkConflicts);
  assigneeSel.addEventListener("change", checkConflicts);

  function checkConflicts() {
    clearTimeout(conflictTimer);
    conflictTimer = setTimeout(function () {
      if (!f.start.value || !f.end.value || !f.assignee_id.value) return;
      var q = "?start=" + f.start.value.slice(0, 16) + "&ende=" + f.end.value.slice(0, 16) + "&mitarbeiter=" + f.assignee_id.value + (state.editing ? "&ohne=" + state.editing.id : "");
      request("GET", "/konflikte" + q).then(function (d) {
        conflictEl.hidden = !d.conflicts.length;
        conflictEl.textContent = d.conflicts.length ? "Hinweis: " + (member(+f.assignee_id.value) || {}).name + " hat zu dieser Zeit bereits " + d.conflicts.map(function (c) { return "„" + c.title + "“ (" + hm(c.start) + "–" + hm(c.end) + ")"; }).join(", ") + ". Speichern ist trotzdem möglich." : "";
      }, function () {});
    }, 250);
  }

  custInput.addEventListener("input", function () {
    f.customer_id.value = "";
    clearTimeout(custTimer);
    var q = custInput.value.trim();
    if (q.length < 2) { custList.hidden = true; custInput.setAttribute("aria-expanded", "false"); return; }
    custTimer = setTimeout(function () {
      request("GET", "/kunden?q=" + encodeURIComponent(q)).then(function (d) {
        custList.textContent = "";
        d.customers.forEach(function (c, i) {
          var li = el("li"); li.setAttribute("role", "option"); li.id = "cal-cust-" + i; li.tabIndex = -1;
          li.appendChild(el("strong", null, c.name)); if (c.detail) li.appendChild(el("span", null, c.detail));
          li.addEventListener("mousedown", function (ev) { ev.preventDefault(); pick(c); });
          li.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); pick(c); } });
          custList.appendChild(li);
        });
        if (!d.customers.length) custList.appendChild(el("li", "cal-suggest-empty", "Kein Kunde gefunden."));
        custList.hidden = false; custInput.setAttribute("aria-expanded", "true");
      }, function () {});
    }, 250);
  });
  custInput.addEventListener("keydown", function (ev) { if (ev.key === "ArrowDown" && !custList.hidden) { var first = custList.querySelector("[role=option]"); if (first) { ev.preventDefault(); first.focus(); } } });
  custList.addEventListener("keydown", function (ev) {
    var items = Array.prototype.slice.call(custList.querySelectorAll("[role=option]")), i = items.indexOf(document.activeElement);
    if (ev.key === "ArrowDown" && i < items.length - 1) { ev.preventDefault(); items[i + 1].focus(); }
    if (ev.key === "ArrowUp") { ev.preventDefault(); (i > 0 ? items[i - 1] : custInput).focus(); }
    if (ev.key === "Escape") { custList.hidden = true; custInput.focus(); }
  });
  function pick(c) { f.customer_id.value = c.id; custInput.value = c.name; custList.hidden = true; custInput.setAttribute("aria-expanded", "false"); custInput.focus(); }

  f.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (saveBtn.disabled) return;
    if (!f.title.value.trim()) { showErr("Bitte einen Titel angeben."); f.title.focus(); return; }
    if (custInput.value.trim() && !f.customer_id.value) { showErr("Bitte den Kunden aus der Liste auswählen oder das Feld leeren."); custInput.focus(); return; }
    var body = {
      title: f.title.value, start: f.start.value.slice(0, 16), end: f.end.value.slice(0, 16), assignee_id: +f.assignee_id.value,
      customer_id: f.customer_id.value ? +f.customer_id.value : null, location_kind: f.location_kind.value,
      location: f.location.value, note: f.note.value,
    };
    var editing = state.editing, req;
    if (editing) { body.version = editing.version; req = request("PATCH", "/termine/" + editing.id, body); }
    else { body.client_ref = clientRef; req = request("POST", "/termine", body); }
    saveBtn.disabled = true; saveBtn.textContent = "Wird gespeichert …"; errEl.hidden = true;
    req.then(function (d) {
      replaceEvent(d.event); closeForm();
      state.anchor = dayOf(d.event.start);
      load().then(function () {
        status((editing ? "Gespeichert: " : "Termin angelegt: ") + d.event.title + ", " + dayLabel(dayOf(d.event.start)) + " " + hm(d.event.start) + " Uhr" + conflictText(d.conflicts), d.conflicts && d.conflicts.length ? "warn" : "ok");
      });
    }, function (err) {
      showErr(err.message);
      if (err.status === 409 && err.data && err.data.event) { state.editing = err.data.event; replaceEvent(err.data.event); }
    }).finally(function () { saveBtn.disabled = false; saveBtn.textContent = "Speichern"; });
  });
  function showErr(msg) { errEl.textContent = msg; errEl.hidden = false; }

  // --- Toolbar -------------------------------------------------------------------------------
  function saveView() { localStorage.setItem("zentriq-cal-view", state.view); }
  root.querySelector("[data-cal-prev]").onclick = function () { step(-1); };
  root.querySelector("[data-cal-next]").onclick = function () { step(1); };
  root.querySelector("[data-cal-today]").onclick = function () { state.anchor = cfg.today; load(); };
  root.querySelector("[data-cal-new]").onclick = function () {
    var now = berlinNow(), day = state.view === "day" ? state.anchor : (state.anchor === cfg.today ? cfg.today : state.anchor);
    var minutes = day === dayOf(now) ? Math.ceil((minutesOf(now) + 1) / 30) * 30 : 9 * 60;
    openForm(null, { start: isoAt(day, minutes), end: isoAt(day, minutes + 30) });
  };
  Array.prototype.forEach.call(root.querySelectorAll("[data-cal-view]"), function (b) {
    b.onclick = function () { state.view = b.dataset.calView; saveView(); load(); };
  });
  function toggleSide(open) {
    var willOpen = open === undefined ? !root.classList.contains("side-open") : open;
    root.classList.toggle("side-open", willOpen);
    els.sideToggle.setAttribute("aria-expanded", String(willOpen));
  }
  els.sideToggle.onclick = function () { toggleSide(); };
  document.addEventListener("keydown", function (ev) {
    if (ev.target.closest("input, textarea, select, dialog") || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    if (ev.key === "t") { state.anchor = cfg.today; load(); }
  });
  // Jetzt-Linie minuetlich nachziehen
  setInterval(function () { if (!state.dragging && !els.dialog.open && state.view !== "month" && state.view !== "list") render(); }, 60000);

  load().then(function () {
    var pf = cfg.prefill || {};
    if (pf.start) {
      openForm(null, {
        title: pf.titel, start: pf.start, end: pf.ende || addMinutes(pf.start, 30), assignee_id: pf.mitarbeiter ? +pf.mitarbeiter : cfg.me,
        customer_id: pf.kunde ? +pf.kunde : null, customer_name: pf.kunde_name, note: pf.notiz,
      });
    }
  });
})();
