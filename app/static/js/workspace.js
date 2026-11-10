// Arbeitsplatz fuer Buero-Rollen (components/workspace.html, app/services/workspace.py):
// 1. Tabs: Kundenakten, Kalender und Kfz-Angebote parallel offen halten (gespeichert im Browser).
// 2. Seitenfenster: Kundenakte rechts (Handy: Vollbild mit Zurueck), die Liste bleibt unveraendert.
// 3. Bearbeiten direkt am Feld (Kundenakte) ueber die vorhandene serverseitige Pruefung.
// 4. Formularentwuerfe (Kfz-Angebot): Zwischenstand serverseitig je Benutzer, fortsetzen/verwerfen.
// 5. Schnellsuche (Strg/Cmd+K, Lupen-Knopf): Kunden und Seiten.
// 6. Tagesbegruessung, "Neu"-Markierungen, Listendichte und Handy-Tastatur.
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
    }).catch(function () {
      var err = new Error("Keine Verbindung zum Server. Ihre Eingaben bleiben erhalten – bitte erneut versuchen.");
      err.status = 0;
      throw err;
    }).then(function (r) {
      // Erfolg nur bei echter JSON-Antwort: eine Weiterleitung zur Anmeldung (Sitzung abgelaufen)
      // liefert HTML mit Status 200 und darf nicht als "gespeichert" gelten.
      var isJSON = (r.headers.get("Content-Type") || "").indexOf("json") >= 0;
      if (r.redirected || (r.ok && !isJSON)) {
        var expired = new Error("Ihre Anmeldung ist abgelaufen. Bitte in einem neuen Tab anmelden und dann erneut speichern – die Eingaben bleiben stehen.");
        expired.status = 401;
        throw expired;
      }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var err = new Error(data.error || "Speichern nicht möglich. Bitte erneut versuchen."); err.status = r.status; throw err; }
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
    var loading = el("p", "zq-panel-loading", "Kundenakte wird geladen …");
    loading.setAttribute("role", "status");
    panelBody.appendChild(loading);
    panelBody.setAttribute("aria-busy", "true");
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
        panelBody.removeAttribute("aria-busy");
        panelBody.innerHTML = html;
        var page = panelBody.querySelector("[data-zq-panel-title]");
        if (page && page.getAttribute("data-zq-panel-title")) panel.querySelector("[data-zq-panel-heading]").textContent = page.getAttribute("data-zq-panel-title");
        panelBody.scrollTop = 0;
        panelBody.focus();
      })
      .catch(function () {
        if (panelUrl !== url) return;
        // Fehler im Fenster zeigen (Liste bleibt, wo sie war) - mit erneutem Versuch.
        panelBody.removeAttribute("aria-busy");
        panelBody.textContent = "";
        var box = el("div", "zq-panel-error");
        box.setAttribute("role", "alert");
        box.appendChild(el("p", null, "Die Kundenakte konnte gerade nicht geladen werden."));
        var retry = el("button", "btn btn-primary btn-sm", "Erneut versuchen");
        retry.type = "button";
        retry.addEventListener("click", function () { openPanel(url, panelOpener); });
        var page = el("a", "btn btn-secondary btn-sm", "Als Seite öffnen");
        page.href = url;
        box.appendChild(retry);
        box.appendChild(page);
        panelBody.appendChild(box);
      });
  }

  // Angeklickte Zeile sofort markieren (bleibt markiert, bis eine andere gewaehlt wird).
  function pickRow(link) {
    var row = link && link.closest("tr, li");
    if (!row) return;
    Array.prototype.forEach.call(document.querySelectorAll(".zq-row-picked"), function (r) { r.classList.remove("zq-row-picked"); });
    row.classList.add("zq-row-picked");
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
      pickRow(link);
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
      if (ev.key === "Escape" && !panel.hidden && !document.querySelector("[data-zq-inline] form")) { ev.preventDefault(); closePanel(false); }
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
      Object.keys(inputs).forEach(function (k) { inputs[k].removeAttribute("aria-invalid"); });
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
        status.textContent = "Gespeichert ✓";
        status.className = "zq-field-status is-ok";
        clearTimeout(status._timer);
        status._timer = setTimeout(function () { if (status.textContent === "Gespeichert ✓") status.textContent = ""; }, 4000);
      }, function (err) {
        save.disabled = false;
        save.textContent = err.status === 0 || err.status >= 500 ? "Erneut versuchen" : "Speichern";
        status.textContent = err.status === 409 ? err.message + " (Seite neu laden)" : err.message;
        status.className = "zq-field-status is-error";
        // Nur bei Eingabefehlern die Felder markieren - bei Verbindungsproblemen ist die Eingabe in Ordnung.
        if (err.status === 400 || err.status === 422) Object.keys(inputs).forEach(function (k) { inputs[k].setAttribute("aria-invalid", "true"); });
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
            .concat(d.customers.map(function (c) { return { label: c.label, detail: c.detail, url: c.url, group: "Kunden" }; }))
            .concat((d.cases || []).map(function (c) { return { label: c.label, detail: c.detail, url: c.url, group: "Vorgänge ohne Kundenakte" }; }));
          if (d.create) hits.push({ label: "Vorgang ohne Kundenakte erfassen", detail: "„" + q + "“", url: d.create, group: "Kunde nicht gefunden?" });
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
  // Tagesbegruessung: gleitet herein (Desktop rechts oben, Handy von unten; CSS), nicht modal.
  // =========================================================================================
  var greeting = document.querySelector("[data-zq-greeting]");
  if (greeting) {
    var closeGreeting = function () {
      if (!greeting.parentNode) return;
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      greeting.classList.add("is-leaving");
      setTimeout(function () { greeting.remove(); }, reduce ? 0 : 200);
    };
    Array.prototype.forEach.call(greeting.querySelectorAll("[data-zq-greeting-close]"), function (b) { b.addEventListener("click", closeGreeting); });
    document.addEventListener("keydown", function (ev) {
      var root = document.documentElement;
      if (ev.key !== "Escape" || !greeting.parentNode || ev.defaultPrevented || openTip) return;
      if (root.classList.contains("zq-panel-open") || root.classList.contains("zq-search-open")) return;
      closeGreeting();
    });
    // Tastatur: ohne anderen Fokus auf "Los geht’s" (Enter schliesst), ohne zu scrollen.
    var primary = greeting.querySelector("[data-zq-greeting-primary]");
    if (primary && (!document.activeElement || document.activeElement === document.body)) {
      try { primary.focus({ preventScroll: true }); } catch (e) { primary.focus(); }
    }
  }

  // =========================================================================================
  // "Neu"-Markierung: Antippen erklaert in einem Satz, danach verschwindet sie fuer diesen Benutzer.
  // =========================================================================================
  var openTip = null;
  function closeTip() {
    if (!openTip) return;
    var t = openTip;
    openTip = null;
    t.tip.remove();
    t.badge.setAttribute("aria-expanded", "false");
    if (t.seen) t.badge.remove();
  }
  document.addEventListener("click", function (ev) {
    var badge = ev.target.closest && ev.target.closest("[data-zq-new]");
    if (openTip && badge === openTip.badge) { ev.preventDefault(); closeTip(); return; }
    if (openTip && !openTip.tip.contains(ev.target)) closeTip();
    if (!badge || openTip) return;
    ev.preventDefault();
    var tip = el("span", "zq-new-tip");
    tip.setAttribute("role", "status");
    tip.appendChild(el("span", null, badge.getAttribute("data-hint")));
    var ok = el("button", "zq-new-ok", "Verstanden");
    ok.type = "button";
    ok.addEventListener("click", function () { closeTip(); });
    tip.appendChild(ok);
    document.body.appendChild(tip);
    // Neben der Markierung, innerhalb des Fensters (unten, sonst oben).
    var r = badge.getBoundingClientRect(), tw = tip.offsetWidth, th = tip.offsetHeight;
    var left = Math.max(12, Math.min(r.left, window.innerWidth - tw - 12));
    var top = r.bottom + 8 + th > window.innerHeight ? r.top - th - 8 : r.bottom + 8;
    tip.style.left = left + "px";
    tip.style.top = Math.max(8, top) + "px";
    badge.setAttribute("aria-expanded", "true");
    var entry = { badge: badge, tip: tip, seen: false };
    openTip = entry;
    sendJSON("POST", badge.getAttribute("data-url")).then(function () {
      entry.seen = true;
      // Andere Stellen derselben Neuerung auf dieser Seite ebenfalls ausblenden.
      Array.prototype.forEach.call(document.querySelectorAll('[data-zq-new="' + badge.getAttribute("data-zq-new") + '"]'), function (b) { if (b !== badge) b.remove(); });
      if (openTip !== entry) badge.remove();
    }, function () { /* nicht gemerkt: Markierung bleibt und erscheint beim naechsten Mal wieder */ });
    ok.focus();
  });
  window.addEventListener("scroll", closeTip, true);
  window.addEventListener("resize", closeTip);
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && openTip) { var b = openTip.badge; closeTip(); ev.preventDefault(); if (document.contains(b)) b.focus(); }
  });

  // =========================================================================================
  // Listen: Kompakt / Geraeumig (je Benutzer, serverseitig gespeichert)
  // =========================================================================================
  document.addEventListener("click", function (ev) {
    var opt = ev.target.closest && ev.target.closest("[data-zq-density] .zq-density-opt");
    if (!opt) return;
    var box = opt.closest("[data-zq-density]"), value = opt.getAttribute("data-value");
    var status = box.querySelector("[data-zq-density-status]");
    var body = document.body, before = body.classList.contains("density-kompakt") ? "kompakt" : "geraeumig";
    if (value === before) return;
    var setUi = function (v) {
      body.classList.remove("density-kompakt", "density-geraeumig");
      body.classList.add("density-" + v);
      Array.prototype.forEach.call(document.querySelectorAll("[data-zq-density] .zq-density-opt"), function (b) {
        var on = b.getAttribute("data-value") === v;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    };
    setUi(value);  // sofort sichtbar; bei Fehler zurueck
    status.className = "zq-density-status";
    status.textContent = "";
    sendJSON("POST", box.getAttribute("data-url"), { density: value }).then(function () {
      status.textContent = "Gespeichert ✓";
      status.classList.add("is-ok");
      clearTimeout(status._timer);
      status._timer = setTimeout(function () { status.textContent = ""; }, 2500);
    }, function (err) {
      setUi(before);
      status.textContent = err.status === 0 ? "Nicht gespeichert – keine Verbindung. Bitte erneut tippen." : err.message;
      status.classList.add("is-error");
    });
  });

  // =========================================================================================
  // Handy-Tastatur: Fokussiertes Feld (und seine Knoepfe) im sichtbaren Bereich halten.
  // =========================================================================================
  var vv = window.visualViewport;
  if (vv) {
    var syncViewport = function () { document.documentElement.style.setProperty("--zq-vvh", vv.height + "px"); };
    vv.addEventListener("resize", syncViewport);
    syncViewport();
    document.addEventListener("focusin", function (ev) {
      var t = ev.target;
      if (!t.matches || !t.matches("input:not([type=checkbox]):not([type=radio]), textarea, select")) return;
      setTimeout(function () {
        var r = t.getBoundingClientRect();
        if (r.bottom > vv.height - 8 || r.top < 0) t.scrollIntoView({ block: "center", behavior: "auto" });
      }, 300);  // nach dem Aufklappen der Tastatur
    });
  }
})();
