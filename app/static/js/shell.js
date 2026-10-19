// ZentriqAI-Oberflaeche auf allen Seiten:
// - Seitenleiste: einklappen (Desktop), gespeicherte/letzte Unterhaltungen, Chat-Suche
// - Zeiterfassung unten rechts: Status, Ein-/Ausstempeln, Pause - ueber die bestehenden
//   Endpunkte der Zeiterfassung, ohne Seitenreload.
// Inhalte werden nur per textContent eingesetzt. localStorage nur fuer den Einklapp-Zustand.
(function () {
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  function csrf() { return csrfMeta ? csrfMeta.getAttribute("content") : ""; }

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw Object.assign(new Error(data.error || "Fehler"), { data: data });
        return data;
      });
    });
  }

  function postJSON(url) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRFToken": csrf() },
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw Object.assign(new Error(data.error || "Aktion nicht möglich."), { data: data });
        return data;
      });
    }, function () { throw new Error("Keine Verbindung zum Server."); });
  }

  // --- Seitenleiste: einklappen ---------------------------------------------------------
  var KEY = "zentriq-sidebar-collapsed";
  try { if (localStorage.getItem(KEY) === "1") document.body.classList.add("zs-collapsed"); } catch (e) {}
  function setCollapsed(state) {
    document.body.classList.toggle("zs-collapsed", state);
    try { localStorage.setItem(KEY, state ? "1" : "0"); } catch (e) {}
  }
  var collapseBtn = document.querySelector("[data-zs-collapse]");
  var expandBtn = document.querySelector("[data-zs-expand]");
  if (collapseBtn) collapseBtn.addEventListener("click", function () { setCollapsed(true); });
  if (expandBtn) expandBtn.addEventListener("click", function () { setCollapsed(false); });

  // --- Seitenleiste: Unterhaltungen -----------------------------------------------------
  var sidebar = document.querySelector("[data-zs-sidebar]");
  var activeId = null;
  var searchTimer = null;

  function chatItem(c) {
    var li = document.createElement("li");
    var a = document.createElement("a");
    a.className = "app-sidebar-link zs-chat-link" + (String(c.id) === String(activeId) ? " is-active" : "");
    a.href = sidebar.dataset.chatBase + "/c/" + c.id;
    a.dataset.zsChatLink = c.id;
    a.title = c.title + (c.project ? " · " + c.project : "");
    var span = document.createElement("span");
    span.textContent = c.title;
    a.appendChild(span);
    li.appendChild(a);
    return li;
  }

  function renderChats(list, pinnedList, query) {
    var pinnedBox = sidebar.querySelector("[data-zs-pinned-box]");
    var pinnedUl = sidebar.querySelector("[data-zs-pinned]");
    var recentUl = sidebar.querySelector("[data-zs-recent]");
    var heading = sidebar.querySelector("[data-zs-recent-heading]");
    pinnedUl.textContent = "";
    recentUl.textContent = "";
    (pinnedList || []).forEach(function (c) { pinnedUl.appendChild(chatItem(c)); });
    pinnedBox.hidden = !pinnedUl.children.length;
    list.forEach(function (c) { recentUl.appendChild(chatItem(c)); });
    heading.textContent = query ? "Suchergebnisse" : "Letzte Unterhaltungen";
    sidebar.querySelector("[data-zs-chats-empty]").hidden = list.length > 0 || pinnedUl.children.length > 0;
    sidebar.querySelector("[data-zs-chats-empty]").textContent = query ? "Keine Treffer." : "Noch keine Unterhaltungen.";
  }

  function refreshChats() {
    if (!sidebar) return Promise.resolve();
    var input = sidebar.querySelector("[data-zs-chat-search]");
    var q = input ? input.value.trim() : "";
    var base = sidebar.dataset.chatsUrl;
    if (q) {
      return getJSON(base + "?limit=30&q=" + encodeURIComponent(q)).then(function (d) { renderChats(d.conversations || [], [], q); }).catch(function () {});
    }
    return Promise.all([getJSON(base + "?limit=20&pinned=0"), getJSON(base + "?limit=20&pinned=1")])
      .then(function (r) { renderChats(r[0].conversations || [], r[1].conversations || [], ""); })
      .catch(function () {});
  }

  function setActive(id) {
    activeId = id;
    if (!sidebar) return;
    Array.prototype.forEach.call(sidebar.querySelectorAll("[data-zs-chat-link]"), function (a) {
      a.classList.toggle("is-active", String(a.dataset.zsChatLink) === String(id));
    });
  }

  if (sidebar) {
    var search = sidebar.querySelector("[data-zs-chat-search]");
    if (search) search.addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(refreshChats, 250);
    });
    // Auf der Chat-Seite oeffnet ein Klick die Unterhaltung ohne Seitenwechsel.
    sidebar.addEventListener("click", function (e) {
      var link = e.target.closest("[data-zs-chat-link]");
      var isNew = e.target.closest("[data-zs-new]");
      if (!window.ZentriqChat || e.metaKey || e.ctrlKey || e.shiftKey) return;
      if (link) { e.preventDefault(); window.ZentriqChat.open(link.dataset.zsChatLink); }
      else if (isNew) { e.preventDefault(); window.ZentriqChat.reset(); }
      else return;
      document.body.classList.remove("nav-open");
      if (window.Alpine) try { Alpine.$data(document.body).navOpen = false; } catch (err) {}
    });
    refreshChats();
  }

  window.ZentriqShell = { refreshChats: refreshChats, setActive: setActive };

  // --- Zeiterfassung unten rechts -------------------------------------------------------
  var clock = document.querySelector("[data-zs-clock]");
  if (!clock) return;
  var pill = clock.querySelector("[data-zs-clock-toggle]");
  var panel = clock.querySelector("[data-zs-clock-panel]");
  var labelEl = clock.querySelector("[data-zs-clock-label]");
  var timeEl = clock.querySelector("[data-zs-clock-time]");
  var statusEl = clock.querySelector("[data-zs-clock-status]");
  var errorEl = clock.querySelector("[data-zs-clock-error]");
  var buttons = {};
  Array.prototype.forEach.call(clock.querySelectorAll("[data-zs-clock-action]"), function (b) { buttons[b.dataset.zsClockAction] = b; });
  var current = null;
  var loadedAt = 0;
  var busy = false;

  function fmt(seconds) {
    var m = Math.max(0, Math.floor(seconds / 60));
    return Math.floor(m / 60) + ":" + String(m % 60).padStart(2, "0");
  }

  function netNow() {
    if (!current) return 0;
    var extra = current.status === "in" ? (Date.now() - loadedAt) / 1000 : 0;
    return current.today_net_seconds + extra;
  }

  function render() {
    if (!current) return;
    clock.dataset.status = current.status;
    var labels = { "in": "Eingestempelt", "break": "Pause", "out": "Ausgestempelt" };
    labelEl.textContent = labels[current.status] || "Zeiterfassung";
    timeEl.textContent = fmt(netNow());
    var text = current.status === "in" ? "Eingestempelt seit " + current.since + " Uhr."
      : current.status === "break" ? "In Pause (Arbeitsbeginn " + current.since + " Uhr)."
      : "Ausgestempelt.";
    statusEl.textContent = text + " Heute: " + fmt(netNow()) + " Std.";
    buttons["in"].hidden = current.status !== "out";
    buttons["break-start"].hidden = current.status !== "in";
    buttons["break-end"].hidden = current.status !== "break";
    buttons["out"].hidden = current.status === "out";
  }

  function apply(data) {
    current = data;
    loadedAt = Date.now();
    render();
  }

  function load() {
    return getJSON(clock.dataset.statusUrl).then(apply).catch(function () {
      labelEl.textContent = "Zeiterfassung";
      statusEl.textContent = "Status nicht verfügbar.";
    });
  }

  var urls = { "in": clock.dataset.inUrl, "out": clock.dataset.outUrl, "break-start": clock.dataset.breakStartUrl, "break-end": clock.dataset.breakEndUrl };
  Object.keys(buttons).forEach(function (action) {
    buttons[action].addEventListener("click", function () {
      if (busy) return;
      if (action === "out" && !window.confirm("Jetzt ausstempeln?")) return;
      busy = true;
      errorEl.hidden = true;
      postJSON(urls[action]).then(apply).catch(function (err) {
        errorEl.textContent = err.message;
        errorEl.hidden = false;
        if (err.data && err.data.status) apply(err.data);
      }).finally(function () { busy = false; });
    });
  });

  pill.addEventListener("click", function () {
    panel.hidden = !panel.hidden;
    pill.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) load();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !panel.hidden) { panel.hidden = true; pill.setAttribute("aria-expanded", "false"); }
  });

  load();
  setInterval(render, 30000);
  setInterval(function () { if (!document.hidden) load(); }, 5 * 60000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) load(); });
})();
