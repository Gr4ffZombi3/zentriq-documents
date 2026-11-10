// Live-Eingabe im Hauptchat: Vorschau fuer Kundensuche, Notiz und Termin schon beim Tippen
// (app/services/live_input.py, GET /zentriq/api/vorschau - liest nur).
//
// - Lokale Vorpruefung (0 Modellaufrufe): nur klare Eingaben fragen den Server, erst nach einer
//   kurzen Tipp-Pause. Antworten auf aeltere Eingaben werden verworfen (Sequenznummer + Text).
// - Jedes erkannte Feld ist anklickbar (Auswahl bleibt bis zur naechsten Texteingabe erhalten).
// - Enter speichert nur, wenn der Auftrag eindeutig, vollstaendig und die Aktion sichtbar ist;
//   sonst geht die Eingabe wie bisher ab (js/zentriq-ai.js). Gespeichert wird ueber die
//   bestehenden Wege, die Rechte und Verfuegbarkeit serverseitig erneut pruefen.
// Alle Texte per textContent.
(function () {
  "use strict";
  var chat = document.querySelector("[data-zc]");
  var input = document.querySelector("[data-zc-input]");
  var form = document.querySelector("[data-zc-form]");
  if (!chat || !input || !form || !document.querySelector("[data-zn]")) return;
  var api = chat.dataset.api;
  var DEBOUNCE = 220;

  var NOTE = /^\s*notiz\s+[^:]{2,80}:/i;
  var TERM = /^\s*(?:bitte\s+)?(?:(?:einen?|neuen?)\s+)?termin\b(?!e)/i;
  var NAME = /^[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß'-]+(?:\s+[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß'-]+){0,2}$/;
  var SHORT_BOOKING = /\btermin\b(?!e)/i;
  function locallyRelevant(text) {
    var t = text.trim();
    if (!t || t.length > 300 || /\?\s*$/.test(t)) return false;
    return NOTE.test(t) || TERM.test(t) || (SHORT_BOOKING.test(t) && /\d/.test(t)) || (NAME.test(t) && t.length >= 3);
  }

  var box = document.createElement("div");
  box.className = "zl";
  box.setAttribute("data-zl", "");
  box.setAttribute("role", "region");
  box.setAttribute("aria-label", "Vorschau");
  box.setAttribute("aria-live", "polite");
  box.hidden = true;
  // Schwebend am Eingabefeld (verschiebt das Layout nicht - die Eingabe springt nicht): unter dem
  // Eingabebereich, wenn dort Platz ist, sonst darueber (z. B. Handy mit offener Tastatur).
  var anchor = input.closest(".zc-composer-inner") || input.parentNode;
  anchor.classList.add("zl-anchor");
  anchor.appendChild(box);
  function place() {
    if (box.hidden) return;
    var r = anchor.getBoundingClientRect();
    var vh = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    var below = vh - r.bottom - 12, above = r.top - 12;
    var need = Math.min(box.scrollHeight, 360);
    var down = below >= need || below >= above;
    box.classList.toggle("is-below", down);
    box.classList.toggle("is-above", !down);
    box.style.maxHeight = Math.max(140, Math.min(down ? below : above, 380)) + "px";
  }
  window.addEventListener("resize", place);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", place);

  var seq = 0, timer = null, data = null, overrides = {}, selected = -1, openField = null, slots = null, saving = false, lastText = "";

  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function btn(cls, text, fn) { var b = el("button", cls, text); b.type = "button"; b.addEventListener("mousedown", function (e) { e.preventDefault(); }); b.addEventListener("click", fn); return b; }

  function hide() {
    data = null; selected = -1; openField = null; slots = null;
    if (box.hidden) return;
    box.classList.remove("is-in");
    box.hidden = true;
    box.textContent = "";
  }

  function query(withSlots) {
    var text = input.value;
    var mine = ++seq;
    var params = new URLSearchParams({ text: text.trim(), seq: String(mine) });
    Object.keys(overrides).forEach(function (k) { if (overrides[k] !== undefined && overrides[k] !== null) params.set(k, overrides[k] === true ? "1" : String(overrides[k])); });
    if (withSlots) params.set("slots", "1");
    return fetch(api + "/vorschau?" + params.toString(), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : { kind: null }; })
      .then(function (d) {
        // Aeltere Antwort oder inzwischen geaenderter Text: verwerfen.
        if (mine !== seq || input.value !== text) return null;
        if (withSlots) slots = d.slots || [];
        d._text = text;
        render(d);
        return d;
      }, function () { return null; });
  }

  function schedule() {
    clearTimeout(timer);
    var text = input.value;
    if (text === lastText) return;
    lastText = text;
    overrides = {}; // neue Eingabe: Auswahl aus der Vorschau gilt nicht mehr
    if (!locallyRelevant(text)) { seq++; hide(); return; }
    timer = setTimeout(function () { query(false); }, DEBOUNCE);
  }

  function show() {
    if (!box.hidden) { place(); return; }
    box.hidden = false;
    place();
    // Sanft einblenden (CSS, ohne Animation bei reduzierter Bewegung); die Eingabe behaelt den Fokus.
    requestAnimationFrame(function () { box.classList.add("is-in"); });
  }

  // --- Darstellung -------------------------------------------------------------------------
  function render(d) {
    if (!d || !d.kind) { hide(); return; }
    var keepField = data && data.kind === d.kind ? openField : null;
    data = d;
    openField = keepField;
    box.textContent = "";
    if (d.kind === "customer") renderCustomers(d);
    else if (d.kind === "note") renderNote(d);
    else if (d.kind === "appointment") renderAppointment(d);
    show();
  }

  function renderCustomers(d) {
    var list = el("ul", "zl-list");
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Passende Kunden");
    if (selected >= d.customers.length) selected = -1;
    d.customers.forEach(function (c, i) {
      var li = el("li", "zl-option" + (i === selected ? " is-selected" : ""));
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === selected ? "true" : "false");
      li.appendChild(el("strong", null, c.name));
      if (c.detail) li.appendChild(el("span", "zl-muted", c.detail));
      li.addEventListener("mousedown", function (e) { e.preventDefault(); openCustomer(c); });
      list.appendChild(li);
    });
    box.appendChild(list);
    box.appendChild(el("p", "zl-hint", "↑ ↓ auswählen · Enter öffnet die Kundenkarte" + (d.more ? " · weitere Treffer – genauer tippen" : "")));
  }

  function openCustomer(c) {
    if (!window.ZentriqNotes || !window.ZentriqNotes.openCustomer) return;
    window.ZentriqChat.clearInput();
    lastText = "";
    hide();
    window.ZentriqNotes.openCustomer(c.id, null);
  }

  function chip(label, value, state, field) {
    var b = btn("zl-chip is-" + state + (openField === field ? " is-open" : ""), "", function () { toggleField(field); });
    b.setAttribute("aria-expanded", openField === field ? "true" : "false");
    b.setAttribute("aria-label", label + ": " + value + (state === "ok" ? "" : " – bitte wählen"));
    b.appendChild(el("span", "zl-chip-label", label));
    b.appendChild(el("strong", null, value));
    return b;
  }

  function toggleField(field) {
    openField = openField === field ? null : field;
    if (openField === "time" && data && data.kind === "appointment") { query(true); return; }
    render(data);
  }

  function pick(values) {
    Object.keys(values).forEach(function (k) { overrides[k] = values[k]; });
    if ("customer_id" in values) delete overrides.no_customer;
    if (values.no_customer) delete overrides.customer_id;
    openField = null;
    query(false);
  }

  function renderNote(d) {
    var row = el("div", "zl-row");
    var c = d.customer;
    row.appendChild(chip("Kunde", c.value ? c.value.name : (c.status === "ambiguous" ? "Mehrere „" + d.name + "“" : "„" + d.name + "“ nicht gefunden"), c.status === "ok" ? "ok" : "missing", "customer"));
    box.appendChild(row);
    if (openField === "customer" || c.status !== "ok") box.appendChild(customerOptions(c.options, false));
    var area = el("textarea", "zl-note field-surface");
    area.rows = 2;
    area.maxLength = 5000;
    area.value = d.text.value;
    area.setAttribute("aria-label", "Notiztext (bearbeitbar)");
    area.setAttribute("data-zl-note", "");
    if (d.text.status !== "ok") { area.classList.add("is-missing"); area.placeholder = "Notiztext fehlt"; }
    box.appendChild(area);
    var actions = el("div", "zl-actions");
    var save = btn("btn btn-primary btn-sm", "Notiz speichern", saveNote);
    save.disabled = !d.ready;
    save.setAttribute("data-zl-save", "");
    actions.appendChild(save);
    actions.appendChild(el("span", "zl-hint", d.ready ? "Enter speichert" : "Kunde und Text prüfen"));
    box.appendChild(actions);
  }

  function customerOptions(options, allowNone) {
    var wrap = el("div", "zl-pop");
    if (!options.length) wrap.appendChild(el("p", "zl-muted", "Kein passender Kunde. Namen im Satz ändern."));
    options.forEach(function (o) {
      var b = btn("zl-pick", "", function () { pick({ customer_id: o.id }); });
      b.appendChild(el("strong", null, o.name));
      if (o.detail) b.appendChild(el("span", "zl-muted", o.detail));
      wrap.appendChild(b);
    });
    if (allowNone) wrap.appendChild(btn("zl-pick zl-pick-quiet", "Ohne Kunde", function () { pick({ no_customer: true }); }));
    return wrap;
  }

  function renderAppointment(d) {
    var f = d.fields;
    var row = el("div", "zl-row");
    var c = f.customer;
    var customerText = c.value ? c.value.name : c.status === "skipped" ? "ohne Kunde" : c.status === "ambiguous" ? "Mehrere „" + c.query + "“" : c.query ? "„" + c.query + "“ unbekannt" : "Kunde fehlt";
    row.appendChild(chip("Kunde", customerText, c.status === "ok" || c.status === "skipped" ? "ok" : "missing", "customer"));
    row.appendChild(chip("Datum", f.day.label || "fehlt", f.day.status === "ok" ? "ok" : "missing", "day"));
    row.appendChild(chip("Uhrzeit", f.time.value ? f.time.value + " Uhr" : "fehlt", f.time.status === "ok" ? "ok" : "missing", "time"));
    row.appendChild(chip("Mitarbeiter", f.assignee.value ? f.assignee.value.name : "wählen", f.assignee.status === "ok" ? "ok" : "missing", "assignee"));
    row.appendChild(chip("Dauer", f.minutes.value + " Min.", f.minutes.status === "ok" ? "ok" : "missing", "minutes"));
    var av = d.availability;
    if (av) {
      var badge = el("span", "zl-status is-" + av.status, av.status === "free" ? "Frei" : "Belegt");
      row.appendChild(badge);
    }
    box.appendChild(row);

    if (openField === "customer" || (!openField && c.status === "ambiguous")) box.appendChild(customerOptions(c.options, true));
    else if (openField === "day") {
      var pop = el("div", "zl-pop");
      var dayInput = el("input", "field-surface zl-date");
      dayInput.type = "date";
      dayInput.value = f.day.value || "";
      dayInput.setAttribute("aria-label", "Datum wählen");
      dayInput.addEventListener("change", function () { if (dayInput.value) pick({ start: dayInput.value + "T" + (f.time.value || "09:00") }); });
      pop.appendChild(dayInput);
      box.appendChild(pop);
    } else if (openField === "time") {
      var tp = el("div", "zl-pop");
      if (!f.day.value) tp.appendChild(el("p", "zl-muted", "Zuerst ein Datum wählen."));
      else if (slots === null) tp.appendChild(el("p", "zl-muted", "Freie Zeiten werden geladen …"));
      else if (!slots.length) tp.appendChild(el("p", "zl-muted", "An diesem Tag ist bei " + (f.assignee.value ? f.assignee.value.name : "der Person") + " nichts mehr frei."));
      else {
        tp.appendChild(el("p", "zl-muted", "Frei bei " + (f.assignee.value ? f.assignee.value.name : "") + ":"));
        var grid = el("div", "zl-slots");
        slots.forEach(function (s) {
          var b = btn("zl-slot" + (d.start === s.start ? " is-current" : ""), s.label, function () { pick({ start: s.start }); });
          grid.appendChild(b);
        });
        tp.appendChild(grid);
      }
      box.appendChild(tp);
    } else if (openField === "assignee" || (!openField && f.assignee.status !== "ok")) {
      var ap = el("div", "zl-pop");
      f.assignee.options.forEach(function (m) { ap.appendChild(btn("zl-pick" + (f.assignee.value && f.assignee.value.id === m.id ? " is-current" : ""), m.name, function () { pick({ assignee_id: m.id }); })); });
      box.appendChild(ap);
    } else if (openField === "minutes") {
      var mp = el("div", "zl-pop zl-slots");
      [15, 30, 45, 60, 90].forEach(function (m) { mp.appendChild(btn("zl-slot" + (f.minutes.value === m ? " is-current" : ""), m + " Min.", function () { pick({ minutes: m }); })); });
      box.appendChild(mp);
    }

    if (av && av.status === "busy") {
      var warn = el("div", "zl-busy");
      warn.appendChild(el("p", null, av.reason));
      if (av.alternatives.length) {
        var alt = el("div", "zl-slots");
        av.alternatives.forEach(function (a) { alt.appendChild(btn("zl-slot", a.label.replace(/^\w+, /, ""), function () { pick({ start: a.start }); })); });
        warn.appendChild(el("p", "zl-muted", "Frei wäre:"));
        warn.appendChild(alt);
      }
      box.appendChild(warn);
    }
    if (d.error) box.appendChild(el("p", "zl-error", d.error));

    var actions = el("div", "zl-actions");
    var save = btn("btn btn-primary btn-sm", "Termin speichern", saveAppointment);
    save.disabled = !d.ready;
    save.setAttribute("data-zl-save", "");
    actions.appendChild(save);
    var who = d.fields.assignee.value ? " bei " + d.fields.assignee.value.name.split(" ")[0] : "";
    var why = d.ready ? "Frei" + who + " · Enter speichert" : missingText(d);
    actions.appendChild(el("span", "zl-hint", why));
    box.appendChild(actions);
  }

  function missingText(d) {
    var f = d.fields, out = [];
    if (!(f.customer.status === "ok" || f.customer.status === "skipped")) out.push("Kunde");
    if (f.day.status !== "ok") out.push("Datum");
    if (f.time.status !== "ok") out.push("Uhrzeit");
    if (f.assignee.status !== "ok") out.push("Mitarbeiter");
    if (out.length) return out.join(", ") + " fehlt";
    if (d.availability && d.availability.status === "busy") return "Zeit ist belegt – Alternative wählen";
    return "Bitte prüfen";
  }

  // --- Speichern ---------------------------------------------------------------------------
  function busyNow() { return saving || (window.ZentriqChat && window.ZentriqChat.isBusy && window.ZentriqChat.isBusy()); }

  function finish(text, r) {
    var reply = window.ZentriqChat.addTurn(text);
    window.ZentriqNotes.show(reply, r, text);
    window.ZentriqChat.clearInput();
    lastText = "";
    hide();
    input.focus();
  }

  function saveAppointment() {
    if (!data || !data.ready || busyNow()) return;
    var text = input.value.trim(), shown = data;
    saving = true;
    box.classList.add("is-saving");
    var choice = Object.assign({}, shown.choice, { book: true });
    window.ZentriqNotes.post({ text: text, calendar: choice }).then(function (r) {
      saving = false;
      box.classList.remove("is-saving");
      if (r.kind === "calendar_saved") { finish(text, r); return; }
      // Inzwischen belegt o. Ae.: Grund zeigen, Eingaben behalten, Vorschau mit Alternativen neu laden.
      query(false).then(function (d) {
        if (!d || !data) return;
        var nowBusy = data.availability && data.availability.status === "busy";
        data.error = nowBusy ? "Nicht gebucht – die Zeit wurde inzwischen belegt. Bitte eine freie Zeit wählen." : r.message;
        render(data);
      });
    }, function (err) {
      saving = false;
      box.classList.remove("is-saving");
      if (data) { data.error = err.message + " Die Eingabe bleibt erhalten."; render(data); }
    });
  }

  function saveNote() {
    if (!data || data.kind !== "note" || busyNow()) return;
    var area = box.querySelector("[data-zl-note]");
    var noteText = (area ? area.value : data.text.value).trim();
    if (!noteText || !data.customer.value) return;
    var text = input.value.trim();
    saving = true;
    box.classList.add("is-saving");
    window.ZentriqNotes.post({ text: noteText, customer_id: data.customer.value.id, mode: "note" }).then(function (r) {
      saving = false;
      box.classList.remove("is-saving");
      finish(text, r);
    }, function (err) {
      saving = false;
      box.classList.remove("is-saving");
      if (data) { data.error = err.message; var p = el("p", "zl-error", err.message + " Die Eingabe bleibt erhalten."); box.appendChild(p); }
    });
  }

  // --- Tastatur ----------------------------------------------------------------------------
  input.addEventListener("input", schedule);
  input.addEventListener("keydown", function (e) {
    if (box.hidden || !data) return;
    if (data.kind === "customer" && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      var n = data.customers.length;
      selected = e.key === "ArrowDown" ? (selected + 1) % n : (selected <= 0 ? n - 1 : selected - 1);
      render(data);
    } else if (e.key === "Escape") {
      if (openField) { openField = null; render(data); } else hide();
      e.stopPropagation();
    }
  });
  // Tippen im Notizfeld der Vorschau: Enter ohne Umschalt speichert (wie im Eingabefeld).
  box.addEventListener("keydown", function (e) {
    if (e.target.matches("[data-zl-note]") && e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); saveNote(); }
  });

  window.ZentriqLive = {
    // Aufruf aus js/zentriq-ai.js bei Enter: true = von der Vorschau erledigt.
    handleEnter: function () {
      // Nur eine Vorschau, die genau zum aktuellen Text gehoert (keine halbfertige Eingabe).
      if (box.hidden || !data || data._text !== input.value || saving) {
        if (saving) return true;
        seq++; hide(); lastText = "";
        return false;
      }
      if (data.kind === "customer" && selected >= 0 && data.customers[selected]) { openCustomer(data.customers[selected]); return true; }
      if (data.kind === "note" && data.ready) { saveNote(); return true; }
      if (data.kind === "appointment" && data.ready) { saveAppointment(); return true; }
      seq++; hide(); lastText = "";
      return false;
    },
  };
})();
