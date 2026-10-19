// ZentriqAI-Chat: Unterhaltungen (Liste, Suche, Umbenennen, Loeschen), Modellwahl,
// Kundendaten-Modus, Senden, Verbrauchsanzeige. Alle Inhalte werden per textContent
// eingesetzt - nie als HTML. Im Browser wird nichts dauerhaft gespeichert.
(function () {
  var root = document.querySelector("[data-zai]");
  if (!root) return;

  var api = root.dataset.api;
  var maxChars = parseInt(root.dataset.maxChars, 10) || 12000;
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var el = function (sel) { return root.querySelector(sel); };
  var list = el("[data-zai-list]");
  var listEmpty = el("[data-zai-list-empty]");
  var search = el("[data-zai-search]");
  var messagesBox = el("[data-zai-messages]");
  var welcome = el("[data-zai-welcome]");
  var form = el("[data-zai-form]");
  var input = el("[data-zai-input]");
  var sendBtn = el("[data-zai-send]");
  var statusEl = el("[data-zai-status]");
  var errorEl = el("[data-zai-error]");
  var modelSelect = el("[data-zai-model]");
  var customerBox = el("[data-zai-customer]");
  var titleEl = el("[data-zai-title]");
  var renameBtn = el("[data-zai-rename]");
  var deleteBtn = el("[data-zai-delete]");
  var usageBox = el("[data-zai-usage]");

  var current = null; // aktuelle Unterhaltung
  var busy = false;
  var searchTimer = null;

  function request(method, path, body) {
    return fetch(api + path, {
      method: method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRFToken": csrfMeta ? csrfMeta.getAttribute("content") : "",
      },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var err = new Error(resp.status === 401
            ? "Die Anmeldung ist abgelaufen. Bitte die Seite neu laden."
            : (data.error || "Anfrage fehlgeschlagen. Bitte erneut versuchen."));
          err.data = data;
          throw err;
        }
        return data;
      });
    }, function () {
      throw new Error("Keine Verbindung zum Server. Bitte erneut versuchen.");
    });
  }

  function showError(message, details) {
    errorEl.textContent = "";
    if (message) {
      errorEl.appendChild(document.createTextNode(message));
      if (details && details.length) {
        var small = document.createElement("span");
        small.className = "zai-attempts";
        small.textContent = "Versuche: " + details.join(" · ");
        errorEl.appendChild(small);
      }
    }
    errorEl.hidden = !message;
  }

  function formatCost(value) {
    if (value === null || value === undefined) return "";
    return value === 0 ? "kostenlos" : value.toFixed(4) + " USD";
  }

  function renderMessage(m) {
    welcome.hidden = true;
    var item = document.createElement("article");
    item.className = "zai-msg zai-msg-" + m.role;
    var body = document.createElement("div");
    body.className = "zai-msg-body";
    body.textContent = m.content;
    item.appendChild(body);
    if (m.role === "assistant") {
      var meta = document.createElement("div");
      meta.className = "zai-msg-meta";
      var parts = [m.model || ""];
      if (m.cached) parts.push("aus Cache");
      else if (m.input_tokens !== null) parts.push((m.input_tokens + m.output_tokens) + " Tokens", formatCost(m.cost_usd));
      meta.textContent = parts.filter(Boolean).join(" · ");
      item.appendChild(meta);
      var copy = document.createElement("button");
      copy.type = "button";
      copy.className = "zai-copy";
      copy.textContent = "Kopieren";
      copy.addEventListener("click", function () {
        if (navigator.clipboard) navigator.clipboard.writeText(m.content).then(function () {
          copy.textContent = "Kopiert";
          setTimeout(function () { copy.textContent = "Kopieren"; }, 1500);
        });
      });
      meta.appendChild(copy);
    }
    messagesBox.appendChild(item);
    return item;
  }

  function clearMessages() {
    Array.prototype.slice.call(messagesBox.querySelectorAll(".zai-msg, .zai-note")).forEach(function (n) { n.remove(); });
    welcome.hidden = false;
  }

  function note(text) {
    var n = document.createElement("p");
    n.className = "zai-note";
    n.textContent = text;
    messagesBox.appendChild(n);
  }

  function scrollDown() { messagesBox.scrollTop = messagesBox.scrollHeight; }

  function setCurrent(conversation) {
    current = conversation;
    titleEl.textContent = conversation ? conversation.title : "ZentriqAI";
    renameBtn.hidden = deleteBtn.hidden = !conversation;
    customerBox.checked = !!(conversation && conversation.customer_data);
    if (conversation) modelSelect.value = conversation.model_choice;
    if (modelSelect.value === "" || modelSelect.selectedIndex < 0) modelSelect.value = "auto";
    Array.prototype.forEach.call(list.children, function (li) {
      li.classList.toggle("is-active", conversation && li.dataset.id === String(conversation.id));
    });
  }

  function loadModels() {
    return request("GET", "/models").then(function (data) {
      (data.models || []).forEach(function (m) {
        var opt = document.createElement("option");
        opt.value = m.ref;
        opt.textContent = m.label + " – " + m.provider + (m.free ? " (kostenlos)" : "") + (m.paused ? " · pausiert" : "");
        modelSelect.appendChild(opt);
      });
    }).catch(function (err) { showError(err.message); });
  }

  function loadList() {
    var q = search.value.trim();
    return request("GET", "/conversations" + (q ? "?q=" + encodeURIComponent(q) : "")).then(function (data) {
      list.textContent = "";
      (data.conversations || []).forEach(function (c) {
        var li = document.createElement("li");
        li.dataset.id = c.id;
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "zai-item";
        btn.textContent = c.title;
        btn.addEventListener("click", function () { openConversation(c.id); closeSideOnMobile(); });
        li.appendChild(btn);
        if (current && current.id === c.id) li.classList.add("is-active");
        list.appendChild(li);
      });
      listEmpty.hidden = list.children.length > 0;
    }).catch(function (err) { showError(err.message); });
  }

  function openConversation(id) {
    showError("");
    return request("GET", "/conversations/" + id).then(function (data) {
      clearMessages();
      setCurrent(data.conversation);
      data.messages.forEach(renderMessage);
      scrollDown();
    }).catch(function (err) { showError(err.message); });
  }

  function newConversation() {
    setCurrent(null);
    clearMessages();
    showError("");
    input.focus();
  }

  function ensureConversation() {
    if (current) return Promise.resolve(current);
    return request("POST", "/conversations", { model: modelSelect.value, customer_data: customerBox.checked })
      .then(function (data) { setCurrent(data.conversation); return data.conversation; });
  }

  function setBusy(state, text) {
    busy = state;
    sendBtn.disabled = state;
    input.readOnly = state;
    statusEl.textContent = state ? text : "";
  }

  function send() {
    var text = input.value.trim();
    if (!text || busy) return;
    if (text.length > maxChars) { showError("Die Nachricht ist zu lang (maximal " + maxChars + " Zeichen)."); return; }
    showError("");
    setBusy(true, "ZentriqAI antwortet …");
    var pending;
    ensureConversation().then(function (conversation) {
      pending = renderMessage({ role: "user", content: text });
      scrollDown();
      input.value = "";
      autosize();
      return request("POST", "/conversations/" + conversation.id + "/messages", { content: text, model: modelSelect.value });
    }).then(function (data) {
      if (data.fallback_used) note("Das gewählte Modell war nicht verfügbar – beantwortet von " + data.message.model + ".");
      renderMessage(data.message);
      setCurrent(data.conversation);
      scrollDown();
      loadList();
    }).catch(function (err) {
      // Text zurueck ins Eingabefeld, damit er erneut gesendet werden kann.
      if (pending) pending.classList.add("is-failed");
      if (!input.value) input.value = text;
      showError(err.message, err.data && err.data.attempts);
      loadList();
    }).finally(function () { setBusy(false); input.focus(); });
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 240) + "px";
  }

  function closeSideOnMobile() { root.classList.remove("side-open"); }

  // --- Ereignisse ---
  form.addEventListener("submit", function (e) { e.preventDefault(); send(); });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  el("[data-zai-new]").addEventListener("click", function () { newConversation(); closeSideOnMobile(); });
  el("[data-zai-side-toggle]").addEventListener("click", function () { root.classList.toggle("side-open"); });
  search.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadList, 250);
  });

  modelSelect.addEventListener("change", function () {
    if (current) request("PATCH", "/conversations/" + current.id, { model: modelSelect.value }).then(function (d) { current = d.conversation; }).catch(function (err) { showError(err.message); });
  });
  customerBox.addEventListener("change", function () {
    if (current) request("PATCH", "/conversations/" + current.id, { customer_data: customerBox.checked }).then(function (d) { current = d.conversation; }).catch(function (err) { showError(err.message); });
  });
  renameBtn.addEventListener("click", function () {
    if (!current) return;
    var title = window.prompt("Neuer Titel:", current.title);
    if (title === null || !title.trim()) return;
    request("PATCH", "/conversations/" + current.id, { title: title }).then(function (d) { setCurrent(d.conversation); loadList(); }).catch(function (err) { showError(err.message); });
  });
  deleteBtn.addEventListener("click", function () {
    if (!current || !window.confirm("Diese Unterhaltung endgültig löschen?")) return;
    request("DELETE", "/conversations/" + current.id).then(function () { newConversation(); loadList(); }).catch(function (err) { showError(err.message); });
  });

  el("[data-zai-usage-toggle]").addEventListener("click", function () {
    if (!usageBox.hidden) { usageBox.hidden = true; return; }
    request("GET", "/usage").then(function (data) {
      var b = data.budget;
      usageBox.textContent = "";
      var head = document.createElement("p");
      head.textContent = "Kostenpflichtige Modelle – heute " + b.today.toFixed(4) + " / " + b.daily_limit.toFixed(2) +
        " USD, Monat " + b.month.toFixed(4) + " / " + b.monthly_limit.toFixed(2) + " USD. Eigener Verbrauch (30 Tage):";
      usageBox.appendChild(head);
      var ul = document.createElement("ul");
      (data.own.rows || []).forEach(function (r) {
        var li = document.createElement("li");
        li.textContent = r.model + " (" + r.provider_label + "): " + r.requests + " Anfragen, " +
          (r.input_tokens + r.output_tokens) + " Tokens, " + r.cost_usd.toFixed(4) + " USD" + (r.errors ? ", " + r.errors + " Fehler" : "");
        ul.appendChild(li);
      });
      if (!ul.children.length) { var li = document.createElement("li"); li.textContent = "Noch kein Verbrauch."; ul.appendChild(li); }
      usageBox.appendChild(ul);
      usageBox.hidden = false;
    }).catch(function (err) { showError(err.message); });
  });

  loadModels().then(loadList);
})();
