// Kundennotizen und offene Rueckrufe auf der ZentriqAI-Startseite.
//
// js/zentriq-ai.js ruft vor jeder Chatnachricht ZentriqNotes.intercept(text) auf. Der Server
// entscheidet (app/services/customer_notes.py), ob die Eingabe eine Notiz, eine Kundensuche oder
// eine normale Chatnachricht ist; Kunden- und Bueroszuordnung trifft nur das Backend.
// Der ausgewaehlte Kunde lebt nur in dieser Seite (kein Speicher ueber Sitzungen/Ansichten) und
// ist am Eingabefeld immer sichtbar. Alle Texte werden per textContent eingesetzt.
(function () {
  var root = document.querySelector("[data-zn]");
  if (!root) return;

  var api = root.dataset.api;
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var $ = function (sel, scope) { return (scope || document).querySelector(sel); };
  var statusEl = $("[data-zn-status]", root);
  var resultEl = $("[data-zn-result]", root);
  var context = $("[data-zn-context]");
  var contextName = $("[data-zn-context-name]");
  var listEl = $("[data-zn-callback-list]", root);
  var countEl = $("[data-zn-callbacks-count]", root);
  var emptyEl = $("[data-zn-callbacks-empty]", root);
  var moreBtn = $("[data-zn-callbacks-more]", root);

  var selected = null; // {id, name} - nur sichtbar ausgewaehlt
  var callbacks = [];
  var showAll = false;
  var pending = false;

  function request(method, path, body) {
    var headers = { Accept: "application/json", "X-CSRFToken": csrfMeta ? csrfMeta.getAttribute("content") : "" };
    if (body) headers["Content-Type"] = "application/json";
    return fetch(api + path, {
      method: method, credentials: "same-origin", headers: headers, body: body ? JSON.stringify(body) : undefined,
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var msg = resp.status === 401 ? "Die Anmeldung ist abgelaufen. Bitte die Seite neu laden."
            : resp.status === 404 && !data.error ? "Nicht gefunden."
            : (data.error || "Das hat nicht geklappt. Bitte erneut versuchen.");
          throw Object.assign(new Error(msg), { status: resp.status });
        }
        return data;
      });
    }, function () { throw new Error("Keine Verbindung zum Server. Bitte erneut versuchen."); });
  }

  function newRef() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) {}
    return Date.now().toString(36) + Math.random().toString(36).slice(2);
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function button(label, cls, onClick) {
    var b = el("button", cls || "btn btn-secondary btn-sm", label);
    b.type = "button";
    b.addEventListener("click", function () {
      if (pending) return;
      onClick(b);
    });
    return b;
  }

  // Schnellaktionen: waehrend einer Anfrage sind alle Knoepfe gesperrt (Doppelklick).
  function run(promise, btn) {
    pending = true;
    if (btn) btn.disabled = true;
    return promise.then(function (r) { pending = false; return r; }, function (err) {
      pending = false;
      if (btn) btn.disabled = false;
      setStatus(err.message, "error");
      return new Promise(function () {}); // Fehler ist angezeigt; Folgeschritte entfallen.
    });
  }

  function setStatus(message, tone) {
    statusEl.textContent = message || "";
    statusEl.hidden = !message;
    statusEl.className = "zn-status" + (tone ? " is-" + tone : "");
  }

  // --- Auswahl ---------------------------------------------------------------------------
  function select(customer) {
    selected = customer ? { id: customer.id, name: customer.name } : null;
    context.hidden = !selected;
    contextName.textContent = selected ? selected.name : "";
    if (window.ZentriqChat && window.ZentriqChat.setPlaceholder) {
      window.ZentriqChat.setPlaceholder(selected ? "Notiz für " + selected.name + " …" : "");
    }
  }

  function closeCard() {
    select(null);
    resultEl.hidden = true;
    resultEl.textContent = "";
    setStatus("");
  }
  $("[data-zn-context-close]").addEventListener("click", function () {
    closeCard();
    if (window.ZentriqChat) window.ZentriqChat.focus();
  });

  function showResult(node) {
    resultEl.textContent = "";
    resultEl.appendChild(node);
    resultEl.hidden = false;
  }

  // --- Kundenkarte -----------------------------------------------------------------------
  function line(label, value, cls) {
    var p = el("p", "zn-line" + (cls ? " " + cls : ""));
    if (label) p.appendChild(el("span", "zn-label", label + ": "));
    p.appendChild(document.createTextNode(value));
    return p;
  }

  function source(s) {
    var li = el("li", "zn-source");
    li.appendChild(el("span", "zn-source-text", s.text));
    li.appendChild(el("span", "zn-meta", s.source + " · " + s.author + " · " + s.when));
    return li;
  }

  function renderCard(card, answer) {
    var box = el("article", "zn-card");
    var head = el("header", "zn-card-head");
    var title = el("h3", "zn-card-title", card.name + (card.vn ? " · " + card.vn : ""));
    head.appendChild(title);
    head.appendChild(button("×", "zn-close", closeCard));
    head.lastChild.setAttribute("aria-label", "Kundenkarte schließen");
    box.appendChild(head);
    var facts = [card.phone, card.place].filter(Boolean).join(" · ");
    if (facts) box.appendChild(el("p", "zn-meta", facts));

    if (answer) {
      var ans = el("div", "zn-answer");
      ans.appendChild(el("p", "zn-answer-text", answer.text));
      if (answer.sources && answer.sources.length) {
        var ul = el("ul", "zn-sources");
        answer.sources.forEach(function (s) { ul.appendChild(source(s)); });
        ans.appendChild(ul);
      }
      box.appendChild(ans);
    }

    var open = card.open_contacts || [];
    if (card.latest) {
      box.appendChild(line(null, card.latest.author + " · " + card.latest.when, "zn-meta"));
    }
    var reason = (open.length && open[0].topic) || (card.latest ? card.latest.text : null);
    box.appendChild(line("Anlass", reason || "Keine Notiz dokumentiert"));

    if (open.length) {
      open.forEach(function (c) {
        var row = el("div", "zn-contact");
        var text = el("p", "zn-line");
        text.appendChild(el("span", "zn-label", "Status: "));
        text.appendChild(document.createTextNode(c.status_label + (open.length > 1 && c.topic ? " (" + c.topic + ")" : "")
          + (c.attempts > 1 ? " · " + c.attempts + " Versuche" : "")));
        text.appendChild(el("span", "zn-meta", " – " + c.by + ", " + c.when));
        row.appendChild(text);
        var actions = el("div", "zn-actions");
        actions.appendChild(button("Nicht erreicht", null, function (b) {
          run(request("POST", "/kunden/" + card.id + "/nicht-erreicht", { contact_id: c.id, client_ref: newRef() }), b).then(handleSaved);
        }));
        actions.appendChild(button("Erledigt", "btn btn-primary btn-sm", function (b) {
          run(request("POST", "/rueckrufe/" + c.id + "/erledigt", { client_ref: newRef() }), b).then(handleSaved);
        }));
        row.appendChild(actions);
        box.appendChild(row);
      });
    } else {
      var row = el("div", "zn-contact");
      row.appendChild(line("Status", "Kein offener Vorgang"));
      if (card.latest) {
        var actions = el("div", "zn-actions");
        actions.appendChild(button("Nicht erreicht", null, function (b) {
          run(request("POST", "/kunden/" + card.id + "/nicht-erreicht", { note_id: card.latest.id, client_ref: newRef() }), b).then(handleSaved);
        }));
        row.appendChild(actions);
      }
      box.appendChild(row);
    }

    box.appendChild(renderLeipziger(card.leipziger || {}));

    var notes = card.notes || [];
    if (notes.length) {
      var details = el("details", "zn-history");
      details.appendChild(el("summary", null, "Notizverlauf (" + notes.length + (card.more_notes ? "+" : "") + ")"));
      var ul = el("ul", "zn-sources");
      notes.forEach(function (n) {
        ul.appendChild(source({ text: n.text, author: n.author, when: n.when, source: n.event_label || "Notiz" }));
      });
      details.appendChild(ul);
      box.appendChild(details);
    }
    if (card.customer_url) {
      var link = el("a", "zn-link", "Kundenakte öffnen");
      link.href = card.customer_url;
      box.appendChild(link);
    }
    return box;
  }

  function renderLeipziger(l) {
    var wrap = el("div", "zn-leipziger");
    var label = el("span", "zn-label", "Leipziger Liste: ");
    var p = el("p", "zn-line");
    p.appendChild(label);
    var entries = l.entries || [];
    var text = {
      found: entries.length ? (entries.length === 1 ? "Eintrag vorhanden" : entries.length + " Einträge vorhanden") : "Einträge nur bei anderen Vermittlern",
      none: "Kein zugeordneter Eintrag gefunden",
      no_list: "Noch keine Liste importiert",
      error: "Konnte gerade nicht geladen werden",
    }[l.state] || "Konnte gerade nicht geladen werden";
    p.appendChild(document.createTextNode(text));
    if (l.state === "error") p.classList.add("is-error");
    wrap.appendChild(p);
    if (entries.length) {
      var ul = el("ul", "zn-entries");
      entries.forEach(function (e) {
        var li = el("li");
        var a = el("a", "zn-link", e.number);
        a.href = e.url;
        li.appendChild(a);
        li.appendChild(el("span", "zn-meta", " " + [e.product, e.status].filter(Boolean).join(" · ")));
        ul.appendChild(li);
      });
      wrap.appendChild(ul);
    }
    if (l.hidden) wrap.appendChild(el("p", "zn-meta", "+ " + l.hidden + (l.hidden === 1 ? " Eintrag" : " Einträge") + " anderer Vermittler"));
    return wrap;
  }

  function showCard(card, answer) {
    select(card);
    showResult(renderCard(card, answer));
  }

  function handleSaved(r) {
    setStatus(r.message, r.duplicate ? null : "ok");
    if (r.card) showCard(r.card);
    loadCallbacks();
    return r;
  }

  // --- Rueckfragen -------------------------------------------------------------------------
  function candidateLabel(c) {
    return [c.vn, c.place, c.birth ? "geb. " + c.birth : null, c.phone].filter(Boolean).join(" · ") || "keine weiteren Angaben";
  }

  function renderChoice(r, text, tone) {
    var box = el("div", "zn-choice" + (tone ? " is-" + tone : ""));
    box.appendChild(el("p", "zn-choice-text", r.message));
    var note = r.pending_note;
    if (note) box.appendChild(el("p", "zn-meta", "Notiz: „" + note + "“ – wird erst nach der Auswahl gespeichert."));
    var ul = el("ul", "zn-candidates");
    (r.candidates || []).forEach(function (c) {
      var li = el("li");
      var b = button("", "zn-candidate", function (btn) {
        var body = note ? { text: note, customer_id: c.id, client_ref: newRef() } : { text: text, customer_id: c.id, mode: "search" };
        run(request("POST", "/kunden/eingabe", body), btn).then(handle);
      });
      b.appendChild(el("strong", null, c.name));
      b.appendChild(el("span", "zn-meta", candidateLabel(c)));
      li.appendChild(b);
      ul.appendChild(li);
    });
    box.appendChild(ul);
    if (r.more) box.appendChild(el("p", "zn-meta", "Weitere Treffer – bitte genauer suchen (z. B. mit VN-Nummer)."));
    if (r.create_option) box.appendChild(createButton(r.create, note || text));
    box.appendChild(button("Abbrechen", "zn-link", closeCard));
    return box;
  }

  // Ausdruecklich "Neuen Kunden anlegen" (nach Rueckfrage). Den Kunden legt nur der Server an,
  // nach erneuter Pruefung auf vorhandene Kunden mit dieser Nummer.
  function createButton(create, text) {
    var label = create ? "Neuen Kunden „" + create.name + "“ anlegen" : "Neuen Kunden anlegen";
    return button(label, "btn btn-secondary btn-sm", function (b) {
      var body = { text: (create && create.text) || text, mode: "create", create_new: true, client_ref: newRef() };
      if (create && create.name) body.new_name = create.name;
      run(request("POST", "/kunden/eingabe", body), b).then(function (r) { handle(r, body.text); });
    });
  }

  // Fehlende Angabe fuer eine Neuanlage abfragen (nur diese eine).
  function renderMissing(r, field) {
    // Kein eigenes <form>: die Karte liegt im Formular des Eingabefelds (keine verschachtelten Formulare).
    var box = el("div", "zn-choice");
    box.appendChild(el("p", "zn-choice-text", r.message));
    box.appendChild(el("p", "zn-meta", "Notiz: „" + r.pending_note + "“ – wird mit der neuen Kundenakte gespeichert."));
    var row = el("div", "zn-actions");
    var inputEl = el("input", "field-surface zn-missing-input");
    inputEl.type = "text";
    inputEl.maxLength = field === "name" ? 120 : 20;
    inputEl.placeholder = field === "name" ? "Vor- und Nachname" : "z. B. 808/123456-E";
    inputEl.setAttribute("aria-label", field === "name" ? "Name des Kunden" : "Versicherungsnehmernummer");
    row.appendChild(inputEl);
    var submit = button("Anlegen und speichern", "btn btn-primary btn-sm", function () { go(); });
    row.appendChild(submit);
    box.appendChild(row);
    box.appendChild(button("Abbrechen", "zn-link", closeCard));
    function go() {
      var value = inputEl.value.trim();
      if (!value || pending) { inputEl.focus(); return; }
      var body = { text: r.pending_note, mode: "create", client_ref: newRef() };
      if (field === "name") body.new_name = value;
      else { body.new_vn = value; body.new_name = r.name; }
      run(request("POST", "/kunden/eingabe", body), submit).then(function (res) { handle(res, r.pending_note); });
    }
    inputEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); go(); }
    });
    setTimeout(function () { inputEl.focus(); }, 0);
    return box;
  }

  function renderContactChoice(r) {
    var box = el("div", "zn-choice");
    box.appendChild(el("p", "zn-choice-text", r.message));
    box.appendChild(el("p", "zn-meta", "Notiz: „" + r.note + "“"));
    var ul = el("ul", "zn-candidates");
    function add(label, sub, contactId) {
      var li = el("li");
      var b = button("", "zn-candidate", function (btn) {
        run(request("POST", "/kunden/eingabe", { text: r.note, customer_id: r.customer.id, contact_id: contactId, mode: "note", client_ref: newRef() }), btn).then(handle);
      });
      b.appendChild(el("strong", null, label));
      if (sub) b.appendChild(el("span", "zn-meta", sub));
      li.appendChild(b);
      ul.appendChild(li);
    }
    (r.contacts || []).forEach(function (c) { add(c.topic || "Anlass nicht angegeben", c.status_label + " – " + c.by + ", " + c.when, c.id); });
    add("Neuer Vorgang", null, "new");
    box.appendChild(ul);
    box.appendChild(button("Abbrechen", "zn-link", closeCard));
    return box;
  }

  function renderClarify(r, text) {
    var box = el("div", "zn-choice");
    box.appendChild(el("p", "zn-choice-text", r.message));
    var actions = el("div", "zn-actions");
    actions.appendChild(button("Als Notiz speichern", "btn btn-primary btn-sm", function (b) {
      run(request("POST", "/kunden/eingabe", { text: text, customer_id: r.customer.id, mode: "note", client_ref: newRef() }), b).then(handle);
    }));
    actions.appendChild(button("An ZentriqAI senden", null, function () {
      resultEl.hidden = true;
      if (window.ZentriqChat) window.ZentriqChat.send(text);
    }));
    box.appendChild(actions);
    return box;
  }

  // Ergebnis anzeigen. Rueckgabe: {handled, clear} fuer js/zentriq-ai.js.
  function handle(r, text) {
    switch (r.kind) {
      case "chat":
        return { handled: false };
      case "saved":
        handleSaved(r);
        return { handled: true, clear: true };
      case "closed":
        handleSaved(r);
        return { handled: true, clear: true };
      case "card":
        setStatus(r.created ? r.message : "", r.created ? "ok" : null);
        showCard(r.card, r.answer);
        return { handled: true, clear: true };
      case "choose":
      case "conflict":
        setStatus("");
        select(null);
        showResult(renderChoice(r, text, r.kind === "conflict" ? "warn" : null));
        return { handled: true, clear: true };
      case "choose_contact":
        setStatus("");
        showResult(renderContactChoice(r));
        return { handled: true, clear: true };
      case "clarify":
        setStatus("");
        showResult(renderClarify(r, text));
        return { handled: true, clear: false };
      case "need_name":
        setStatus("");
        select(null);
        showResult(renderMissing(r, "name"));
        return { handled: true, clear: true };
      case "need_vn":
        setStatus("");
        select(null);
        showResult(renderMissing(r, "vn"));
        return { handled: true, clear: true };
      case "not_found":
        setStatus(r.message, "warn");
        if (r.create) {
          var wrap = el("div", "zn-choice");
          wrap.appendChild(el("p", "zn-choice-text", r.message));
          wrap.appendChild(createButton(r.create, text));
          showResult(wrap);
          setStatus("");
        }
        return { handled: true, clear: false };
      default:
        return { handled: false };
    }
  }

  function intercept(text) {
    setStatus("");
    var body = { text: text, client_ref: newRef() };
    if (selected) body.selected_id = selected.id;
    return request("POST", "/kunden/eingabe", body).then(function (r) { return handle(r, text); });
  }

  // --- Offene Rueckrufe ---------------------------------------------------------------------
  function renderCallbacks() {
    listEl.textContent = "";
    var shown = showAll ? callbacks : callbacks.slice(0, 5);
    shown.forEach(function (c) {
      var li = el("li", "zn-callback");
      var open = button("", "zn-callback-open", function (b) {
        run(request("GET", "/kunden/" + c.customer.id), b).then(function (r) {
          setStatus("");
          showCard(r.card);
          if (window.ZentriqChat) window.ZentriqChat.focus();
        });
      });
      open.appendChild(el("strong", null, c.customer.name));
      open.appendChild(el("span", "zn-callback-topic", c.topic || "Anlass nicht angegeben"));
      open.appendChild(el("span", "zn-meta", c.status_label + (c.attempts > 1 ? " · " + c.attempts + " Versuche" : "") + " · " + c.by + ", " + c.when));
      li.appendChild(open);
      li.appendChild(button("Erledigt", "btn btn-secondary btn-sm", function (b) {
        run(request("POST", "/rueckrufe/" + c.id + "/erledigt", { client_ref: newRef() }), b).then(function (r) {
          setStatus(r.message, "ok");
          if (selected && r.card && selected.id === r.card.id) showCard(r.card);
          loadCallbacks();
        });
      }));
      listEl.appendChild(li);
    });
    countEl.textContent = callbacks.length ? "(" + callbacks.length + ")" : "";
    emptyEl.hidden = callbacks.length > 0;
    moreBtn.hidden = callbacks.length <= 5;
    moreBtn.textContent = showAll ? "Weniger anzeigen" : "Alle anzeigen (" + callbacks.length + ")";
  }
  moreBtn.addEventListener("click", function () { showAll = !showAll; renderCallbacks(); });

  function loadCallbacks() {
    return request("GET", "/rueckrufe").then(function (d) {
      callbacks = d.callbacks || [];
      renderCallbacks();
    }).catch(function () {
      listEl.textContent = "";
      emptyEl.hidden = false;
      emptyEl.textContent = "Offene Rückrufe konnten gerade nicht geladen werden.";
    });
  }

  window.ZentriqNotes = { intercept: intercept, close: closeCard };
  loadCallbacks();
})();
