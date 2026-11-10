// ZentriqAI-Chat (Startseite): Unterhaltungen oeffnen/fortsetzen, Modellwahl, Kundendaten-Modus,
// Datei-Anhaenge, asynchrone Antworten (Abfrage des Platzhalters), Markdown mit Codebloecken,
// Kopieren, Speichern/Umbenennen/Projekt/Loeschen.
//
// Sicherheit: Modellantworten werden zuerst vollstaendig HTML-maskiert und erst dann in ein
// kleines, festes Markdown-Subset uebersetzt (keine Roh-HTML-Durchreichung; Links nur
// http(s)/mailto). Benutzertexte werden per textContent eingesetzt.
(function () {
  var root = document.querySelector("[data-zc]");
  if (!root) return;

  var api = root.dataset.api;
  var base = root.dataset.base;
  var maxChars = parseInt(root.dataset.maxChars, 10) || 12000;
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var $ = function (sel) { return root.querySelector(sel); };
  var scroller = $("[data-zc-scroll]");
  var thread = $("[data-zc-thread]");
  var welcome = $("[data-zc-welcome]");
  var form = $("[data-zc-form]");
  var input = $("[data-zc-input]");
  var sendBtn = $("[data-zc-send]");
  var statusEl = $("[data-zc-status]");
  var alertEl = $("[data-zc-alert]");
  var modelButton = $("[data-zc-model-button]");
  var modelMenu = $("[data-zc-model-menu]");
  var modelOptions = $("[data-zc-model-options]");
  var modelLabel = $("[data-zc-model-label]");
  var customerBadge = $("[data-zc-customer-badge]");
  var modelChoice = "auto";
  var models = [];
  var customerBox = $("[data-zc-customer]");
  var titleEl = $("[data-zc-title]");
  var projectEl = $("[data-zc-project]");
  var menu = $("[data-zc-menu]");
  var fileInputs = Array.prototype.slice.call(document.querySelectorAll("[data-zc-file]"));
  var attachMenu = $("[data-zc-attach-menu]");
  var chipsEl = $("[data-zc-attachments]");

  var current = null;
  var busy = false;
  var files = [];
  var stickToBottom = true;
  var pollToken = 0;

  // --- HTTP ------------------------------------------------------------------------------
  function request(method, path, body, isForm) {
    var headers = { Accept: "application/json", "X-CSRFToken": csrfMeta ? csrfMeta.getAttribute("content") : "" };
    if (body && !isForm) headers["Content-Type"] = "application/json";
    return fetch(api + path, {
      method: method, credentials: "same-origin", headers: headers,
      body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var msg = resp.status === 401 ? "Die Anmeldung ist abgelaufen. Bitte die Seite neu laden."
            : resp.status === 413 ? "Die Datei ist zu groß."
            : (data.error || "Anfrage fehlgeschlagen. Bitte erneut versuchen.");
          throw Object.assign(new Error(msg), { data: data, status: resp.status });
        }
        return data;
      });
    }, function () { throw new Error("Keine Verbindung zum Server. Bitte erneut versuchen."); });
  }

  // --- Markdown (sicheres Subset) ------------------------------------------------------
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function inline(text) {
    var codes = [];
    var out = esc(text).replace(/`([^`\n]+)`/g, function (_, c) { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    out = out
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return out.replace(/\u0000(\d+)\u0000/g, function (_, i) { return "<code>" + codes[+i] + "</code>"; });
  }
  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (c) { return c.trim(); });
  }
  function markdown(src) {
    var lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    var html = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
      if (fence) {
        var code = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { code.push(lines[i]); i++; }
        i++;
        html.push('<div class="zc-code' + (/entwurf/i.test(fence[1] || "") ? " zc-code-prose" : "") + '"><div class="zc-code-head"><span>' + esc(fence[1] || "Code") + '</span><button type="button" data-zc-copy-code>Kopieren</button></div><pre><code>' + esc(code.join("\n")) + "</code></pre></div>");
        continue;
      }
      if (/^\s*$/.test(line)) { i++; continue; }
      var h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { html.push("<h" + h[1].length + ">" + inline(h[2]) + "</h" + h[1].length + ">"); i++; continue; }
      if (/^\s*(---|\*\*\*)\s*$/.test(line)) { html.push("<hr>"); i++; continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        var head = splitRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(splitRow(lines[i])); i++; }
        html.push("<table><thead><tr>" + head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") + "</tr></thead><tbody>" +
          rows.map(function (r) { return "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table>");
        continue;
      }
      if (/^\s*>/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { quote.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
        html.push("<blockquote>" + inline(quote.join(" ")) + "</blockquote>");
        continue;
      }
      var listMatch = line.match(/^\s*([-*+]|\d+[.)])\s+/);
      if (listMatch) {
        var ordered = /\d/.test(listMatch[1]);
        var items = [];
        while (i < lines.length) {
          var m = lines[i].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
          if (m && /\d/.test(m[1]) === ordered) { items.push(m[2]); i++; }
          else if (items.length && /^\s{2,}\S/.test(lines[i])) { items[items.length - 1] += " " + lines[i].trim(); i++; }
          else break;
        }
        var tag = ordered ? "ol" : "ul";
        html.push("<" + tag + ">" + items.map(function (t) { return "<li>" + inline(t) + "</li>"; }).join("") + "</" + tag + ">");
        continue;
      }
      var para = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*```/.test(lines[i]) && !/^#{1,4}\s/.test(lines[i]) &&
             !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) && !/^\s*>/.test(lines[i]) && !/^\s*\|.*\|\s*$/.test(lines[i])) {
        para.push(lines[i]); i++;
      }
      if (!para.length) { para.push(lines[i]); i++; }
      html.push("<p>" + para.map(inline).join("<br>") + "</p>");
    }
    return html.join("");
  }

  // --- Darstellung --------------------------------------------------------------------
  function isNearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; }
  scroller.addEventListener("scroll", function () { stickToBottom = isNearBottom(); });
  function scrollDown(force) { if (force || stickToBottom) scroller.scrollTop = scroller.scrollHeight; }

  function copyText(text, button) {
    var shown = function () {
      var old = button.textContent;
      button.textContent = "Kopiert";
      setTimeout(function () { button.textContent = old; }, 1500);
    };
    // Rueckfall ohne Clipboard-API: Kopierbefehl, dessen copy-Ereignis nur text/plain setzt.
    var fallback = function () {
      var put = function (e) { e.clipboardData.setData("text/plain", text); e.preventDefault(); };
      document.addEventListener("copy", put, true);
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.removeEventListener("copy", put, true);
      if (ok) shown();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(shown, fallback);
    else fallback();
  }

  // E-Mail-Entwurf manuell markiert und kopiert (Strg/Cmd+C, Kontextmenue, iPhone): beruehrt die
  // Markierung einen Entwurf, nur den markierten Text als text/plain - sonst uebernimmt Outlook den
  // Hintergrund aus dem HTML. Zeilenumbrueche (Absaetze, Aufzaehlungen) bleiben im <pre> erhalten.
  // Markierungen ohne Entwurf kopiert der Browser wie bisher.
  thread.addEventListener("copy", function (e) {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !e.clipboardData) return;
    var drafts = thread.querySelectorAll(".zc-code-prose code");
    var hit = false;
    for (var r = 0; r < sel.rangeCount && !hit; r++) {
      for (var d = 0; d < drafts.length && !hit; d++) hit = sel.getRangeAt(r).intersectsNode(drafts[d]);
    }
    if (!hit) return;
    e.clipboardData.setData("text/plain", sel.toString());
    e.preventDefault();
  });

  function formatCost(v) { return v === null || v === undefined ? "" : (v === 0 ? "kostenlos" : v.toFixed(4) + " USD"); }

  function setEmpty(state) {
    root.classList.toggle("is-empty", state);
    document.body.classList.toggle("zc-empty", state);
    updateComposerHeight();
  }

  function renderUser(m) {
    setEmpty(false);
    var el = document.createElement("div");
    el.className = "zc-msg zc-msg-user";
    el.dataset.id = m.id || "";
    var text = document.createElement("div");
    text.textContent = m.content;
    el.appendChild(text);
    if (m.attachments && m.attachments.length) {
      var box = document.createElement("div");
      box.className = "zc-files";
      m.attachments.forEach(function (a) {
        var chip = document.createElement("span");
        chip.className = "zc-file";
        chip.textContent = (a.kind === "audio" ? "🎙 " : "📄 ") + a.filename;
        box.appendChild(chip);
      });
      el.appendChild(box);
    }
    thread.appendChild(el);
    return el;
  }

  function fillAssistant(el, m) {
    el.textContent = "";
    el.dataset.id = m.id || "";
    if (m.status === "pending") {
      var p = document.createElement("div");
      p.className = "zc-pending";
      var dot = document.createElement("span");
      dot.className = "zc-pending-dot";
      p.appendChild(dot);
      p.appendChild(document.createTextNode((m.progress || "Analysiere Anfrage") + " …"));
      el.appendChild(p);
      return;
    }
    if (m.status === "error") {
      var err = document.createElement("div");
      err.className = "zc-error";
      err.setAttribute("role", "alert");
      err.textContent = m.error || "Die Antwort konnte nicht erstellt werden.";
      el.appendChild(err);
      // Die gestellte Frage kann mit einem Klick erneut ins Eingabefeld uebernommen werden.
      var asked = el.previousElementSibling;
      if (asked && asked.classList.contains("zc-msg-user")) {
        var retry = document.createElement("button");
        retry.type = "button";
        retry.className = "zc-retry";
        retry.textContent = "Text erneut ins Eingabefeld übernehmen";
        retry.addEventListener("click", function () { restoreInput(asked.firstChild ? asked.firstChild.textContent : "", true); });
        el.appendChild(retry);
      }
      return;
    }
    var body = document.createElement("div");
    body.className = "zc-md";
    body.innerHTML = markdown(m.content);
    el.appendChild(body);
    if (m.error) {
      var note = document.createElement("p");
      note.className = "zc-note";
      note.textContent = m.error;
      el.appendChild(note);
    }
    (m.actions || []).forEach(function (a) { el.appendChild(renderAction(a)); });
    var meta = document.createElement("div");
    meta.className = "zc-msg-meta";
    // Technische Angaben nur dezent (Tooltip), nicht im Antworttext.
    var parts = [m.model || ""];
    if (m.tools && m.tools.length) parts.push("Daten: " + m.tools.join(", "));
    if (m.cached) parts.push("aus Cache");
    else if (m.input_tokens) parts.push((m.input_tokens + m.output_tokens) + " Tokens", formatCost(m.cost_usd));
    var info = document.createElement("span");
    info.className = "zc-info";
    info.textContent = "ⓘ";
    info.title = parts.filter(Boolean).join(" · ");
    info.setAttribute("aria-label", info.title);
    meta.appendChild(info);
    var copy = document.createElement("button");
    copy.type = "button";
    copy.title = "Antwort kopieren";
    copy.textContent = "⧉ Kopieren";
    copy.addEventListener("click", function () { copyText(m.content, copy); });
    meta.appendChild(copy);
    el.appendChild(meta);
  }

  function renderAction(a) {
    var box = document.createElement("div");
    box.className = "zc-action is-" + a.status;
    var text = document.createElement("p");
    text.textContent = a.summary;
    box.appendChild(text);
    var status = document.createElement("p");
    status.className = "zc-action-status";
    if (a.status === "pending") {
      var row = document.createElement("div");
      row.className = "zc-action-buttons";
      [["bestaetigen", "Bestätigen", "btn btn-primary btn-sm"], ["abbrechen", "Abbrechen", "btn btn-secondary btn-sm"]].forEach(function (d) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = d[2];
        b.textContent = d[1];
        b.addEventListener("click", function () {
          Array.prototype.forEach.call(row.querySelectorAll("button"), function (x) { x.disabled = true; });
          request("POST", "/actions/" + a.id + "/" + d[0]).then(function (r) {
            box.replaceWith(renderAction(r.action));
          }).catch(function (err) {
            showAlert(err.message);
            Array.prototype.forEach.call(row.querySelectorAll("button"), function (x) { x.disabled = false; });
          });
        });
        row.appendChild(b);
      });
      box.appendChild(row);
    } else {
      status.textContent = a.status === "done" ? (a.result || "Ausgeführt.") : a.status === "cancelled" ? "Abgebrochen." : (a.result || "Nicht ausgeführt.");
      box.appendChild(status);
    }
    return box;
  }

  function renderAssistant(m) {
    setEmpty(false);
    var el = document.createElement("div");
    el.className = "zc-msg zc-msg-assistant";
    fillAssistant(el, m);
    thread.appendChild(el);
    return el;
  }

  thread.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-zc-copy-code]");
    if (btn) copyText(btn.closest(".zc-code").querySelector("code").textContent, btn);
  });

  // Neuer bzw. anderer Verlauf: kein ausgewaehlter Kunde wird stillschweigend mitgenommen.
  function clearThread() {
    Array.prototype.slice.call(thread.querySelectorAll(".zc-msg")).forEach(function (n) { n.remove(); });
    if (window.ZentriqNotes) window.ZentriqNotes.clear();
    setEmpty(true);
  }

  function showAlert(message) {
    alertEl.textContent = message || "";
    alertEl.hidden = !message;
  }

  // Offener Auftrag (Rueckfrage mit Gedaechtnis, app/services/chat_task.py): Wartezeile + Abbrechen.
  var taskBar = $("[data-zc-task]");
  function setTask(task) {
    if (!taskBar) return;
    taskBar.hidden = !task;
    taskBar.querySelector("[data-zc-task-text]").textContent = task ? task.label : "";
    updateComposerHeight();
  }
  if (taskBar) {
    taskBar.querySelector("[data-zc-task-cancel]").addEventListener("click", function () {
      if (!current) { setTask(null); return; }
      var id = current.id;
      request("POST", "/conversations/" + id + "/auftrag/abbrechen", {}).then(function (d) {
        setTask(null);
        if (d.cancelled && current && current.id === id) {
          var el = renderAssistant({ status: "done", content: "Offener Auftrag abgebrochen – es wurde nichts erstellt." });
          scrollDown(true);
          el.classList.add("zn-reply");
        }
        input.focus();
      }).catch(function (err) { showAlert(err.message); });
    });
  }

  function setCurrent(c) {
    current = c;
    setTask(c ? c.task : null);
    // Titel steht in der Seitenleiste und im Fenstertitel (kein eigener Kopf wie bei Chat-Apps).
    if (titleEl) titleEl.textContent = c ? c.title : "Neue Unterhaltung";
    if (projectEl) {
      projectEl.hidden = !(c && c.project);
      projectEl.textContent = c && c.project ? c.project : "";
    }
    menu.hidden = !c;
    customerBox.checked = !!(c && c.customer_data);
    customerBadge.hidden = !customerBox.checked;
    if (c) {
      setModel(c.model_choice);
      var pinBtn = menu.querySelector('[data-zc-action="pin"]');
      pinBtn.textContent = c.pinned ? "Nicht mehr speichern" : "Speichern";
    }
    document.title = (c ? c.title + " – " : "") + "ZentriqAI";
    if (window.ZentriqShell) window.ZentriqShell.setActive(c ? c.id : null);
  }

  function pushUrl(id) {
    var url = id ? base + "/c/" + id : base;
    if (location.pathname !== url) history.pushState({ id: id || null }, "", url);
  }

  // --- Laden ---------------------------------------------------------------------------
  // --- Modellwahl ------------------------------------------------------------------------
  function option(ref, label, sub) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "zc-option";
    b.setAttribute("role", "option");
    b.dataset.ref = ref;
    var l = document.createElement("span");
    l.className = "zc-option-label";
    l.textContent = label;
    var check = document.createElement("span");
    check.className = "zc-option-check";
    check.textContent = "✓";
    var s2 = document.createElement("span");
    s2.className = "zc-option-sub";
    s2.textContent = sub;
    b.appendChild(l); b.appendChild(check); b.appendChild(s2);
    return b;
  }

  function renderModelOptions() {
    modelOptions.textContent = "";
    if (!models.length) {
      var p = document.createElement("p");
      p.className = "zc-option-empty";
      p.textContent = "Derzeit ist kein KI-Modell eingerichtet oder erreichbar.";
      modelOptions.appendChild(p);
      setModel("auto");
      return;
    }
    modelOptions.appendChild(option("auto", "Automatisch", "Wählt ein verfügbares Modell – kostenlose zuerst, Ausweichen bei Ausfällen"));
    models.forEach(function (m) {
      modelOptions.appendChild(option(m.ref, m.label, m.provider + (m.free ? " · kostenlos" : "") + (m.customer_data ? " · für Kundendaten freigegeben" : "") + (m.paused ? " · gerade pausiert" : "")));
    });
    setModel(modelChoice);
  }

  function setModel(ref) {
    var known = ref === "auto" || models.some(function (m) { return m.ref === ref; });
    modelChoice = known ? ref : "auto";
    var current = models.filter(function (m) { return m.ref === modelChoice; })[0];
    modelLabel.textContent = !models.length ? "kein Modell verfügbar" : (current ? current.label : "Automatisch");
    Array.prototype.forEach.call(modelOptions.querySelectorAll(".zc-option"), function (o) {
      o.setAttribute("aria-selected", String(o.dataset.ref === modelChoice));
    });
  }

  function toggleModelMenu(open) {
    modelMenu.hidden = open === undefined ? !modelMenu.hidden : !open;
    modelButton.setAttribute("aria-expanded", String(!modelMenu.hidden));
  }
  modelButton.addEventListener("click", function () { toggleModelMenu(); });
  document.addEventListener("click", function (e) {
    if (!modelMenu.hidden && !e.target.closest("[data-zc-model-root]")) toggleModelMenu(false);
    if (menu.open && !e.target.closest("[data-zc-menu]")) menu.open = false;
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") toggleModelMenu(false); });
  modelOptions.addEventListener("click", function (e) {
    var o = e.target.closest(".zc-option");
    if (!o) return;
    setModel(o.dataset.ref);
    toggleModelMenu(false);
    if (current) request("PATCH", "/conversations/" + current.id, { model: modelChoice }).then(function (d) { current = d.conversation; }).catch(function (err) { showAlert(err.message); });
  });

  function loadModels() {
    return request("GET", "/models").then(function (data) {
      models = data.models || [];
      renderModelOptions();
    }).catch(function () { renderModelOptions(); });
  }

  function open(id, fromHistory) {
    showAlert("");
    pollToken++;
    return request("GET", "/conversations/" + id).then(function (data) {
      clearThread();
      setCurrent(data.conversation);
      if (!fromHistory) pushUrl(data.conversation.id);
      data.messages.forEach(function (m) {
        if (m.role === "user") renderUser(m);
        else {
          var el = renderAssistant(m);
          if (m.status === "pending") poll(data.conversation.id, m.id, el);
        }
      });
      stickToBottom = true;
      scrollDown(true);
    }).catch(function (err) {
      showAlert(err.message);
      if (err.status === 404) reset();
    });
  }

  function reset(fromHistory) {
    pollToken++;
    setCurrent(null);
    clearThread();
    showAlert("");
    files = [];
    renderChips();
    if (!fromHistory) pushUrl(null);
    input.focus();
  }

  // --- Senden + Abfrage ------------------------------------------------------------------
  function ensureConversation() {
    if (current) return Promise.resolve(current);
    return request("POST", "/conversations", { model: modelChoice, customer_data: customerBox.checked }).then(function (d) {
      setCurrent(d.conversation);
      pushUrl(d.conversation.id);
      return d.conversation;
    });
  }

  function updateSend() {
    sendBtn.disabled = busy || (!input.value.trim() && !files.length);
  }

  function setBusy(state) {
    busy = state;
    updateSend();
    statusEl.textContent = state ? "ZentriqAI arbeitet …" : "";
  }

  function poll(conversationId, messageId, el) {
    var token = pollToken;
    var started = Date.now();
    var delay = 1200;
    setBusy(true);
    function tick() {
      if (token !== pollToken) { setBusy(false); return; }
      request("GET", "/conversations/" + conversationId + "/messages/" + messageId).then(function (d) {
        if (token !== pollToken) return;
        if (d.message.status === "pending" && Date.now() - started < 6 * 60000) {
          fillAssistant(el, d.message);
          delay = Math.min(delay + 400, 3000);
          setTimeout(tick, delay);
          return;
        }
        fillAssistant(el, d.message);
        if (d.conversation) { setCurrent(d.conversation); }
        setBusy(false);
        scrollDown();
        if (window.ZentriqShell) window.ZentriqShell.refreshChats();
      }).catch(function (err) {
        if (token !== pollToken) return;
        if (Date.now() - started < 6 * 60000) { setTimeout(tick, 3000); return; }
        fillAssistant(el, { status: "error", error: err.message });
        setBusy(false);
      });
    }
    setTimeout(tick, delay);
  }

  function newRef() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) {}
    return Date.now().toString(36) + Math.random().toString(36).slice(2);
  }

  // Eingabe nach einem Fehler wiederherstellen (nur, wenn inzwischen nichts Neues getippt wurde).
  function restoreInput(text, force) {
    if (!text || (input.value.trim() && !force)) return;
    input.value = text;
    autosize();
    input.focus();
  }

  // Jede Eingabe erscheint sofort im Verlauf, darunter eine Antwortblase mit Ladezustand.
  // Kundensuche und Notizen (js/customer-notes.js) beantwortet Zentriq direkt; alles andere geht
  // als Frage an das KI-Modell. Waehrend der Bearbeitung ist Senden gesperrt (kein Doppelversand).
  function send(text, skipNotes) {
    text = (text !== undefined ? text : input.value).trim();
    if (busy || (!text && !files.length)) return;
    if (text.length > maxChars) { showAlert("Die Nachricht ist zu lang (maximal " + maxChars + " Zeichen)."); return; }
    if (files.some(isImage) && isApptRequest(text)) { importAppointments(text); return; }
    showAlert("");
    if (!skipNotes && !files.length && window.ZentriqNotes) {
      setBusy(true);
      input.value = "";
      autosize();
      var userEl = renderUser({ content: text });
      var reply = renderAssistant({ status: "pending", progress: "Wird geprüft" });
      stickToBottom = true;
      scrollDown(true);
      window.ZentriqNotes.intercept(text, reply).then(function (handled) {
        if (!handled) { sendChat(text, { user: userEl, reply: reply }); return; }
        setBusy(false);
        scrollDown();
        // Ein in der Antwort geoeffnetes Formularfeld (z. B. "Die Strasse ist falsch") behaelt den Fokus.
        if (!thread.contains(document.activeElement)) input.focus();
      }, function () {
        setBusy(false);
        restoreInput(text);
      });
      return;
    }
    sendChat(text);
  }

  function sendChat(text, slots) {
    setBusy(true);
    var sending = files.slice();
    var ref = newRef();
    if (slots) fillAssistant(slots.reply, { status: "pending", progress: "Frage geht an die KI" });
    ensureConversation().then(function (c) {
      var body;
      var isForm = sending.length > 0;
      var contextCustomer = window.ZentriqNotes && window.ZentriqNotes.takeCustomer ? window.ZentriqNotes.takeCustomer() : null;
      if (isForm) {
        body = new FormData();
        body.append("content", text);
        body.append("model", modelChoice);
        body.append("client_ref", ref);
        sending.forEach(function (f) { body.append("files", f); });
        if (contextCustomer) body.append("customer_id", contextCustomer);
      } else {
        body = { content: text, model: modelChoice, client_ref: ref };
        if (contextCustomer) body.customer_id = contextCustomer;
      }
      return request("POST", "/conversations/" + c.id + "/messages", body, isForm).then(function (d) {
        if (!slots) { input.value = ""; autosize(); }
        files = [];
        renderChips();
        var el;
        if (slots) {
          slots.user.dataset.id = d.message.id || "";
          el = slots.reply;
          el.classList.remove("zn-reply");
          fillAssistant(el, d.reply);
        } else {
          renderUser(d.message);
          el = renderAssistant(d.reply);
        }
        setCurrent(d.conversation);
        stickToBottom = true;
        scrollDown(true);
        if (window.ZentriqShell) window.ZentriqShell.refreshChats();
        if (d.reply.status === "pending") poll(c.id, d.reply.id, el);
        else setBusy(false);
      });
    }).catch(function (err) {
      if (slots) {
        fillAssistant(slots.reply, { status: "error", error: err.message });
        restoreInput(text);
      } else {
        showAlert(err.message);
      }
      setBusy(false);
    });
  }

  // --- Anhaenge ------------------------------------------------------------------------
  function isImage(f) { return /^image\//.test(f.type || "") || /\.(jpe?g|png|webp|heic|heif)$/i.test(f.name || ""); }
  function renderChips() {
    chipsEl.textContent = "";
    if (files.some(isImage)) {
      // Schnellaktion: Termine aus den angehaengten Bildern in den Buerokalender
      var action = document.createElement("button");
      action.type = "button";
      action.className = "zc-chip zc-chip-action";
      action.setAttribute("data-zc-appt-start", "");
      action.textContent = "Termine übernehmen";
      action.addEventListener("click", function () { importAppointments(input.value.trim()); });
      chipsEl.appendChild(action);
    }
    files.forEach(function (f, idx) {
      var chip = document.createElement("span");
      chip.className = "zc-chip";
      chip.appendChild(document.createTextNode(f.name));
      var x = document.createElement("button");
      x.type = "button";
      x.setAttribute("aria-label", "Anhang entfernen");
      x.textContent = "×";
      x.addEventListener("click", function () { files.splice(idx, 1); renderChips(); });
      chip.appendChild(x);
      chipsEl.appendChild(chip);
    });
    updateSend();
    updateComposerHeight();
  }
  // Plus-Button: Auswahlmenue mit getrennten Dateifeldern (Fotos / Dokumente / Audio+Video).
  var attachBtn = $("[data-zc-attach]");
  function closeAttachMenu() { if (attachMenu) { attachMenu.hidden = true; attachBtn.setAttribute("aria-expanded", "false"); } }
  attachBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    if (!attachMenu) { fileInputs[0].click(); return; }
    var open = attachMenu.hidden;
    attachMenu.hidden = !open;
    attachBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", function (e) { if (attachMenu && !attachMenu.hidden && !attachMenu.contains(e.target)) closeAttachMenu(); });
  if (attachMenu) {
    Array.prototype.forEach.call(attachMenu.querySelectorAll("[data-zc-pick]"), function (btn) {
      btn.addEventListener("click", function () {
        closeAttachMenu();
        var target = fileInputs.filter(function (i) { return i.getAttribute("data-zc-file") === btn.getAttribute("data-zc-pick"); })[0];
        if (target) target.click();
      });
    });
  }
  fileInputs.forEach(function (fileInput) {
    fileInput.addEventListener("change", function () {
      var dropped = fileInput.files.length + files.length > 3;
      Array.prototype.forEach.call(fileInput.files, function (f) { if (files.length < 3) files.push(f); });
      if (dropped) showAlert("Höchstens 3 Dateien pro Nachricht.");
      fileInput.value = "";
      renderChips();
    });
  });

  // --- Termine aus Bildern -------------------------------------------------------------
  // Gleiches Muster wie im Backend (image_appointments.PATTERN, ohne \b vor Umlauten).
  var APPT_REQUEST = new RegExp(root.dataset.apptPattern || "termin[a-zäöü]*\\s.*eintrag", "i");
  function isApptRequest(text) { return APPT_REQUEST.test(" " + (text || "") + " "); }
  function node(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function importAppointments(text) {
    var images = files.filter(isImage);
    if (!images.length || busy) return;
    setBusy(true);
    showAlert("");
    text = text || "Termine aus dem Bild eintragen";
    renderUser({ content: text + " (" + images.map(function (f) { return f.name; }).join(", ") + ")" });
    var reply = renderAssistant({ status: "pending", progress: "Lade Bild hoch" });
    input.value = ""; autosize();
    stickToBottom = true; scrollDown(true);
    ensureConversation().then(function (c) {
      var body = new FormData();
      images.forEach(function (f) { body.append("files", f); });
      body.append("conversation_id", c.id);
      body.append("text", text);
      fillAssistant(reply, { status: "pending", progress: "Lese Text im Bild (auf dem Server)" });
      return request("POST", "/termine/aus-bildern", body, true);
    }).then(function (d) {
      // Erst nach erfolgreichem Upload aus dem Eingabefeld nehmen - das Bild liegt jetzt im Chat.
      files = files.filter(function (f) { return images.indexOf(f) < 0; });
      renderChips();
      renderAppointmentPreview(reply, d);
      setBusy(false); scrollDown();
      if (window.ZentriqShell) window.ZentriqShell.refreshChats();
    }, function (err) {
      var code = err.data && err.data.code;
      var msg = (code === "upload_failed" || code === "no_image" || code === "too_many") ? err.message
        : "Upload fehlgeschlagen: " + err.message;
      fillAssistant(reply, { status: "error", error: msg + " Das Bild bleibt angehängt – bitte erneut senden." });
      setBusy(false);  // Anhaenge bleiben im Eingabefeld
    });
  }
  // Befehl ohne neuen Anhang: Bild der vorherigen Nachricht dieses Chats (Backend prueft Zugriff).
  function appointmentsFromChat(reply, attachmentIds) {
    if (!current) { fillAssistant(reply, { status: "error", error: "Bild fehlt: In diesem Chat wurde noch kein Bild hochgeladen. Bitte über „+ › Fotos und Screenshots“ anhängen." }); return; }
    fillAssistant(reply, { status: "pending", progress: "Lese Termine aus dem Bild" });
    request("POST", "/termine/aus-chat", { conversation_id: current.id, attachment_ids: attachmentIds || [] }).then(function (d) {
      if (d.kind === "choose_image") {
        reply.textContent = "";
        var box = node("div", "zc-appt");
        box.appendChild(node("p", "zc-appt-head", d.message));
        var row = node("div", "zc-appt-actions");
        d.options.forEach(function (o) {
          var b = node("button", "btn btn-secondary btn-sm", o.name); b.type = "button";
          b.addEventListener("click", function () { appointmentsFromChat(reply, [o.id]); }); row.appendChild(b);
        });
        var all = node("button", "btn btn-secondary btn-sm", "Alle Bilder"); all.type = "button";
        all.addEventListener("click", function () { appointmentsFromChat(reply, d.options.map(function (o) { return o.id; })); });
        row.appendChild(all); box.appendChild(row); reply.appendChild(box);
        return;
      }
      renderAppointmentPreview(reply, d);
      scrollDown();
    }, function (err) { fillAssistant(reply, { status: "error", error: err.message }); });
  }
  function renderAppointmentPreview(el, d) {
    el.textContent = "";
    var box = node("div", "zc-appt");
    var items = d.items || [];
    var states = (d.image_status || []).map(function (s) { return s.status; });
    var head = items.length
      ? items.length + (items.length === 1 ? " Termin" : " Termine") + " erkannt – bitte prüfen. " + (d.used_model ? "Zuordnung mit KI-Hilfe aus den erkannten Textzeilen." : "Erkannt ohne KI (Texterkennung auf dem Server).")
      : (states.length && states.every(function (s) { return s === "unreadable"; }) ? "Text nicht lesbar." : "Keine Termine erkannt.");
    box.appendChild(node("p", "zc-appt-head", head));
    (d.problems || []).forEach(function (p) { box.appendChild(node("p", "zc-appt-problem", p)); });
    var list = node("ul", "zc-appt-list");
    items.forEach(function (it) {
      var li = node("li", "zc-appt-item is-" + it.state);
      li.dataset.ref = it.ref;
      var check = node("input"); check.type = "checkbox"; check.checked = it.state !== "incomplete"; check.setAttribute("aria-label", "Termin übernehmen");
      check.setAttribute("data-appt-check", "");
      var top = node("label", "zc-appt-top"); top.appendChild(check); top.appendChild(node("span", "zc-appt-title-label", "Übernehmen"));
      li.appendChild(top);
      var grid = node("div", "zc-appt-fields");
      function field(label, input) { var w = node("label", "zc-appt-field"); w.appendChild(node("span", null, label)); w.appendChild(input); grid.appendChild(w); return input; }
      var day = node("input"); day.type = "date"; day.value = it.day || ""; day.setAttribute("data-appt", "day");
      var start = node("input"); start.type = "time"; start.value = it.start || ""; start.setAttribute("data-appt", "start");
      var mins = node("input"); mins.type = "number"; mins.min = "5"; mins.max = "720"; mins.step = "5"; mins.inputMode = "numeric"; mins.value = it.minutes || d.default_minutes || 30; mins.setAttribute("data-appt", "minutes");
      var title = node("input"); title.type = "text"; title.maxLength = 120; title.value = it.title || ""; title.setAttribute("data-appt", "title");
      var who = node("select"); who.setAttribute("data-appt", "assignee_id");
      (d.members || []).forEach(function (m) { var o = node("option", null, m.name); o.value = m.id; if (m.id === it.assignee_id) o.selected = true; who.appendChild(o); });
      field("Datum", day); field("Uhrzeit", start); field("Minuten", mins); field("Titel", title); field("Zuständig", who);
      li.appendChild(grid);
      var notes = (it.problems || []).concat(it.flags || []);
      if (notes.length) li.appendChild(node("p", "zc-appt-flags", "⚠ " + notes.join(" · ")));
      if (it.source) li.appendChild(node("p", "zc-appt-source", "Erkannt: „" + it.source + "“"));
      var result = node("p", "zc-appt-result"); result.setAttribute("aria-live", "polite"); li.appendChild(result);
      [day, start].forEach(function (inp) { inp.addEventListener("change", function () { if (day.value && start.value) check.checked = true; }); });
      list.appendChild(li);
    });
    box.appendChild(list);
    if (items.length) {
      var actions = node("div", "zc-appt-actions");
      var save = node("button", "btn btn-primary btn-sm", "Ausgewählte Termine eintragen");
      save.type = "button"; save.setAttribute("data-appt-save", "");
      var batch = "img" + Date.now().toString(36);
      save.addEventListener("click", function () {
        var rows = Array.prototype.filter.call(list.children, function (li) { return li.querySelector("[data-appt-check]").checked && !li.classList.contains("is-saved"); });
        if (!rows.length) return;
        var payload = rows.map(function (li) {
          var item = { ref: li.dataset.ref };
          Array.prototype.forEach.call(li.querySelectorAll("[data-appt]"), function (inp) { item[inp.getAttribute("data-appt")] = inp.value; });
          return item;
        });
        save.disabled = true; save.textContent = "Wird gespeichert …";
        request("POST", "/termine/uebernehmen", { items: payload, batch: batch }).then(function (r) {
          (r.results || []).forEach(function (res) {
            var li = list.querySelector('[data-ref="' + res.ref + '"]');
            if (!li) return;
            li.querySelector(".zc-appt-result").textContent = (res.status === "saved" ? "✓ " : res.status === "duplicate" ? "= " : "⚠ ") + res.message;
            li.classList.remove("is-ok", "is-warn", "is-incomplete", "is-conflict", "is-duplicate");
            li.classList.add("is-" + (res.status === "saved" ? "saved" : res.status));
            if (res.status === "saved" || res.status === "duplicate") {
              li.querySelector("[data-appt-check]").checked = false;
              Array.prototype.forEach.call(li.querySelectorAll("input,select"), function (inp) { inp.disabled = true; });
            }
          });
          var saved = (r.results || []).filter(function (x) { return x.status === "saved"; }).length;
          summary.textContent = saved + " von " + (r.results || []).length + " ausgewählten Terminen eingetragen.";
          save.disabled = false; save.textContent = "Ausgewählte Termine eintragen";
        }, function (err) {
          save.disabled = false; save.textContent = "Ausgewählte Termine eintragen";
          showAlert(err.message);
        });
      });
      actions.appendChild(save);
      var cal = node("a", "zn-link", "Kalender öffnen"); cal.href = "/kalender"; actions.appendChild(cal);
      box.appendChild(actions);
      var summary = node("p", "zc-appt-summary"); summary.setAttribute("aria-live", "polite"); summary.setAttribute("data-appt-summary", "");
      box.appendChild(summary);
    }
    el.appendChild(box);
  }
  root.addEventListener("dragover", function (e) { e.preventDefault(); });
  root.addEventListener("drop", function (e) {
    e.preventDefault();
    Array.prototype.forEach.call(e.dataTransfer.files || [], function (f) { if (files.length < 3) files.push(f); });
    renderChips();
  });

  // --- Eingabe -------------------------------------------------------------------------
  function autosize() {
    updateSend();
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 224) + "px";
    updateComposerHeight();
  }
  function updateComposerHeight() {
    document.documentElement.style.setProperty("--zs-composer-h", form.offsetHeight + "px");
  }
  // iPhone: die Tastatur verkleinert nur den sichtbaren Bereich (visualViewport), nicht 100dvh.
  // Hoehe des Chats daran koppeln, damit Eingabefeld und Senden ueber der Tastatur bleiben; wer
  // unten war, bleibt unten (wer aeltere Nachrichten liest, springt nicht).
  (function () {
    var vv = window.visualViewport;
    function fit() {
      var wasBottom = stickToBottom;
      if (vv && vv.height < window.innerHeight - 1) {
        document.documentElement.style.setProperty("--zc-vh", Math.round(vv.height) + "px");
        if (window.scrollY) window.scrollTo(0, 0);
      } else {
        document.documentElement.style.removeProperty("--zc-vh");
      }
      updateComposerHeight();
      if (wasBottom) scrollDown(true);
    }
    if (vv) { vv.addEventListener("resize", fit); vv.addEventListener("scroll", fit); }
    window.addEventListener("resize", fit);
  })();
  form.addEventListener("submit", function (e) { e.preventDefault(); send(); });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });

  customerBox.addEventListener("change", function () {
    customerBadge.hidden = !customerBox.checked;
    if (current) request("PATCH", "/conversations/" + current.id, { customer_data: customerBox.checked }).then(function (d) { current = d.conversation; }).catch(function (err) { showAlert(err.message); });
  });

  menu.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-zc-action]");
    if (!btn || !current) return;
    menu.open = false;
    var action = btn.dataset.zcAction;
    var done = function (d) { setCurrent(d.conversation); if (window.ZentriqShell) window.ZentriqShell.refreshChats(); };
    var fail = function (err) { showAlert(err.message); };
    if (action === "pin") request("PATCH", "/conversations/" + current.id, { pinned: !current.pinned }).then(done).catch(fail);
    if (action === "rename") {
      var title = window.prompt("Neuer Titel:", current.title);
      if (title && title.trim()) request("PATCH", "/conversations/" + current.id, { title: title }).then(done).catch(fail);
    }
    if (action === "project") {
      var project = window.prompt("Projekt (leer = keins). Gespräche desselben Projekts teilen ihre Zusammenfassungen als Kontext:", current.project || "");
      if (project !== null) request("PATCH", "/conversations/" + current.id, { project: project }).then(done).catch(fail);
    }
    if (action === "usage") {
      request("GET", "/usage").then(function (d) {
        var rows = (d.own.rows || []).map(function (r) {
          return r.model + ": " + r.requests + " Anfragen, " + (r.input_tokens + r.output_tokens) + " Tokens, " + r.cost_usd.toFixed(4) + " USD";
        });
        window.alert("Eigener Verbrauch (30 Tage):\n" + (rows.join("\n") || "noch keiner"));
      }).catch(fail);
    }
    if (action === "delete" && window.confirm("Diese Unterhaltung endgültig löschen?")) {
      request("DELETE", "/conversations/" + current.id).then(function () {
        reset();
        if (window.ZentriqShell) window.ZentriqShell.refreshChats();
      }).catch(fail);
    }
  });

  window.addEventListener("popstate", function (e) {
    var id = e.state && e.state.id;
    if (id) open(id, true); else reset(true);
  });

  window.ZentriqChat = {
    open: function (id) { open(id); },
    reset: function () { reset(); },
    // Text unveraendert als Frage an das KI-Modell senden (ohne Kundennotiz-Erkennung).
    send: function (text) { input.value = ""; autosize(); send(text, true); },
    // Rueckfrage "An die KI senden": Antwort erscheint in der Blase der Rueckfrage.
    appointmentsFromChat: function (reply) { appointmentsFromChat(reply); },
    sendInto: function (text, reply) {
      if (busy) return;
      var asked = reply.previousElementSibling;
      sendChat(text, { user: asked && asked.classList.contains("zc-msg-user") ? asked : renderUser({ content: text }), reply: reply });
    },
    // Neue Antwortblase ohne Eingabe (z. B. Kunde aus "Offene Rueckrufe" geoeffnet).
    addReply: function (label) {
      if (label) {
        var marker = document.createElement("p");
        marker.className = "zc-msg zc-msg-context";
        marker.textContent = label;
        setEmpty(false);
        thread.appendChild(marker);
      }
      var el = renderAssistant({ status: "pending" });
      stickToBottom = true;
      scrollDown(true);
      return el;
    },
    scrollDown: function () { scrollDown(); },
    // Offener Auftrag: Chat-ID mitsenden, neu angelegten Chat uebernehmen, Wartezeile setzen.
    currentId: function () { return current ? current.id : null; },
    // Chat fuer eine Aktion der Kundenkarte sicherstellen (Entwurf/Buchung bleiben im Verlauf).
    ensure: function () { return ensureConversation(); },
    adopt: function (c) {
      var isNew = !current || current.id !== c.id;
      setCurrent(c);
      if (isNew) { pushUrl(c.id); if (window.ZentriqShell) window.ZentriqShell.refreshChats(); }
    },
    setTask: function (task) { setTask(task); },
    fillReply: function (el, message) { el.classList.remove("zn-reply"); fillAssistant(el, message); },
    focus: function () { input.focus(); },
    setPlaceholder: function (text) { input.placeholder = text || input.dataset.placeholder; },
  };
  input.dataset.placeholder = input.placeholder;

  if (window.ResizeObserver) new ResizeObserver(updateComposerHeight).observe(form);
  updateComposerHeight();
  if (!root.dataset.initial) setEmpty(true);
  updateSend();
  loadModels().then(function () {
    if (root.dataset.initial) open(root.dataset.initial, true);
    else input.focus();
  });
})();
