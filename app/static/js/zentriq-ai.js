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
  var modelSelect = $("[data-zc-model]");
  var customerBox = $("[data-zc-customer]");
  var titleEl = $("[data-zc-title]");
  var projectEl = $("[data-zc-project]");
  var menu = $("[data-zc-menu]");
  var fileInput = $("[data-zc-file]");
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
        html.push('<div class="zc-code"><button type="button" class="zc-code-copy" data-zc-copy-code>Kopieren</button><pre><code>' + esc(code.join("\n")) + "</code></pre></div>");
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
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(function () {
      var old = button.textContent;
      button.textContent = "Kopiert";
      setTimeout(function () { button.textContent = old; }, 1500);
    });
  }

  function formatCost(v) { return v === null || v === undefined ? "" : (v === 0 ? "kostenlos" : v.toFixed(4) + " USD"); }

  function renderUser(m) {
    welcome.hidden = true;
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
    el.dataset.id = m.id;
    if (m.status === "pending") {
      var p = document.createElement("div");
      p.className = "zc-pending";
      var dot = document.createElement("span");
      dot.className = "zc-pending-dot";
      p.appendChild(dot);
      p.appendChild(document.createTextNode(m.attachments && m.attachments.length ? "ZentriqAI verarbeitet …" : "ZentriqAI denkt nach …"));
      el.appendChild(p);
      return;
    }
    if (m.status === "error") {
      var err = document.createElement("div");
      err.className = "zc-error";
      err.textContent = m.error || "Die Antwort konnte nicht erstellt werden.";
      el.appendChild(err);
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
    var meta = document.createElement("div");
    meta.className = "zc-msg-meta";
    var parts = [m.model || ""];
    if (m.tools && m.tools.length) parts.push("Daten: " + m.tools.join(", "));
    if (m.cached) parts.push("aus Cache");
    else if (m.input_tokens !== null && m.input_tokens !== undefined) parts.push((m.input_tokens + m.output_tokens) + " Tokens", formatCost(m.cost_usd));
    var info = document.createElement("span");
    info.textContent = parts.filter(Boolean).join(" · ");
    meta.appendChild(info);
    var copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Kopieren";
    copy.addEventListener("click", function () { copyText(m.content, copy); });
    meta.appendChild(copy);
    el.appendChild(meta);
  }

  function renderAssistant(m) {
    welcome.hidden = true;
    var el = document.createElement("div");
    el.className = "zc-msg zc-msg-assistant";
    fillAssistant(el, m);
    thread.appendChild(el);
    return el;
  }

  thread.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-zc-copy-code]");
    if (btn) copyText(btn.parentNode.querySelector("code").textContent, btn);
  });

  function clearThread() {
    Array.prototype.slice.call(thread.querySelectorAll(".zc-msg")).forEach(function (n) { n.remove(); });
    welcome.hidden = false;
  }

  function showAlert(message) {
    alertEl.textContent = message || "";
    alertEl.hidden = !message;
  }

  function setCurrent(c) {
    current = c;
    titleEl.textContent = c ? c.title : "Neue Unterhaltung";
    projectEl.hidden = !(c && c.project);
    projectEl.textContent = c && c.project ? c.project : "";
    menu.hidden = !c;
    customerBox.checked = !!(c && c.customer_data);
    if (c) {
      modelSelect.value = c.model_choice;
      if (modelSelect.selectedIndex < 0) modelSelect.value = "auto";
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
  function loadModels() {
    return request("GET", "/models").then(function (data) {
      var models = data.models || [];
      models.forEach(function (m) {
        var opt = document.createElement("option");
        opt.value = m.ref;
        opt.textContent = m.label + " · " + m.provider + (m.free ? " (kostenlos)" : "") + (m.paused ? " – pausiert" : "");
        modelSelect.appendChild(opt);
      });
      if (!models.length) {
        modelSelect.options[0].textContent = "Kein Modell verfügbar";
        statusEl.textContent = "Derzeit ist kein KI-Modell eingerichtet oder erreichbar.";
      }
    }).catch(function () {});
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
    return request("POST", "/conversations", { model: modelSelect.value, customer_data: customerBox.checked }).then(function (d) {
      setCurrent(d.conversation);
      pushUrl(d.conversation.id);
      return d.conversation;
    });
  }

  function setBusy(state) {
    busy = state;
    sendBtn.disabled = state;
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

  function send(text) {
    text = (text !== undefined ? text : input.value).trim();
    if (busy || (!text && !files.length)) return;
    if (text.length > maxChars) { showAlert("Die Nachricht ist zu lang (maximal " + maxChars + " Zeichen)."); return; }
    showAlert("");
    setBusy(true);
    var sending = files.slice();
    ensureConversation().then(function (c) {
      var body;
      var isForm = sending.length > 0;
      if (isForm) {
        body = new FormData();
        body.append("content", text);
        body.append("model", modelSelect.value);
        sending.forEach(function (f) { body.append("files", f); });
      } else {
        body = { content: text, model: modelSelect.value };
      }
      return request("POST", "/conversations/" + c.id + "/messages", body, isForm).then(function (d) {
        input.value = "";
        files = [];
        renderChips();
        autosize();
        renderUser(d.message);
        var el = renderAssistant(d.reply);
        setCurrent(d.conversation);
        stickToBottom = true;
        scrollDown(true);
        if (window.ZentriqShell) window.ZentriqShell.refreshChats();
        if (d.reply.status === "pending") poll(c.id, d.reply.id, el);
        else setBusy(false);
      });
    }).catch(function (err) {
      showAlert(err.message);
      setBusy(false);
    });
  }

  // --- Anhaenge ------------------------------------------------------------------------
  function renderChips() {
    chipsEl.textContent = "";
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
    updateComposerHeight();
  }
  $("[data-zc-attach]").addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    Array.prototype.forEach.call(fileInput.files, function (f) { if (files.length < 3) files.push(f); });
    if (fileInput.files.length + files.length > 3) showAlert("Höchstens 3 Dateien pro Nachricht.");
    fileInput.value = "";
    renderChips();
  });
  root.addEventListener("dragover", function (e) { e.preventDefault(); });
  root.addEventListener("drop", function (e) {
    e.preventDefault();
    Array.prototype.forEach.call(e.dataTransfer.files || [], function (f) { if (files.length < 3) files.push(f); });
    renderChips();
  });

  // --- Eingabe -------------------------------------------------------------------------
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 224) + "px";
    updateComposerHeight();
  }
  function updateComposerHeight() {
    document.documentElement.style.setProperty("--zs-composer-h", form.offsetHeight + "px");
  }
  form.addEventListener("submit", function (e) { e.preventDefault(); send(); });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  Array.prototype.forEach.call(root.querySelectorAll("[data-zc-suggest]"), function (b) {
    b.addEventListener("click", function () { send(b.textContent); });
  });

  modelSelect.addEventListener("change", function () {
    if (current) request("PATCH", "/conversations/" + current.id, { model: modelSelect.value }).then(function (d) { current = d.conversation; }).catch(function (err) { showAlert(err.message); });
  });
  customerBox.addEventListener("change", function () {
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

  window.ZentriqChat = { open: function (id) { open(id); }, reset: function () { reset(); } };

  if (window.ResizeObserver) new ResizeObserver(updateComposerHeight).observe(form);
  updateComposerHeight();
  loadModels().then(function () {
    if (root.dataset.initial) open(root.dataset.initial, true);
    else input.focus();
  });
})();
