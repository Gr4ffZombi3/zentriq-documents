/* Live-Zusammenarbeit in Kundenakten (Dienst zentriq-live, siehe live_service/server.py).
 *
 * Jedes Element mit data-zq-live="<Token-URL>" ist eine geoeffnete Kundenakte: ganze Seite,
 * Seitenfenster (wird per innerHTML eingesetzt - deshalb MutationObserver) oder die Kundenkarte
 * im Assistenten. Je Akte eine SSE-Verbindung:
 *   presence -> "Auch geöffnet: …"     editing -> "… bearbeitet gerade …"
 *   changed  -> "… hat die Akte gerade geändert" + Aktualisieren
 * Ohne Live-Dienst (noch nicht ausgerollt, Neustart) bleibt alles unsichtbar: nach wenigen
 * Fehlversuchen gibt die Akte auf, Speichern funktioniert davon unabhaengig wie bisher.
 * Eigene Aenderungen loesen keinen Hinweis aus. */
(function () {
  "use strict";
  if (!window.EventSource || !window.fetch) return;

  var MAX_ACTIVE = 3; // Assistenten-Verlauf kann viele Karten enthalten: nur die neuesten live
  var EDIT_TTL = 90000; // "bearbeitet gerade" verfaellt ohne Auffrischung (Tab geschlossen o. ae.)
  var EDIT_REFRESH = 45000;
  var roots = [];

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function names(list) {
    if (list.length <= 2) return list.join(" und ");
    return list.slice(0, 2).join(", ") + " und " + (list.length - 2) + " weitere";
  }

  function scopeOf(root) {
    // Bereich der Akte: Karte selbst, Seitenfenster-Inhalt oder Seiteninhalt.
    return root.hasAttribute("data-zq-live-scope") ? root : (root.closest("[data-zq-panel-body]") || root.closest(".page-shell") || document.body);
  }
  function barOf(root) { return root.querySelector("[data-zq-live-bar]") || root; }

  function start(root) {
    if (root._zqLive) return;
    var state = root._zqLive = {
      root: root, es: null, me: null, viewers: [], editing: {}, changed: null,
      failures: 0, timer: null, stopped: false, editingOn: null, editTimer: null,
    };
    roots.push(state);
    connect(state);
  }

  function stop(state) {
    state.stopped = true;
    if (state.es) state.es.close();
    clearTimeout(state.timer);
    clearInterval(state.editTimer);
    if (state.editingOn) sendEditing(state, false, state.editingOn, true);
    roots = roots.filter(function (s) { return s !== state; });
    state.root._zqLive = null;
  }

  function retry(state, delay) {
    if (state.stopped) return;
    clearTimeout(state.timer);
    state.timer = setTimeout(function () { connect(state); }, delay);
  }

  function connect(state) {
    if (state.stopped) return;
    var root = state.root;
    fetch(root.getAttribute("data-zq-live"), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok || (r.headers.get("Content-Type") || "").indexOf("json") < 0) throw new Error("token");
        return r.json();
      })
      .then(function (d) {
        if (state.stopped) return;
        if (!d.enabled) { stop(state); return; }
        state.me = d.me;
        var es = state.es = new EventSource(d.stream + "?token=" + encodeURIComponent(d.token));
        var opened = false;
        es.addEventListener("hello", function () { opened = true; state.failures = 0; });
        es.addEventListener("presence", function (ev) { state.viewers = parse(ev).viewers || []; render(state); });
        es.addEventListener("editing", function (ev) { onEditing(state, parse(ev)); });
        es.addEventListener("changed", function (ev) { onChanged(state, parse(ev)); });
        es.addEventListener("renew", function () { es.close(); retry(state, 100); });
        es.onerror = function () {
          es.close();
          if (state.stopped) return;
          // Nie verbunden (Dienst fehlt) -> nach drei Versuchen aufgeben; abgerissen -> neu verbinden.
          state.failures = opened ? 1 : state.failures + 1;
          if (state.failures >= 3) { state.viewers = []; state.editing = {}; render(state); return; }
          retry(state, opened ? 3000 : 5000 * state.failures);
        };
      })
      .catch(function () {
        state.failures += 1;
        if (state.failures < 3) retry(state, 10000 * state.failures);
      });
  }

  function parse(ev) { try { return JSON.parse(ev.data) || {}; } catch (e) { return {}; } }

  function onEditing(state, d) {
    if (!d.by || d.by.u === state.me) return;
    if (d.on) state.editing[d.by.u] = { n: d.by.n, part: d.part || "Stammdaten", until: Date.now() + EDIT_TTL };
    else delete state.editing[d.by.u];
    render(state);
  }

  function onChanged(state, d) {
    if (d.by && d.by.u === state.me) return; // eigene Aenderung (auch aus anderem Tab)
    var kinds = d.kinds || [];
    state.changed = { who: d.by ? d.by.n : null, kinds: kinds };
    if (d.by) delete state.editing[d.by.u]; // gespeichert -> bearbeitet nicht mehr
    render(state);
    // Offene Feldbearbeitung: vor dem Speichern warnen statt erst beim Konflikt.
    if (kinds.indexOf("Stammdaten") >= 0) {
      Array.prototype.forEach.call(scopeOf(state.root).querySelectorAll(".zq-field-form, .zn-edit"), function (form) {
        if (form.querySelector(".zq-live-formwarn")) return;
        form.appendChild(el("p", "zq-live-formwarn", (d.by ? d.by.n + " hat" : "Jemand hat") + " die Kundendaten gerade geändert. Bitte erst aktualisieren, sonst wird Ihre Eingabe abgelehnt."));
      });
    }
  }

  function refresh(state) {
    var root = state.root;
    if (root.hasAttribute("data-zq-live-scope")) {
      root.dispatchEvent(new CustomEvent("zq:live-refresh", { bubbles: true }));
    } else if (root.closest("[data-zq-panel-body]")) {
      document.dispatchEvent(new CustomEvent("zq:panel-reload"));
    } else {
      window.location.reload();
    }
  }

  function render(state) {
    var bar = barOf(state.root);
    var now = Date.now();
    bar.textContent = "";
    var others = state.viewers.filter(function (v) { return v.u !== state.me; }).map(function (v) { return v.n; });
    var editors = Object.keys(state.editing).map(function (u) { return state.editing[u]; }).filter(function (e) { return e.until > now; });
    if (others.length) {
      var who = el("p", "zq-live-who");
      who.appendChild(el("span", "zq-live-dot"));
      who.appendChild(document.createTextNode("Auch geöffnet: " + names(others)));
      bar.appendChild(who);
    }
    editors.forEach(function (e) {
      bar.appendChild(el("p", "zq-live-editing", e.n + " bearbeitet gerade " + (e.part === "Stammdaten" ? "die Kundendaten" : e.part) + "."));
    });
    if (state.changed) {
      var box = el("div", "zq-live-changed");
      var kinds = state.changed.kinds.length ? " (" + state.changed.kinds.join(", ") + ")" : "";
      box.appendChild(el("span", null, state.changed.who
        ? state.changed.who + " hat diese Akte gerade geändert" + kinds + "."
        : "Diese Akte wurde gerade aktualisiert" + kinds + "."));
      var actions = el("span", "zq-live-actions");
      var go = el("button", "zq-live-refresh", "Aktualisieren");
      go.type = "button";
      go.addEventListener("click", function () { refresh(state); });
      var later = el("button", "zq-live-dismiss", "Später");
      later.type = "button";
      later.addEventListener("click", function () { state.changed = null; render(state); });
      // Im offenen Bearbeitungsformular der Assistenten-Karte nicht neu zeichnen (Eingaben blieben weg).
      if (!state.root.classList.contains("zn-edit")) actions.appendChild(go);
      actions.appendChild(later);
      box.appendChild(actions);
      bar.appendChild(box);
    }
    bar.hidden = !bar.childNodes.length;
  }

  // "Bearbeitet gerade": workspace.js / customer-notes.js melden Start und Ende der Bearbeitung.
  function sendEditing(state, on, part, beacon) {
    var url = state.root.getAttribute("data-zq-live-editing");
    if (!url) return;
    fetch(url, {
      method: "POST", credentials: "same-origin", keepalive: !!beacon,
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify({ on: on, part: part }),
    }).catch(function () { /* nur Komfort */ });
  }

  document.addEventListener("zq:editing", function (ev) {
    var target = ev.target;
    var state = null;
    roots.forEach(function (s) { if (!state && scopeOf(s.root).contains(target)) state = s; });
    if (!state || state.failures >= 3) return;
    var part = (ev.detail && ev.detail.part) || "Stammdaten";
    clearInterval(state.editTimer);
    if (ev.detail && ev.detail.on) {
      state.editingOn = part;
      sendEditing(state, true, part);
      state.editTimer = setInterval(function () { if (state.editingOn) sendEditing(state, true, state.editingOn); }, EDIT_REFRESH);
    } else if (state.editingOn) {
      state.editingOn = null;
      sendEditing(state, false, part);
    }
  });

  // Abgelaufene "bearbeitet gerade"-Hinweise ausblenden.
  setInterval(function () {
    roots.forEach(function (s) { if (Object.keys(s.editing).length) render(s); });
  }, 15000);

  function scan() {
    roots.slice().forEach(function (s) { if (!document.contains(s.root) || !s.root.hasAttribute("data-zq-live")) stop(s); });
    var found = Array.prototype.slice.call(document.querySelectorAll("[data-zq-live]"));
    found.slice(-MAX_ACTIVE).forEach(start);
    found.slice(0, -MAX_ACTIVE).forEach(function (r) { if (r._zqLive) stop(r._zqLive); });
  }

  var pending = false;
  new MutationObserver(function () {
    if (pending) return;
    pending = true;
    setTimeout(function () { pending = false; scan(); }, 50);
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-zq-live"] });
  window.addEventListener("pagehide", function () { roots.slice().forEach(stop); });
  window.addEventListener("pageshow", function (ev) { if (ev.persisted) scan(); });
  scan();
})();
