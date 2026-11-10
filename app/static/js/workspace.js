// Arbeitsplatz fuer Buero-Rollen (components/workspace.html, app/services/workspace.py):
// 1. Tabs: Kundenakten, Kalender und Kfz-Angebote parallel offen halten (gespeichert im Browser).
// 2. Seitenfenster: Kundenakte rechts (Handy: Vollbild mit Zurueck), die Liste bleibt unveraendert.
// 3. Bearbeiten direkt am Feld (Kundenakte) ueber die vorhandene serverseitige Pruefung.
// 4. Formularentwuerfe (Kfz-Angebot): Zwischenstand serverseitig je Benutzer, fortsetzen/verwerfen.
// 5. Schnellsuche (Strg/Cmd+K, Lupen-Knopf): Kunden und Seiten.
// Alles ohne KI-Aufrufe. Rechte prueft immer der Server.
(function () {
  "use strict";

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  function csrf() { return csrfMeta ? csrfMeta.getAttribute("content") : ""; }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function store(kind) { try { return window[kind]; } catch (e) { return null; } }
  function readJSON(kind, key, fallback) {
    var s = store(kind);
    if (!s) return fallback;
    try { var v = JSON.parse(s.getItem(key)); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function writeJSON(kind, key, value) {
    var s = store(kind);
    if (!s) return;
    try { s.setItem(key, JSON.stringify(value)); } catch (e) { /* voll oder gesperrt: nur Komfort */ }
  }
  function sendJSON(method, url, body, keepalive) {
    return fetch(url, {
      method: method,
      credentials: "same-origin",
      keepalive: !!keepalive,
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf() },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var err = new Error(data.error || "Speichern nicht möglich."); err.status = r.status; throw err; }
        return data;
      });
    });
  }
  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

  // =========================================================================================
  // 1. Tabs
  // =========================================================================================
  var tabBar = document.querySelector("[data-zq-tabs]");
  var TAB_KEY = tabBar ? "zq-tabs:" + (tabBar.getAttribute("data-user") || "") : null;
  var MAX_TABS = 8;
  var here = location.pathname;

  function loadTabs() { return TAB_KEY ? readJSON("localStorage", TAB_KEY, []) : []; }
  function saveTabs(tabs) { if (TAB_KEY) writeJSON("localStorage", TAB_KEY, tabs); }
  function findTab(tabs, path) { for (var i = 0; i < tabs.length; i++) if (tabs[i].path === path) return i; return -1; }

  function setTabState(path, patch) {
    var tabs = loadTabs(), i = findTab(tabs, path), changed = false;
    if (i < 0) return;
    for (var k in patch) if (tabs[i][k] !== patch[k]) { tabs[i][k] = patch[k]; changed = true; }
    // Nur bei echter Aenderung neu zeichnen - sonst ginge ein Klick auf "×" verloren, wenn das
    // Verlassen eines Felds (change) die Leiste zwischen Maus-Druecken und -Loslassen erneuert.
    if (!changed) return;
    saveTabs(tabs);
    renderTabs();
  }

  function registerTab() {
    var main = document.getElementById("inhalt");
    var marker = main && main.querySelector("[data-zq-tab-kind]");
    if (!marker || !TAB_KEY) return;
    var tabs = loadTabs();
    var entry = { path: here, url: location.pathname + location.search, kind: marker.getAttribute("data-zq-tab-kind"),
                  title: marker.getAttribute("data-zq-tab-title") || document.title };
    var form = main.querySelector("[data-zq-draft]");
    if (form) entry.draftKey = form.getAttribute("data-zq-draft");
    // Nach dem Absenden (z. B. neues Angebot -> gespeicherter Auftrag) den bisherigen Tab ersetzen.
    var replace = readJSON("sessionStorage", "zq-tab-replace", null);
    writeJSON("sessionStorage", "zq-tab-replace", null);
    var i = findTab(tabs, here);
    var j = replace ? findTab(tabs, replace) : -1;
    if (i < 0 && j >= 0) { entry.dirty = false; tabs[j] = entry; }
    else if (i >= 0) { entry.dirty = tabs[i].dirty && !!entry.draftKey; tabs[i] = entry; }
    else {
      entry.dirty = false;
      tabs.push(entry);
      while (tabs.length > MAX_TABS) {
        var drop = -1;
        for (var d = 0; d < tabs.length; d++) if (!tabs[d].dirty && tabs[d].path !== here) { drop = d; break; }
        if (drop < 0) break;
        tabs.splice(drop, 1);
      }
    }
    saveTabs(tabs);
  }

  function rememberScroll() {
    var pos = readJSON("sessionStorage", "zq-tab-scroll", {});
    pos[here] = window.scrollY || document.documentElement.scrollTop || 0;
    writeJSON("sessionStorage", "zq-tab-scroll", pos);
  }

  function openTab(tab) {
    rememberScroll();
    writeJSON("sessionStorage", "zq-tab-resume", tab.path);
    location.href = tab.url;
  }

  function closeTab(tab) {
    if (tab.dirty && !window.confirm("In „" + tab.title + "“ gibt es ungespeicherte Eingaben.\nTab trotzdem schließen? Der Entwurf wird verworfen.")) return;
    var discard = tab.dirty && tab.draftKey ? sendJSON("DELETE", "/arbeitsplatz/entwuerfe/" + encodeURIComponent(tab.draftKey)).catch(function () {}) : Promise.resolve();
    var tabs = loadTabs(), i = findTab(tabs, tab.path);
    if (i >= 0) tabs.splice(i, 1);
    saveTabs(tabs);
    if (tab.path === here) {
      if (draft) draft.detach();
      var next = tabs[Math.min(i, tabs.length - 1)];
      discard.then(function () { if (next) openTab(next); else renderTabs(); });
    } else {
      renderTabs();
    }
  }

  function renderTabs() {
    if (!tabBar) return;
    var tabs = loadTabs();
    tabBar.textContent = "";
    tabBar.hidden = !tabs.length;
    var icons = { kunde: "👤", kalender: "📅", angebot: "🚗" };
    tabs.forEach(function (tab) {
      var item = el("div", "zq-tab" + (tab.path === here ? " is-active" : "") + (tab.dirty ? " is-dirty" : ""));
      var link = el("a", "zq-tab-link");
      link.href = tab.url;
      link.title = tab.title + (tab.dirty ? " – ungespeicherte Eingaben" : "");
      if (tab.path === here) link.setAttribute("aria-current", "page");
      link.appendChild(el("span", "zq-tab-ico", icons[tab.kind] || "•")).setAttribute("aria-hidden", "true");
      link.appendChild(el("span", "zq-tab-title", tab.title));
      if (tab.dirty) link.appendChild(el("span", "zq-tab-dot", "●")).setAttribute("aria-label", "ungespeichert");
      link.addEventListener("click", function (ev) {
        if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button !== 0) return;
        ev.preventDefault();
        if (tab.path !== here) openTab(tab);
      });
      var x = el("button", "zq-tab-close", "×");
      x.type = "button";
      x.setAttribute("aria-label", "Tab „" + tab.title + "“ schließen");
      x.addEventListener("click", function () { closeTab(tab); });
      item.appendChild(link);
      item.appendChild(x);
      tabBar.appendChild(item);
    });
  }

  registerTab();
  // Scrollposition beim Zurueckwechseln in einen Tab wiederherstellen.
  var resumePath = readJSON("sessionStorage", "zq-tab-resume", null);
  writeJSON("sessionStorage", "zq-tab-resume", null);
  var resumed = resumePath === here;
  if (resumed) {
    var pos = readJSON("sessionStorage", "zq-tab-scroll", {})[here];
    if (pos) window.addEventListener("load", function () { window.scrollTo(0, pos); });
  }
  window.addEventListener("pagehide", rememberScroll);
  window.addEventListener("storage", function (ev) { if (ev.key === TAB_KEY) renderTabs(); });

  // =========================================================================================
  // 4. Formularentwuerfe (Kfz-Angebot)
  // =========================================================================================
  var draft = null;
  var draftForm = document.querySelector("[data-zq-draft]");
  if (draftForm) draft = initDraft(draftForm);

  function initDraft(form) {
    var url = form.getAttribute("data-zq-draft-url");
    var banner = document.querySelector("[data-zq-draft-banner]");
    var state = document.querySelector("[data-zq-draft-state]");
    var timer = null, holding = false, active = true, lastSent = null;

    function collect() {
      var data = {};
      new FormData(form).forEach(function (value, name) {
        if (name === "csrf_token" || name === "aktion" || name === "ki_text" || typeof value !== "string") return;
        var field = form.querySelector('[name="' + name + '"]');
        if (field && field.type === "checkbox") { (data[name] = data[name] || []).push(value); }
        else data[name] = value;
      });
      var who = form.querySelector("[data-huk-customer-name]");
      if (who && data.kunde) data._kunde_name = who.textContent.trim();
      return data;
    }

    function apply(data) {
      Object.keys(data).forEach(function (name) {
        if (name === "_kunde_name") return;
        var value = data[name];
        var fields = form.querySelectorAll('[name="' + name + '"]');
        Array.prototype.forEach.call(fields, function (field) {
          if (field.type === "radio") field.checked = field.value === value;
          else if (field.type === "checkbox") field.checked = Array.isArray(value) && value.indexOf(field.value) >= 0;
          else field.value = value;
        });
      });
      // Kundenzuordnung sichtbar machen (huk-form.js verwaltet die Suche).
      var current = form.querySelector("[data-huk-customer-current]"), search = form.querySelector("[data-huk-customer-search]");
      var name = form.querySelector("[data-huk-customer-name]");
      if (current && search && name) {
        current.hidden = !data.kunde;
        search.hidden = !!data.kunde;
        name.textContent = data.kunde ? (data._kunde_name || "") : "";
      }
      form.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

    function setState(text) { if (state) state.textContent = text; }

    function save(keepalive) {
      if (!active || holding) return Promise.resolve();
      clearTimeout(timer);
      timer = null;
      var data = collect();
      if (lastSent && same(data, lastSent)) return Promise.resolve();
      lastSent = data;
      return sendJSON("PUT", url, { data: data }, keepalive).then(function (r) {
        setState("Entwurf gespeichert · " + r.draft.updated_label.split(", ")[1]);
      }).catch(function () { setState("Entwurf konnte nicht gespeichert werden – Eingaben bleiben auf dieser Seite."); lastSent = null; });
    }

    function changed() {
      if (!active || holding) return;
      setTabState(here, { dirty: true });
      clearTimeout(timer);
      timer = setTimeout(save, 1200);
    }

    form.addEventListener("input", changed);
    form.addEventListener("change", function (ev) { if (ev.isTrusted) changed(); });
    form.addEventListener("submit", function () {
      // Absenden: der Server uebernimmt die Angaben und loescht den Entwurf nach erfolgreichem Speichern.
      clearTimeout(timer);
      active = false;
      setTabState(here, { dirty: false });
      writeJSON("sessionStorage", "zq-tab-replace", here);
    });
    window.addEventListener("pagehide", function () { if (timer) save(true); });

    function discard() {
      holding = false;
      if (banner) banner.hidden = true;
      sendJSON("DELETE", url).catch(function () {});
      lastSent = collect();
      setTabState(here, { dirty: false });
      setState("Entwurf verworfen.");
    }

    // Vorhandenen Entwurf anbieten bzw. beim Zurueckwechseln in den Tab direkt fortsetzen.
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : { draft: null }; })
      .then(function (r) {
        var d = r.draft;
        lastSent = collect();
        if (!d) return;
        if (same(d.data, lastSent)) { setState("Entwurf gespeichert · " + d.updated_label.split(", ")[1]); return; }
        if (form.hasAttribute("data-zq-draft-server")) return;  // Serverantwort ist neuer (Pruefung/Vorschlaege)
        if (resumed) {
          apply(d.data);
          lastSent = d.data;
          setTabState(here, { dirty: true });
          setState("Eingaben wiederhergestellt (Stand " + d.updated_label + ").");
          return;
        }
        if (!banner) return;
        holding = true;
        banner.querySelector("[data-zq-draft-time]").textContent = d.updated_label + (d.data._kunde_name ? " (Kunde: " + d.data._kunde_name + ")" : "");
        banner.hidden = false;
        banner.querySelector("[data-zq-draft-resume]").onclick = function () {
          holding = false;
          banner.hidden = true;
          apply(d.data);
          lastSent = d.data;
          setTabState(here, { dirty: true });
          setState("Entwurf fortgesetzt (Stand " + d.updated_label + ").");
        };
        banner.querySelector("[data-zq-draft-discard]").onclick = discard;
      })
      .catch(function () {});

    return { detach: function () { active = false; clearTimeout(timer); } };
  }

  renderTabs();

  // =========================================================================================
  // 2. Seitenfenster (Kundenakte)
  // =========================================================================================
  var panel = document.querySelector("[data-zq-panel-root]");
  var panelBody = panel && panel.querySelector("[data-zq-panel-body]");
  var panelOpener = null, panelUrl = null, panelScroll = 0;

  function panelSrc(url) { return url + (url.indexOf("?") >= 0 ? "&" : "?") + "panel=1"; }

  function closePanel(fromHistory) {
    if (!panel || panel.hidden) return;
    panel.hidden = true;
    panelBody.textContent = "";
    document.documentElement.classList.remove("zq-panel-open");
    panelUrl = null;
    if (!fromHistory && history.state && history.state.zqPanel) history.back();
    // Liste genau dort lassen, wo sie war (Verlauf und Fokus wuerden sonst scrollen).
    var y = panelScroll;
    var restore = function () { window.scrollTo(0, y); };
    restore();
    requestAnimationFrame(restore);
    setTimeout(restore, 60);
    if (panelOpener && document.contains(panelOpener)) {
      try { panelOpener.focus({ preventScroll: true }); } catch (e) { panelOpener.focus(); }
    }
  }

  function openPanel(url, opener) {
    panelOpener = opener || null;
    if (panel.hidden) panelScroll = window.scrollY || document.documentElement.scrollTop || 0;
    panelUrl = url;
    panelBody.textContent = "";
    panelBody.appendChild(el("p", "zq-panel-loading", "Kundenakte wird geladen …"));
    panel.querySelector("[data-zq-panel-full]").href = url;
    panel.querySelector("[data-zq-panel-heading]").textContent = "Kundenakte";
    panel.hidden = false;
    document.documentElement.classList.add("zq-panel-open");
    if (!history.state || !history.state.zqPanel) history.pushState({ zqPanel: url }, "");
    fetch(panelSrc(url), { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (r) {
        if (!r.ok || r.redirected) throw new Error("panel");
        return r.text();
      })
      .then(function (html) {
        if (panelUrl !== url) return;
        panelBody.innerHTML = html;
        var page = panelBody.querySelector("[data-zq-panel-title]");
        if (page && page.getAttribute("data-zq-panel-title")) panel.querySelector("[data-zq-panel-heading]").textContent = page.getAttribute("data-zq-panel-title");
        panelBody.scrollTop = 0;
        panelBody.focus();
      })
      .catch(function () { location.href = url; });
  }

  if (panel) {
    // Neu geladene Seite mit altem Fenster-Eintrag im Verlauf: Eintrag neutralisieren, sonst
    // wuerde "Schliessen" einen Schritt zu weit zurueckgehen.
    if (history.state && history.state.zqPanel) history.replaceState(null, "");
    document.addEventListener("click", function (ev) {
      var link = ev.target.closest && ev.target.closest("a[data-zq-panel]");
      if (!link || ev.defaultPrevented || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
      if (panel.contains(link)) return;  // Links im Fenster selbst normal oeffnen
      ev.preventDefault();
      openPanel(link.getAttribute("href"), link);
    });
    Array.prototype.forEach.call(panel.querySelectorAll("[data-zq-panel-close]"), function (b) {
      b.addEventListener("click", function () { closePanel(false); });
    });
    panel.querySelector("[data-zq-panel-full]").addEventListener("click", function () {
      // Akte als eigene Seite/Tab oeffnen: Zustand der Liste ist ueber Zurueck erreichbar.
      document.documentElement.classList.remove("zq-panel-open");
    });
    window.addEventListener("popstate", function (ev) {
      if (!panel.hidden && !(ev.state && ev.state.zqPanel)) closePanel(true);
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !panel.hidden && !document.querySelector("[data-zq-inline] form")) closePanel(false);
    });
  }

  // =========================================================================================
  // 3. Bearbeiten direkt am Feld (Kundenakte)
  // =========================================================================================
  function fieldText(fields, values) {
    var keys = fields.map(function (f) { return f[0]; });
    if (keys.indexOf("postal_code") >= 0) {
      var line = keys.indexOf("street") >= 0 ? [values.street, values.house_number].filter(Boolean).join(" ") : (values.address || "");
      var place = [values.postal_code, values.city].filter(Boolean).join(" ");
      return [line, place].filter(Boolean).join(", ");
    }
    return values[keys[0]] || "";
  }

  function startEdit(box) {
    if (box.querySelector("form")) return;
    var fields = JSON.parse(box.getAttribute("data-fields"));
    var values = JSON.parse(box.getAttribute("data-values"));
    var dd = box.querySelector("dd");
    var shown = dd.querySelector("[data-zq-value]"), editBtn = dd.querySelector("[data-zq-edit]"), status = dd.querySelector("[data-zq-status]");
    var form = el("form", "zq-field-form");
    form.noValidate = true;
    var inputs = {};
    fields.forEach(function (f) {
      var label = el("label", "zq-field-input" + (f[0] === "house_number" || f[0] === "postal_code" ? " is-short" : ""));
      if (fields.length > 1) label.appendChild(el("span", "zq-field-sub", f[1]));
      var input = el("input", "field-surface");
      input.name = f[0];
      input.value = values[f[0]] || "";
      input.type = box.getAttribute("data-kind") === "email" ? "email" : (box.getAttribute("data-kind") === "tel" ? "tel" : "text");
      if (f[0] === "postal_code") input.inputMode = "numeric";
      if (fields.length === 1) input.setAttribute("aria-label", f[1]);
      inputs[f[0]] = input;
      label.appendChild(input);
      form.appendChild(label);
    });
    var actions = el("span", "zq-field-actions");
    var save = el("button", "btn btn-primary btn-sm", "Speichern");
    save.type = "submit";
    var cancel = el("button", "btn btn-secondary btn-sm", "Abbrechen");
    cancel.type = "button";
    actions.appendChild(save);
    actions.appendChild(cancel);
    form.appendChild(actions);
    shown.hidden = true;
    editBtn.hidden = true;
    status.textContent = "";
    status.className = "zq-field-status";
    dd.insertBefore(form, status);
    form.querySelector("input").focus();

    function end() { form.remove(); shown.hidden = false; editBtn.hidden = false; editBtn.focus(); }
    cancel.addEventListener("click", end);
    form.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { ev.stopPropagation(); end(); } });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var facts = box.closest("[data-zq-version]");
      var body = { version: facts ? facts.getAttribute("data-zq-version") : "" };
      Object.keys(inputs).forEach(function (k) { body[k] = inputs[k].value; });
      save.disabled = true;
      status.textContent = "Wird gespeichert …";
      status.className = "zq-field-status";
      sendJSON("PATCH", box.getAttribute("data-url"), body).then(function (r) {
        var edit = r.card && r.card.edit;
        if (edit) {
          if (facts) facts.setAttribute("data-zq-version", edit.version);
          // Alle Felder der Akte auf den gespeicherten Stand bringen (Server formatiert z. B. Telefonnummern).
          Array.prototype.forEach.call((facts || document).querySelectorAll("[data-zq-inline]"), function (other) {
            other.setAttribute("data-values", JSON.stringify(edit.values));
            other.querySelector("[data-zq-value]").textContent = fieldText(JSON.parse(other.getAttribute("data-fields")), edit.values) || "–";
          });
        }
        end();
        status.textContent = "✓ Gespeichert";
        status.className = "zq-field-status is-ok";
      }, function (err) {
        save.disabled = false;
        status.textContent = err.status === 409 ? err.message + " (Seite neu laden)" : err.message;
        status.className = "zq-field-status is-error";
        Object.keys(inputs).forEach(function (k) { inputs[k].setAttribute("aria-invalid", "true"); });
        form.querySelector("input").focus();
      });
    });
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-zq-edit]");
    if (btn) startEdit(btn.closest("[data-zq-inline]"));
  });

  // =========================================================================================
  // 5. Schnellsuche
  // =========================================================================================
  var search = document.querySelector("[data-zq-search]");
  if (search) {
    var input = search.querySelector("[data-zq-search-input]");
    var list = search.querySelector("[data-zq-search-list]");
    var hits = [], selected = 0, seq = 0, debounce = null, lastFocus = null;

    Array.prototype.forEach.call(document.querySelectorAll("[data-zq-kbd]"), function (k) { k.textContent = isMac ? "⌘ K" : "Strg K"; });

    var render = function (message) {
      list.textContent = "";
      if (message) { list.appendChild(el("li", "zq-search-empty", message)); return; }
      var group = null;
      hits.forEach(function (hit, i) {
        if (hit.group !== group) { group = hit.group; list.appendChild(el("li", "zq-search-group", group)).setAttribute("role", "presentation"); }
        var li = el("li", "zq-search-item" + (i === selected ? " is-selected" : ""));
        li.id = "zq-hit-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", i === selected ? "true" : "false");
        li.appendChild(el("span", "zq-search-label", hit.label));
        if (hit.detail) li.appendChild(el("span", "zq-search-detail", hit.detail));
        li.addEventListener("mousedown", function (ev) { ev.preventDefault(); go(i); });
        list.appendChild(li);
      });
      input.setAttribute("aria-activedescendant", hits.length ? "zq-hit-" + selected : "");
    };

    var query = function () {
      var q = input.value.trim(), mine = ++seq;
      if (!q) { hits = []; render("Kundenname oder Seite eingeben, z. B. „Kalender“ oder „Abrechnung“."); return; }
      fetch(search.getAttribute("data-url") + "?q=" + encodeURIComponent(q), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : { pages: [], customers: [] }; })
        .then(function (d) {
          if (mine !== seq) return;
          hits = d.pages.map(function (p) { return { label: p.label, url: p.url, group: "Seiten" }; })
            .concat(d.customers.map(function (c) { return { label: c.label, detail: c.detail, url: c.url, group: "Kunden" }; }));
          selected = 0;
          render(hits.length ? null : (q.length < 2 ? "Bitte mindestens zwei Zeichen eingeben." : "Keine Treffer für „" + q + "“."));
        })
        .catch(function () { if (mine === seq) render("Suche gerade nicht möglich."); });
    };

    var go = function (i) { var hit = hits[i]; if (hit) { closeSearch(); location.href = hit.url; } };
    var openSearch = function () {
      lastFocus = document.activeElement;
      search.hidden = false;
      document.documentElement.classList.add("zq-search-open");
      input.value = "";
      hits = [];
      render("Kundenname oder Seite eingeben, z. B. „Kalender“ oder „Abrechnung“.");
      setTimeout(function () { input.focus(); }, 0);
    };
    var closeSearch = function () {
      if (search.hidden) return;
      search.hidden = true;
      document.documentElement.classList.remove("zq-search-open");
      if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
    };

    input.addEventListener("input", function () { clearTimeout(debounce); debounce = setTimeout(query, 150); });
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!hits.length) return;
        selected = (selected + (ev.key === "ArrowDown" ? 1 : hits.length - 1)) % hits.length;
        render();
        var cur = document.getElementById("zq-hit-" + selected);
        if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        go(selected);
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        closeSearch();
      }
    });
    Array.prototype.forEach.call(search.querySelectorAll("[data-zq-search-close]"), function (b) { b.addEventListener("click", closeSearch); });
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest && ev.target.closest("[data-zq-search-open]");
      if (btn) { ev.preventDefault(); openSearch(); }
    });
    document.addEventListener("keydown", function (ev) {
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && !ev.shiftKey && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        if (search.hidden) openSearch(); else closeSearch();
      }
    });
  }

  // =========================================================================================
  // Tagesbegruessung
  // =========================================================================================
  var greeting = document.querySelector("[data-zq-greeting]");
  if (greeting) {
    greeting.querySelector("[data-zq-greeting-close]").addEventListener("click", function () { greeting.remove(); });
  }
})();
