/* Interner 1:1-Teamchat (components/teamchat.html, API /arbeitsplatz/teamchat).
 *
 * - Minimierte Chats: schmale Pillen links neben "Ausgestempelt" (Zeiterfassung, js/shell.js); auf
 *   schmalen Bildschirmen in einer Zeile darueber. Die Position wird an der Zeiterfassung ausgerichtet
 *   (fester Abstand, neu berechnet bei Groesse/Zoom/Statuswechsel) - beide ueberlappen sich nie.
 * - Klick auf eine Pille klappt das Fenster nach oben auf (Kopf: Name, Vergroessern, Minimieren,
 *   Schliessen). Der Rest der Seite bleibt bedienbar.
 * - Zustand (offen/minimiert/vergroessert) je Benutzer im Browser - bleibt beim Seitenwechsel.
 * - Handy (schmal): kein schwebender Knopf ueber dem Inhalt. Fester Platz in der Kopfzeile (Symbol
 *   "Teamchat" mit Ungelesen-Zaehler, [data-zq-tc-head]); geoeffnet als Blatt zwischen Kopfzeile und
 *   Zeiterfassung, Hoehe folgt dem sichtbaren Bereich (visualViewport: Tastatur, Safari-Adressleiste).
 * - Emojis: Smiley-Knopf neben dem Feld, Auswahl oeffnet nach oben, Einfuegen an der Cursorposition.
 * - Antworten: Menue an jeder Nachricht (Knopf "…", Handy zusaetzlich langes Tippen, Tastatur: Pfeile/
 *   Enter/Escape). Vorschau ueber dem Feld (× entfernt nur das Zitat). Zitat in der Blase springt zum
 *   Original (laedt aelteren Verlauf nach: ?ab=<id>&vor=<aelteste>); Absender/Ausschnitt liefert der Server.
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
  var heads = Array.prototype.slice.call(document.querySelectorAll("[data-zq-tc-head]"));
  var sheetClose = root.querySelector("[data-zq-tc-sheet-close]");
  var sheetOpen = false, ready = false, unreadById = {};

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

  // --- Handy: Kopfzeilen-Symbol und Blatt ----------------------------------------------------------
  function visibleHead() {
    return heads.find(function (b) { return b.getClientRects().length && getComputedStyle(b).display !== "none"; }) || null;
  }
  function anyOpen() { return order.some(function (id) { return chats[id].state !== "min"; }); }
  function updateHeads() {
    var total = 0;
    Object.keys(unreadById).forEach(function (id) { total += unreadById[id] || 0; });
    order.forEach(function (id) { if (!(id in unreadById)) total += chats[id].unread || 0; });
    heads.forEach(function (b) {
      b.hidden = !ready || (!colleagues.length && !order.length);
      b.setAttribute("aria-expanded", sheetOpen ? "true" : "false");
      b.setAttribute("aria-label", total ? "Teamchat, " + total + " ungelesen" : "Teamchat");
      var c = b.querySelector("[data-zq-tc-head-count]");
      if (c) { c.textContent = total > 99 ? "99+" : String(total || ""); c.hidden = !total; }
    });
  }
  function openSheet() {
    sheetOpen = true;
    applyVisibility();
    if (!order.length) toggleMenu(true);
    else { var first = root.querySelector(".zq-tc-pill, .zq-tc-new"); if (first) first.focus(); }
  }
  function closeSheet() {
    sheetOpen = false;
    order.forEach(function (id) { if (chats[id].state !== "min") setState(chats[id], "min"); });
    toggleMenu(false);
    applyVisibility();
    var head = visibleHead();
    if (head) head.focus();
  }
  heads.forEach(function (b) { b.addEventListener("click", function () { sheetOpen ? closeSheet() : openSheet(); }); });
  if (sheetClose) sheetClose.addEventListener("click", closeSheet);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && sheetOpen && menu.hidden) closeSheet(); });

  function applyVisibility() {
    var mobile = narrow();
    root.classList.toggle("is-mobile", mobile);
    root.classList.toggle("has-open", anyOpen());
    if (mobile) root.hidden = !sheetOpen;
    else { sheetOpen = false; root.hidden = !ready || (!colleagues.length && !order.length); }
    updateHeads();
    place();
  }

  function placeSheet() {
    // Blatt unter der Kopfzeile bis ueber die Zeiterfassung (bzw. bis zum Rand des sichtbaren Bereichs).
    var head = visibleHead();
    var header = head ? head.closest("header") : null;
    var top = header ? Math.max(0, header.getBoundingClientRect().bottom) : 0;
    var vv = window.visualViewport;
    var visibleBottom = vv ? vv.height + vv.offsetTop : window.innerHeight;
    var clock = document.querySelector("[data-zs-clock]");
    var fixedClock = clock && getComputedStyle(clock).position === "fixed" && clock.getClientRects().length;
    var pill = fixedClock && clock.querySelector(".zs-clock-pill");
    var bottom = visibleBottom;
    if (pill) bottom = Math.min(bottom, pill.getBoundingClientRect().top - GAP);  // Zeiterfassung bleibt frei
    root.style.top = top + "px";
    root.style.height = Math.max(200, bottom - top) + "px";
    root.style.right = root.style.bottom = root.style.maxWidth = "";
  }

  // --- Position an der Zeiterfassung -----------------------------------------------------------------
  function place() {
    if (narrow()) { placeSheet(); return; }
    root.style.top = root.style.height = "";
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
    unreadById[chat.id] = chat.unread || 0;
    updateHeads();
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

  // --- Emojis --------------------------------------------------------------------------------------
  var EMOJIS = ("😀 😂 😊 🙂 😉 😍 😘 😎 🤔 😅 😮 😢 😭 😡 🙈 👍 👍🏽 👎 👏 🙏 💪 👌 ✌️ 🤝 👋 👀 " +
    "❤️ 💙 💚 🎉 🔥 ⭐ ✅ ❌ ⚠️ ❓ ☕ 🍰 📞 📅 📄 📎 💶 🚗 🏠 ⏰ 🌞 🤒").split(" ");

  function insertAtCursor(input, text) {
    var start = input.selectionStart == null ? input.value.length : input.selectionStart;
    var end = input.selectionEnd == null ? start : input.selectionEnd;
    if (input.setRangeText) input.setRangeText(text, start, end, "end");
    else { input.value = input.value.slice(0, start) + text + input.value.slice(end); input.selectionStart = input.selectionEnd = start + text.length; }
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function buildEmojiPicker(chat, input, toggle) {
    var panel = el("div", "zq-tc-emoji");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Emoji auswählen");
    panel.hidden = true;
    var grid = el("div", "zq-tc-emoji-grid");
    EMOJIS.forEach(function (e) {
      var b = el("button", null, e); b.type = "button"; b.setAttribute("aria-label", "Emoji " + e);
      b.addEventListener("mousedown", function (ev) { ev.preventDefault(); }); // Fokus/Cursor im Feld behalten
      b.addEventListener("click", function () { insertAtCursor(input, e); close(false); if (!narrow()) input.focus(); });
      grid.appendChild(b);
    });
    panel.appendChild(grid);
    function buttons() { return Array.prototype.slice.call(grid.querySelectorAll("button")); }
    panel.addEventListener("keydown", function (ev) {
      var list = buttons(), i = list.indexOf(document.activeElement), cols = 8;
      var move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols }[ev.key];
      if (move && i >= 0) { ev.preventDefault(); list[Math.max(0, Math.min(list.length - 1, i + move))].focus(); }
      else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(true); }
    });
    function open() {
      panel.hidden = false; toggle.setAttribute("aria-expanded", "true");
      // passt immer ins Fenster (auch mit Handy-Tastatur): hoechstens bis unter die Fensterkopfzeile
      var win = panel.closest(".zq-tc-win"), head = win && win.querySelector(".zq-tc-head"), form = panel.parentNode;
      if (win && head) panel.style.maxHeight = Math.max(96, Math.min(184, form.getBoundingClientRect().top - head.getBoundingClientRect().bottom - 8)) + "px";
      if (!narrow() || !("ontouchstart" in window)) buttons()[0].focus();
    }
    function close(focusToggle) {
      panel.hidden = true; toggle.setAttribute("aria-expanded", "false");
      if (focusToggle) toggle.focus();
    }
    toggle.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
    toggle.addEventListener("click", function () { panel.hidden ? open() : close(false); });
    document.addEventListener("click", function (ev) {
      if (!panel.hidden && !panel.contains(ev.target) && ev.target !== toggle && !toggle.contains(ev.target)) close(false);
    });
    chat.closeEmoji = function () { close(false); };
    return panel;
  }

  // --- Antworten: Menue an der Nachricht ------------------------------------------------------------
  var msgMenu = null;
  function closeMsgMenu(focusBack) {
    if (!msgMenu) return;
    var back = msgMenu.opener;
    msgMenu.remove(); msgMenu = null;
    if (back) { back.setAttribute("aria-expanded", "false"); if (focusBack) back.focus(); }
  }
  function openMsgMenu(chat, li, opener) {
    closeMsgMenu(false);
    var m = li._msg;
    var menuEl = el("div", "zq-tc-msgmenu");
    menuEl.setAttribute("role", "menu");
    menuEl.setAttribute("aria-label", "Aktionen zur Nachricht");
    function item(label, fn) {
      var b = el("button", null, label); b.type = "button"; b.setAttribute("role", "menuitem"); b.tabIndex = -1;
      b.addEventListener("click", function () { closeMsgMenu(false); fn(); });
      menuEl.appendChild(b);
    }
    item("Antworten", function () { startReply(chat, m); });
    item("Text kopieren", function () {
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(m.text).catch(function () {});
      if (opener) opener.focus();
    });
    menuEl.addEventListener("keydown", function (ev) {
      var items = Array.prototype.slice.call(menuEl.querySelectorAll("button")), i = items.indexOf(document.activeElement);
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); items[(i + (ev.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus(); }
      else if (ev.key === "Home") { ev.preventDefault(); items[0].focus(); }
      else if (ev.key === "End") { ev.preventDefault(); items[items.length - 1].focus(); }
      else if (ev.key === "Escape" || ev.key === "Tab") { ev.preventDefault(); ev.stopPropagation(); closeMsgMenu(true); }
    });
    li.appendChild(menuEl);
    menuEl.opener = opener || li.querySelector(".zq-tc-more");
    msgMenu = menuEl;
    if (menuEl.opener) menuEl.opener.setAttribute("aria-expanded", "true");
    // nach oben aufklappen, wenn unten kein Platz ist
    var listRect = chat.list.getBoundingClientRect(), r = menuEl.getBoundingClientRect();
    if (r.bottom > listRect.bottom - 4) menuEl.classList.add("is-up");
    menuEl.querySelector("button").focus();
  }
  document.addEventListener("click", function (ev) {
    if (msgMenu && !msgMenu.contains(ev.target) && !(msgMenu.opener && msgMenu.opener.contains(ev.target))) closeMsgMenu(false);
  });

  function shortText(text) {
    var flat = String(text || "").replace(/\s+/g, " ").trim();
    var chars = Array.from ? Array.from(flat) : flat.split("");
    return chars.length > 90 ? chars.slice(0, 90).join("").trim() + " …" : flat;
  }

  function startReply(chat, m) {
    chat.replyTo = { id: m.id };
    chat.draft.querySelector(".zq-tc-draft-sender").textContent = m.mine ? "Du" : chat.partner.name;
    chat.draft.querySelector(".zq-tc-draft-text").textContent = shortText(m.text);
    chat.draft.hidden = false;
    chat.input.focus();
    place();
  }
  function clearReply(chat) {
    chat.replyTo = null;
    if (chat.draft) chat.draft.hidden = true;
  }

  function jumpTo(chat, id) {
    function show() {
      var target = chat.list.querySelector('[data-id="' + id + '"]');
      if (!target) return false;
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      target.classList.remove("is-flash"); void target.offsetWidth; target.classList.add("is-flash");
      target.tabIndex = -1; target.focus({ preventScroll: true });
      setTimeout(function () { target.classList.remove("is-flash"); }, 1800);
      return true;
    }
    if (show()) return;
    var first = chat.list.querySelector(".zq-tc-msg[data-id]");
    var oldest = first ? Number(first.getAttribute("data-id")) : 0;
    function unavailable() { chat.error.textContent = "Originalnachricht nicht verfügbar"; chat.error.hidden = false; setTimeout(function () { chat.error.hidden = true; }, 3000); }
    if (!oldest || id >= oldest) { unavailable(); return; }
    api("GET", "/" + chat.id + "/nachrichten?ab=" + id + "&vor=" + oldest).then(function (d) {
      prepend(chat, d.messages);
      if (!show()) unavailable();
    }, unavailable);
  }

  function renderMessage(chat, m) {
    var li = el("li", "zq-tc-msg" + (m.mine ? " is-mine" : ""));
    li.setAttribute("data-id", String(m.id));
    li._msg = m;
    if (m.reply) {
      if (m.reply.missing) {
        li.appendChild(el("span", "zq-tc-quote is-missing", "Originalnachricht nicht verfügbar"));
      } else {
        var q = el("button", "zq-tc-quote" + (m.reply.mine ? " is-mine" : "")); q.type = "button";
        q.setAttribute("aria-label", "Zitat von " + m.reply.sender + ": " + m.reply.excerpt + " – zur Originalnachricht springen");
        q.appendChild(el("strong", null, m.reply.sender));
        q.appendChild(el("span", null, m.reply.excerpt));
        q.addEventListener("click", function () { jumpTo(chat, m.reply.id); });
        li.appendChild(q);
      }
    }
    li.appendChild(el("span", "zq-tc-text", m.text));
    li.appendChild(el("time", null, m.when));
    var more = el("button", "zq-tc-more", "⋯"); more.type = "button";
    more.setAttribute("aria-label", "Aktionen zur Nachricht"); more.setAttribute("aria-haspopup", "menu"); more.setAttribute("aria-expanded", "false");
    more.addEventListener("click", function (ev) { ev.stopPropagation(); if (msgMenu && msgMenu.opener === more) closeMsgMenu(true); else openMsgMenu(chat, li, more); });
    li.appendChild(more);
    // Handy: langes Tippen oeffnet dasselbe Menue
    var timer = null, sx = 0, sy = 0, fired = false;
    li.addEventListener("touchstart", function (ev) {
      fired = false;
      if (ev.target.closest(".zq-tc-quote, .zq-tc-more, .zq-tc-msgmenu")) return; sx = ev.touches[0].clientX; sy = ev.touches[0].clientY;
      timer = setTimeout(function () { fired = true; timer = null; if (navigator.vibrate) navigator.vibrate(15); openMsgMenu(chat, li, more); }, 450);
    }, { passive: true });
    li.addEventListener("touchmove", function (ev) {
      if (timer && (Math.abs(ev.touches[0].clientX - sx) > 10 || Math.abs(ev.touches[0].clientY - sy) > 10)) { clearTimeout(timer); timer = null; }
    }, { passive: true });
    li.addEventListener("touchend", function (ev) {
      if (timer) { clearTimeout(timer); timer = null; }
      if (fired) { ev.preventDefault(); fired = false; } // nur das Loslassen nach dem langen Tippen schlucken
    });
    li.addEventListener("contextmenu", function (ev) { if (timer || msgMenu) ev.preventDefault(); });
    return li;
  }

  function prepend(chat, items) {
    var list = chat.list, before = list.scrollHeight, top = list.scrollTop;
    var anchor = list.querySelector(".zq-tc-msg");
    items.forEach(function (m) {
      if (list.querySelector('[data-id="' + m.id + '"]')) return;
      list.insertBefore(renderMessage(chat, m), anchor);
    });
    list.scrollTop = top + (list.scrollHeight - before);
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
    // Zitatvorschau direkt ueber dem Feld
    var draft = el("div", "zq-tc-draft"); draft.hidden = true;
    var draftBody = el("div", "zq-tc-draft-body");
    draftBody.appendChild(el("strong", "zq-tc-draft-sender"));
    draftBody.appendChild(el("span", "zq-tc-draft-text"));
    var draftX = el("button", "zq-tc-draft-x", "×"); draftX.type = "button"; draftX.setAttribute("aria-label", "Zitat entfernen"); draftX.title = "Zitat entfernen";
    draftX.addEventListener("click", function () { clearReply(chat); input.focus(); place(); });
    draft.appendChild(draftBody); draft.appendChild(draftX);
    win.appendChild(draft);
    chat.draft = draft;
    var form = el("form", "zq-tc-form");
    var input = el("textarea"); input.rows = 1; input.maxLength = 2000; input.placeholder = "Nachricht …";
    input.setAttribute("aria-label", "Nachricht an " + chat.partner.name);
    var smile = el("button", "zq-tc-smile", "🙂"); smile.type = "button";
    smile.setAttribute("aria-label", "Emoji einfügen"); smile.title = "Emoji einfügen";
    smile.setAttribute("aria-haspopup", "dialog"); smile.setAttribute("aria-expanded", "false");
    var send = el("button", "btn btn-primary btn-sm", "Senden"); send.type = "submit";
    form.appendChild(smile); form.appendChild(input); form.appendChild(send);
    form.appendChild(buildEmojiPicker(chat, input, smile));
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : submit(); }
      else if (e.key === "Escape" && chat.replyTo) { e.preventDefault(); e.stopPropagation(); clearReply(chat); }
    });
    input.addEventListener("input", function () { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 112) + "px"; });
    form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
    function submit() {
      var text = input.value.trim();
      if (!text || send.disabled) return;
      send.disabled = true;
      chat.error.hidden = true;
      var body = { text: text, client_ref: newRef() };
      if (chat.replyTo) body.reply_to = chat.replyTo.id;
      if (chat.closeEmoji) chat.closeEmoji();
      api("POST", "/" + chat.id + "/nachrichten", body).then(function (d) {
        input.value = ""; input.style.height = "auto";
        clearReply(chat);
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
      if (chat.list.querySelector('[data-id="' + m.id + '"]')) return;
      chat.list.appendChild(renderMessage(chat, m));
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
    if (state !== "min" && narrow() && !sheetOpen) { sheetOpen = true; }
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
    applyVisibility();
  }

  function remove(chat) {
    if (chat.win) chat.win.remove();
    chat.pill.remove();
    delete chats[chat.id];
    order = order.filter(function (id) { return id !== chat.id; });
    save();
    applyVisibility();
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
      unreadById = {};
      (d.chats || []).forEach(function (info) { unreadById[info.id] = info.unread || 0; });
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
      applyVisibility();
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
      // Handy: beim Seitenwechsel nicht ueber die neue Seite legen - Chats bleiben im Blatt minimiert.
      var restore = !narrow() && (s.state === "max" || s.state === "open") ? s.state : "min";
      if (byId[s.id]) ensure(byId[s.id], restore);
    });
    order.slice().forEach(function (id) { if (chats[id].state !== "min") setState(chats[id], chats[id].state); });
    ready = true;
    connect(d.live);
    applyVisibility();
  }, function () { root.hidden = true; });

  window.addEventListener("resize", applyVisibility);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", place);  // iPhone-Tastatur, Safari-Adressleiste
    window.visualViewport.addEventListener("scroll", place);
  }
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
