/* Interner 1:1-Teamchat (components/teamchat.html, API /arbeitsplatz/teamchat).
 *
 * - Minimierte Chats: schmale Pillen links neben "Ausgestempelt" (Zeiterfassung, js/shell.js); auf
 *   schmalen Bildschirmen in einer Zeile darueber. Die Position wird an der Zeiterfassung ausgerichtet
 *   (fester Abstand, neu berechnet bei Groesse/Zoom/Statuswechsel) - beide ueberlappen sich nie.
 * - Klick auf eine Pille klappt das Fenster nach oben auf (Kopf: Name, Vergroessern, Minimieren,
 *   Schliessen). Der Rest der Seite bleibt bedienbar.
 * - Zustand (offen/minimiert/vergroessert) je Benutzer im Browser - bleibt beim Seitenwechsel.
 * - Live: persoenlicher Kanal ueber zentriq-live ({"type":"chat","chat":id}); Texte holt der Browser
 *   angemeldet. Ohne Live-Dienst: regelmaessig abfragen. */
(function () {
  "use strict";
  var root = document.querySelector("[data-zq-tc]");
  if (!root || !window.fetch) return;

  var base = root.getAttribute("data-base");
  var STORE = "zq-teamchat-" + root.getAttribute("data-user");
  var GAP = 8; // fester Abstand zur Zeiterfassung (px)
  // Weitere schwebende Elemente unten rechts, ueber die sich die Chatleiste auf dem Handy stellt.
  var AVOID = ".cal-page .cal-new, .cal-page .cal-new + .zq-new, [data-zq-float]";
  var bar = root.querySelector("[data-zq-tc-bar]");
  var windowsEl = root.querySelector("[data-zq-tc-windows]");
  var newBtn = root.querySelector("[data-zq-tc-new]");
  var menu = root.querySelector("[data-zq-tc-menu]");
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');

  var colleagues = [];
  var chats = {}; // id -> {id, partner, unread, state, last, pill, win, list}
  var order = []; // Reihenfolge in der Leiste (zuletzt geoeffnet links)
  var es = null, failures = 0, pollTimer = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function api(method, path, body) {
    var headers = { Accept: "application/json", "X-Zentriq-Fetch": "1" };
    if (method !== "GET") { headers["Content-Type"] = "application/json"; headers["X-CSRFToken"] = csrfMeta ? csrfMeta.content : ""; }
    return fetch(base + path, { method: method, credentials: "same-origin", headers: headers, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) throw Object.assign(new Error(d.error || (r.status === 401 ? "Bitte neu anmelden." : "Technischer Fehler. Bitte erneut versuchen.")), { status: r.status });
          return d;
        });
      });
  }
  function newRef() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) { /* alt */ }
    return Date.now().toString(36) + Math.random().toString(36).slice(2);
  }
  function narrow() { return window.matchMedia("(max-width: 47.99rem)").matches; }

  // --- Zustand im Browser ------------------------------------------------------------------------
  function load() {
    try { return JSON.parse(window.localStorage.getItem(STORE) || "{}") || {}; } catch (e) { return {}; }
  }
  function save() {
    var data = { chats: order.map(function (id) { return { id: id, state: chats[id].state }; }) };
    try { window.localStorage.setItem(STORE, JSON.stringify(data)); } catch (e) { /* Speicher gesperrt */ }
  }

  // --- Position an der Zeiterfassung -----------------------------------------------------------------
  function place() {
    var clock = document.querySelector("[data-zs-clock]");
    var pill = clock && clock.querySelector(".zs-clock-pill");
    // offsetParent ist bei position:fixed immer null - sichtbar = hat eine Flaeche.
    var fixed = clock && getComputedStyle(clock).position === "fixed" && clock.getClientRects().length > 0;
    var vw = document.documentElement.clientWidth, vh = window.innerHeight;
    var right = 16, bottom = 16, maxW = vw - 16;
    if (fixed && pill && pill.offsetWidth) {
      var r = pill.getBoundingClientRect();
      if (narrow()) {
        // Zeile oberhalb der Zeiterfassung - und oberhalb weiterer schwebender Knoepfe unten rechts
        // (z. B. "Termin buchen" im Kalender), damit nichts verdeckt wird.
        var top = r.top;
        Array.prototype.forEach.call(document.querySelectorAll(AVOID), function (n) {
          if (getComputedStyle(n).position !== "fixed" || !n.getClientRects().length) return;
          var b = n.getBoundingClientRect();
          if (b.right > vw / 2 && b.bottom > vh / 2) top = Math.min(top, b.top);
        });
        right = Math.max(8, vw - r.right);
        bottom = Math.max(8, vh - top + GAP);
        maxW = vw - 16;
      } else {
        right = Math.max(8, vw - r.left + GAP); // links neben der Zeiterfassung
        bottom = Math.max(8, vh - r.bottom);
        maxW = Math.max(160, r.left - GAP - 8);
      }
    } else {
      // Assistent (Zeiterfassung in der Kopfzeile): ueber Eingabefeld und Leiste "Offene Rueckrufe".
      var zoneTop = vh;
      Array.prototype.forEach.call(document.querySelectorAll(".zs-chat-page .zc-composer, .zs-chat-page .zn-bar"), function (n) {
        if (n.getClientRects().length) zoneTop = Math.min(zoneTop, n.getBoundingClientRect().top);
      });
      if (zoneTop < vh) bottom = Math.max(bottom, vh - zoneTop + GAP);
    }
    // Zeiterfassung woanders (z. B. Handy-Kopfzeile im Assistenten): Fenster enden darunter.
    var topLimit = 8;
    if (!fixed && pill && pill.getClientRects().length) topLimit = Math.max(topLimit, pill.getBoundingClientRect().bottom + GAP);
    root.style.right = right + "px";
    root.style.bottom = bottom + "px";
    root.style.maxWidth = maxW + "px";
    root.style.setProperty("--zq-tc-max-w", Math.min(maxW, 34 * 16) + "px");
    root.style.setProperty("--zq-tc-max-h", Math.max(160, vh - bottom - bar.offsetHeight - GAP - Math.max(topLimit, 72)) + "px");
  }

  // --- Leiste und Fenster -------------------------------------------------------------------------
  function setCount(chat) {
    var c = chat.pill.querySelector(".zq-tc-count");
    c.textContent = chat.unread > 99 ? "99+" : String(chat.unread || "");
    c.hidden = !chat.unread || chat.state !== "min";
    chat.pill.setAttribute("aria-label", chat.partner.name + (chat.unread ? ", " + chat.unread + " ungelesen" : ""));
  }

  function ensure(info, state) {
    var chat = chats[info.id];
    if (!chat) {
      chat = chats[info.id] = { id: info.id, partner: info.partner, unread: info.unread || 0, state: state || "min", last: 0, loaded: false };
      chat.pill = el("button", "zq-tc-pill");
      chat.pill.type = "button";
      chat.pill.appendChild(el("span", "zq-tc-name", info.partner.name));
      chat.pill.appendChild(el("span", "zq-tc-count"));
      chat.pill.addEventListener("click", function () { setState(chat, chat.state === "min" ? "open" : "min"); });
      var picker = bar.querySelector(".zq-tc-picker");
      bar.insertBefore(chat.pill, picker.nextSibling); // neuester Chat direkt rechts neben "Teamchat"
      order.unshift(chat.id);
    } else {
      chat.partner = info.partner;
      chat.unread = info.unread || 0;
    }
    chat.pill.querySelector(".zq-tc-name").textContent = chat.partner.name;
    setCount(chat);
    return chat;
  }

  function buildWindow(chat) {
    var win = el("section", "zq-tc-win");
    win.setAttribute("role", "dialog");
    win.setAttribute("aria-label", "Teamchat mit " + chat.partner.name);
    var head = el("header", "zq-tc-head");
    head.appendChild(el("h2", "zq-tc-title", chat.partner.name));
    var max = el("button", null, "⤢"); max.type = "button"; max.title = "Vergrößern"; max.setAttribute("aria-label", "Vergrößern");
    max.addEventListener("click", function () { setState(chat, chat.state === "max" ? "open" : "max"); });
    var min = el("button", null, "–"); min.type = "button"; min.title = "Minimieren"; min.setAttribute("aria-label", "Minimieren");
    min.addEventListener("click", function () { setState(chat, "min"); });
    var close = el("button", null, "×"); close.type = "button"; close.title = "Schließen"; close.setAttribute("aria-label", "Chat schließen");
    close.addEventListener("click", function () { remove(chat); });
    head.appendChild(max); head.appendChild(min); head.appendChild(close);
    win.appendChild(head);
    chat.list = el("ol", "zq-tc-list");
    chat.list.setAttribute("aria-live", "polite");
    win.appendChild(chat.list);
    chat.error = el("p", "zq-tc-error"); chat.error.hidden = true; chat.error.setAttribute("role", "alert");
    win.appendChild(chat.error);
    var form = el("form", "zq-tc-form");
    var input = el("textarea"); input.rows = 1; input.maxLength = 2000; input.placeholder = "Nachricht …";
    input.setAttribute("aria-label", "Nachricht an " + chat.partner.name);
    var send = el("button", "btn btn-primary btn-sm", "Senden"); send.type = "submit";
    form.appendChild(input); form.appendChild(send);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : submit(); }
    });
    input.addEventListener("input", function () { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 112) + "px"; });
    form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
    function submit() {
      var text = input.value.trim();
      if (!text || send.disabled) return;
      send.disabled = true;
      chat.error.hidden = true;
      api("POST", "/" + chat.id + "/nachrichten", { text: text, client_ref: newRef() }).then(function (d) {
        input.value = ""; input.style.height = "auto";
        append(chat, [d.message]);
      }, function (err) {
        chat.error.textContent = err.message; chat.error.hidden = false; // Text bleibt im Feld
      }).then(function () { send.disabled = false; input.focus(); });
    }
    win.appendChild(form);
    chat.input = input;
    chat.win = win;
    return win;
  }

  function append(chat, items) {
    var empty = chat.list.querySelector(".zq-tc-empty");
    if (empty && items.length) empty.remove();
    var nearBottom = chat.list.scrollHeight - chat.list.scrollTop - chat.list.clientHeight < 60;
    items.forEach(function (m) {
      if (m.id <= chat.last && chat.list.querySelector('[data-id="' + m.id + '"]')) return;
      var li = el("li", "zq-tc-msg" + (m.mine ? " is-mine" : ""));
      li.setAttribute("data-id", String(m.id));
      li.appendChild(document.createTextNode(m.text));
      li.appendChild(el("time", null, m.when));
      chat.list.appendChild(li);
      chat.last = Math.max(chat.last, m.id);
    });
    if (nearBottom || items.some(function (m) { return m.mine; })) chat.list.scrollTop = chat.list.scrollHeight;
    markRead(chat);
  }

  function loadMessages(chat) {
    var path = "/" + chat.id + "/nachrichten" + (chat.loaded ? "?nach=" + chat.last : "");
    return api("GET", path).then(function (d) {
      if (!chat.loaded) {
        chat.loaded = true;
        chat.list.textContent = "";
        if (!d.messages.length) chat.list.appendChild(el("p", "zq-tc-empty", "Noch keine Nachrichten. Schreib " + chat.partner.name + " etwas."));
      }
      append(chat, d.messages);
      chat.list.scrollTop = chat.list.scrollHeight;
    }, function (err) {
      if (err.status === 404) { remove(chat); return; }
      chat.error.textContent = err.message; chat.error.hidden = false;
    });
  }

  function markRead(chat) {
    if (chat.state === "min" || !chat.last || document.hidden) return;
    if (chat.readUpTo >= chat.last) return;
    chat.readUpTo = chat.last;
    api("POST", "/" + chat.id + "/gelesen", { last_id: chat.last }).then(function (d) { chat.unread = d.unread; setCount(chat); }, function () { chat.readUpTo = 0; });
  }

  function setState(chat, state) {
    if (state !== "min" && narrow()) {
      // Handy: nur ein Fenster offen, damit Seite und Zeiterfassung bedienbar bleiben.
      order.forEach(function (id) { if (id !== chat.id && chats[id].state !== "min") setState(chats[id], "min"); });
    }
    chat.state = state;
    chat.pill.classList.toggle("is-open", state !== "min");
    chat.pill.setAttribute("aria-expanded", state !== "min" ? "true" : "false");
    if (state === "min") {
      if (chat.win) chat.win.remove();
    } else {
      if (!chat.win) buildWindow(chat);
      chat.win.classList.toggle("is-max", state === "max");
      var maxBtn = chat.win.querySelector('[aria-label="Vergrößern"], [aria-label="Verkleinern"]');
      if (maxBtn) { maxBtn.setAttribute("aria-label", state === "max" ? "Verkleinern" : "Vergrößern"); maxBtn.title = state === "max" ? "Verkleinern" : "Vergrößern"; }
      if (!chat.win.parentNode) windowsEl.appendChild(chat.win);
      if (!chat.loaded) loadMessages(chat); else { chat.list.scrollTop = chat.list.scrollHeight; markRead(chat); }
    }
    setCount(chat);
    save();
    place();
  }

  function remove(chat) {
    if (chat.win) chat.win.remove();
    chat.pill.remove();
    delete chats[chat.id];
    order = order.filter(function (id) { return id !== chat.id; });
    save();
    place();
  }

  // --- Kollegen-Auswahl ("Teamchat") -------------------------------------------------------------
  function renderMenu() {
    menu.textContent = "";
    if (!colleagues.length) { menu.appendChild(el("p", null, "Keine aktiven Kollegen im Büro.")); return; }
    colleagues.forEach(function (c) {
      var b = el("button", null, c.name); b.type = "button"; b.setAttribute("role", "menuitem");
      b.addEventListener("click", function () {
        toggleMenu(false);
        api("POST", "/mit/" + c.id).then(function (d) {
          setState(ensure(d.chat, "open"), "open");
          if (chats[d.chat.id].input) chats[d.chat.id].input.focus();
        }, function (err) { window.alert(err.message); });
      });
      menu.appendChild(b);
    });
  }
  function toggleMenu(on) {
    menu.hidden = !on;
    newBtn.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) { renderMenu(); var first = menu.querySelector("button"); if (first) first.focus(); }
  }
  newBtn.addEventListener("click", function () { toggleMenu(menu.hidden); });
  document.addEventListener("click", function (e) { if (!menu.hidden && !e.target.closest(".zq-tc-picker")) toggleMenu(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !menu.hidden) { toggleMenu(false); newBtn.focus(); } });

  // --- Uebersicht und Live ------------------------------------------------------------------------
  function refresh() {
    return api("GET", "").then(function (d) {
      colleagues = d.colleagues || [];
      root.hidden = !colleagues.length && !order.length;
      (d.chats || []).forEach(function (info) {
        var chat = chats[info.id];
        if (chat) {
          ensure(info);
          if (chat.state !== "min" && chat.loaded) loadMessages(chat);
        } else if (info.unread) {
          ensure(info, "min"); // neue Nachricht: Name mit Zaehler unten, bleibt klein
          save();
        }
      });
      place();
      return d;
    });
  }

  function onChat(id) {
    var chat = chats[id];
    if (chat && chat.state !== "min" && chat.loaded) { loadMessages(chat); return; }
    refresh();
  }

  function connect(live) {
    if (!live || !window.EventSource) { startPolling(30000); return; }
    if (es) es.close();
    es = new EventSource(live.stream + "?token=" + encodeURIComponent(live.token));
    es.addEventListener("hello", function () { failures = 0; stopPolling(); });
    es.addEventListener("chat", function (ev) { try { onChat(JSON.parse(ev.data).chat); } catch (e) { /* ungueltig */ } });
    es.addEventListener("chat_read", function () { refresh(); });
    es.addEventListener("renew", function () { es.close(); es = null; refresh().then(function (d) { connect(d.live); }); });
    es.onerror = function () {
      if (es) { es.close(); es = null; }
      failures += 1;
      startPolling(15000); // bis die Verbindung wieder steht
      setTimeout(function () { refresh().then(function (d) { connect(d.live); }, function () {}); }, Math.min(60000, 2000 * Math.pow(2, Math.min(failures, 5))));
    };
  }
  function startPolling(ms) { stopPolling(); pollTimer = setInterval(function () { refresh().catch(function () {}); }, ms); }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  // --- Start ------------------------------------------------------------------------------------
  var saved = (load().chats || []).slice(0, 8);
  refresh().then(function (d) {
    var byId = {};
    (d.chats || []).forEach(function (c) { byId[c.id] = c; });
    saved.slice().reverse().forEach(function (s) {
      if (byId[s.id]) ensure(byId[s.id], s.state === "max" || s.state === "open" ? s.state : "min");
    });
    order.slice().forEach(function (id) { if (chats[id].state !== "min") setState(chats[id], chats[id].state); });
    root.hidden = !colleagues.length && !order.length;
    connect(d.live);
    place();
  }, function () { root.hidden = true; });

  window.addEventListener("resize", place);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) order.forEach(function (id) { markRead(chats[id]); });
  });
  window.addEventListener("pagehide", function () { if (es) es.close(); });
  if (window.ResizeObserver) {
    var clock = document.querySelector("[data-zs-clock]");
    if (clock) new ResizeObserver(place).observe(clock);
    new ResizeObserver(place).observe(bar);
  }
  setInterval(place, 1500); // Zeiterfassung wechselt Platz (Eingabefeld, Status) - billige Nachfuehrung
})();
