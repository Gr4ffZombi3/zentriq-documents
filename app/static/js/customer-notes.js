// Kundensuche, Kundennotizen und offene Rueckrufe auf der ZentriqAI-Startseite.
//
// js/zentriq-ai.js zeigt jede Eingabe im Gespraechsverlauf und ruft ZentriqNotes.intercept(text,
// reply) auf. Der Server entscheidet (app/services/customer_notes.py), ob die Eingabe eine Notiz,
// eine Kundensuche oder eine Frage an das KI-Modell ist; Kunden- und Bueroszuordnung trifft nur
// das Backend. Ergebnisse (Kundenkarte, Bestaetigung, Rueckfrage, "nicht gefunden", Fehler)
// erscheinen in der Antwortblase `reply`.
// Der ausgewaehlte Kunde lebt nur in dieser Seite (kein Speicher ueber Sitzungen/Ansichten), ist
// am Eingabefeld immer sichtbar und wird beim Wechsel des Gespraechs aufgehoben.
// Alle Texte werden per textContent eingesetzt.
(function () {
  var root = document.querySelector("[data-zn]");
  if (!root) return;

  var api = root.dataset.api;
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var $ = function (sel, scope) { return (scope || document).querySelector(sel); };
  var context = $("[data-zn-context]");
  var contextName = $("[data-zn-context-name]");
  var listEl = $("[data-zn-callback-list]", root);
  var countEl = $("[data-zn-callbacks-count]", root);
  var emptyEl = $("[data-zn-callbacks-empty]", root);
  var moreBtn = $("[data-zn-callbacks-more]", root);

  var CALLBACKS_SHOWN = 3;
  var selected = null; // {id, name} - nur sichtbar ausgewaehlt
  var callbacks = [];
  var callbacksTotal = 0;
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
            : (data.error || "Technischer Fehler. Bitte erneut versuchen.");
          // 400/404/409 mit Meldung: fachliche Rueckmeldung; sonst technischer Fehler.
          var technical = resp.status >= 500 || resp.status === 401 || !data.error;
          throw Object.assign(new Error(msg), { status: resp.status, technical: technical });
        }
        return data;
      });
    }, function () {
      throw Object.assign(new Error("Keine Verbindung zum Server. Bitte erneut versuchen."), { technical: true });
    });
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

  // --- Antwortblase --------------------------------------------------------------------------
  // Jede Antwort steht in einer Blase des Gespraechsverlaufs. Folgeaktionen (Auswahl, Erledigt,
  // Nicht erreicht) ersetzen den Inhalt derselben Blase.
  function message(text, tone) {
    var p = el("p", "zn-msg" + (tone ? " is-" + tone : ""));
    if (tone === "error") p.appendChild(el("span", "zn-msg-label", "Technischer Fehler: "));
    p.appendChild(document.createTextNode(text));
    if (tone === "error" || tone === "warn") p.setAttribute("role", "alert");
    return p;
  }

  function fill(reply, nodes) {
    reply.textContent = "";
    reply.classList.add("zn-reply");
    nodes.filter(Boolean).forEach(function (n) { reply.appendChild(n); });
    if (window.ZentriqChat) window.ZentriqChat.scrollDown();
  }

  function showPending(reply, label) {
    var p = el("div", "zc-pending");
    p.appendChild(el("span", "zc-pending-dot"));
    p.appendChild(document.createTextNode((label || "Wird geprüft") + " …"));
    fill(reply, [p]);
  }

  // Folgeaktion in einer Blase: Ladezustand, Doppelklick gesperrt, Fehler in derselben Blase.
  function act(reply, promise, label, then) {
    pending = true;
    var before = Array.prototype.slice.call(reply.childNodes);
    showPending(reply, label);
    return promise.then(function (r) {
      pending = false;
      then(r);
      loadCallbacks();
    }, function (err) {
      pending = false;
      fill(reply, before);
      var old = reply.querySelector(".zn-msg.is-error");
      if (old) old.remove();
      reply.insertBefore(message(err.message, err.technical ? "error" : "warn"), reply.firstChild);
    });
  }

  // --- Auswahl -------------------------------------------------------------------------------
  function select(customer) {
    selected = customer ? { id: customer.id, name: customer.name } : null;
    context.hidden = !selected;
    contextName.textContent = selected ? selected.name : "";
    if (window.ZentriqChat && window.ZentriqChat.setPlaceholder) {
      window.ZentriqChat.setPlaceholder(selected ? "Notiz oder Frage zu " + selected.name + " …" : "");
    }
  }

  $("[data-zn-context-close]").addEventListener("click", function () {
    select(null);
    if (window.ZentriqChat) window.ZentriqChat.focus();
  });

  // --- Kundenkarte (kompakt, Details aufklappbar) ----------------------------------------------
  function source(s) {
    var li = el("li", "zn-source");
    li.appendChild(el("span", "zn-source-text", s.text));
    li.appendChild(el("span", "zn-meta", [s.source, s.author, s.when].filter(Boolean).join(" · ")));
    return li;
  }

  function renderAnswer(answer) {
    var box = el("div", "zn-answer");
    var empty = !answer.sources || !answer.sources.length;
    box.appendChild(el("p", "zn-answer-text" + (empty ? " is-empty" : ""), answer.text));
    if (!empty) {
      var ul = el("ul", "zn-sources");
      answer.sources.forEach(function (s) { ul.appendChild(source(s)); });
      box.appendChild(ul);
    }
    return box;
  }

  // quiet: "Keine Notizen vorhanden" entfaellt, wenn die Antwort darueber das schon sagt.
  function renderCard(card, reply, quiet) {
    var box = el("article", "zn-card");
    var head = el("header", "zn-card-head");
    head.appendChild(el("h3", "zn-card-title", card.name));
    var facts = [card.vn, card.phone, card.place].filter(Boolean).join(" · ");
    if (facts) head.appendChild(el("p", "zn-meta", facts));
    box.appendChild(head);

    var open = card.open_contacts || [];
    open.forEach(function (c) {
      var row = el("div", "zn-contact");
      var text = el("p", "zn-line");
      text.appendChild(el("span", "zn-badge", "Rückruf offen"));
      text.appendChild(document.createTextNode(" " + (c.topic || "Anlass nicht angegeben")));
      text.appendChild(el("span", "zn-meta", " · " + c.status_label + (c.attempts > 1 ? ", " + c.attempts + " Versuche" : "") + " · " + c.by + ", " + c.when));
      row.appendChild(text);
      var actions = el("div", "zn-actions");
      actions.appendChild(button("Nicht erreicht", null, function () {
        act(reply, request("POST", "/kunden/" + card.id + "/nicht-erreicht", { contact_id: c.id, client_ref: newRef() }), "Wird gespeichert", function (r) { saved(reply, r); });
      }));
      actions.appendChild(button("Erledigt", "btn btn-primary btn-sm", function () {
        act(reply, request("POST", "/rueckrufe/" + c.id + "/erledigt", { client_ref: newRef() }), "Wird gespeichert", function (r) { saved(reply, r); });
      }));
      row.appendChild(actions);
      box.appendChild(row);
    });
    if (!open.length) {
      var row = el("div", "zn-contact");
      var latest = card.latest;
      var line = el("p", "zn-line");
      if (latest) {
        line.appendChild(el("span", "zn-label", "Letzte Notiz: "));
        line.appendChild(document.createTextNode(latest.text));
        line.appendChild(el("span", "zn-meta", " · " + latest.author + ", " + latest.when));
      } else if (!quiet) {
        line.appendChild(el("span", "zn-muted-inline", "Keine Notizen vorhanden."));
      }
      if (line.childNodes.length) row.appendChild(line);
      if (latest) {
        var actions = el("div", "zn-actions");
        actions.appendChild(button("Nicht erreicht", null, function () {
          act(reply, request("POST", "/kunden/" + card.id + "/nicht-erreicht", { note_id: latest.id, client_ref: newRef() }), "Wird gespeichert", function (r) { saved(reply, r); });
        }));
        row.appendChild(actions);
      }
      if (row.childNodes.length) box.appendChild(row);
    }

    var details = el("details", "zn-more");
    var notes = card.notes || [];
    details.appendChild(el("summary", null, "Details" + (notes.length ? " · " + notes.length + (card.more_notes ? "+" : "") + (notes.length === 1 ? " Notiz" : " Notizen") : "")));
    details.appendChild(renderLeipziger(card.leipziger || {}));
    if (notes.length) {
      var ul = el("ul", "zn-sources");
      notes.forEach(function (n) {
        ul.appendChild(source({ text: n.text, author: n.author, when: n.when, source: n.event_label || "Notiz" }));
      });
      details.appendChild(ul);
    }
    if (card.customer_url) {
      var link = el("a", "zn-link", "Kundenakte öffnen");
      link.href = card.customer_url;
      details.appendChild(link);
    }
    box.appendChild(details);
    return box;
  }

  function renderLeipziger(l) {
    var wrap = el("div", "zn-leipziger");
    var p = el("p", "zn-line");
    p.appendChild(el("span", "zn-label", "Leipziger Liste: "));
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

  // Gespeichert / erledigt: Bestaetigung erst nach erfolgreicher Antwort des Servers.
  function saved(reply, r) {
    if (r.card) select(r.card);
    var undo = null;
    // Versehentlich erledigt? Direkt in der Bestaetigung wieder oeffnen (Verlauf bleibt erhalten).
    if (r.kind === "closed" && r.contact_id && !r.duplicate) {
      undo = button("Rückgängig", "zn-link", function () {
        act(reply, request("POST", "/rueckrufe/" + r.contact_id + "/wieder-oeffnen", { client_ref: newRef() }), "Wird gespeichert", function (res) { saved(reply, res); });
      });
    }
    fill(reply, [message(r.message, r.duplicate ? null : "ok"), undo, r.card ? renderCard(r.card, reply) : null]);
  }

  // --- Rueckfragen -----------------------------------------------------------------------------
  function candidateLabel(c) {
    return [c.vn, c.place, c.birth ? "geb. " + c.birth : null, c.phone].filter(Boolean).join(" · ") || "keine weiteren Angaben";
  }

  function cancel(reply) {
    return button("Abbrechen", "zn-link", function () { fill(reply, [message("Abgebrochen – es wurde nichts gespeichert.")]); });
  }

  function renderChoice(reply, r, text, tone) {
    var nodes = [message(r.message, tone)];
    var note = r.pending_note;
    if (note) nodes.push(el("p", "zn-meta", "Notiz: „" + note + "“ – wird erst nach der Auswahl gespeichert."));
    var ul = el("ul", "zn-candidates");
    (r.candidates || []).forEach(function (c) {
      var li = el("li");
      var b = button("", "zn-candidate", function () {
        var body = note ? { text: note, customer_id: c.id, client_ref: newRef() } : { text: text, customer_id: c.id, mode: "search" };
        act(reply, request("POST", "/kunden/eingabe", body), note ? "Wird gespeichert" : "Wird geladen", function (res) { show(reply, res, text); });
      });
      b.appendChild(el("strong", null, c.name));
      b.appendChild(el("span", "zn-meta", candidateLabel(c)));
      li.appendChild(b);
      ul.appendChild(li);
    });
    nodes.push(ul);
    if (r.more) nodes.push(el("p", "zn-meta", "Weitere Treffer – bitte genauer suchen (z. B. mit VN-Nummer)."));
    var row = el("div", "zn-actions");
    if (r.create_option) row.appendChild(createButton(reply, r.create, note || text));
    row.appendChild(cancel(reply));
    nodes.push(row);
    fill(reply, nodes);
  }

  // Ausdruecklich "Neuen Kunden anlegen" (nach Rueckfrage). Den Kunden legt nur der Server an,
  // nach erneuter Pruefung auf vorhandene Kunden mit dieser Nummer.
  function createButton(reply, create, text) {
    var label = create ? "Neuen Kunden „" + create.name + "“ anlegen" : "Neuen Kunden anlegen";
    return button(label, "btn btn-secondary btn-sm", function () {
      var body = { text: (create && create.text) || text, mode: "create", create_new: true, client_ref: newRef() };
      if (create && create.name) body.new_name = create.name;
      act(reply, request("POST", "/kunden/eingabe", body), "Wird angelegt", function (r) { show(reply, r, body.text); });
    });
  }

  // Fehlende Angabe fuer eine Neuanlage abfragen (nur diese eine). Kein eigenes <form>: die
  // Blase liegt nicht im Formular, Enter wird direkt behandelt.
  function renderMissing(reply, r, field) {
    var nodes = [message(r.message), el("p", "zn-meta", "Notiz: „" + r.pending_note + "“ – wird mit der neuen Kundenakte gespeichert.")];
    var row = el("div", "zn-actions");
    var inputEl = el("input", "field-surface zn-missing-input");
    inputEl.type = "text";
    inputEl.maxLength = field === "name" ? 120 : 20;
    inputEl.placeholder = field === "name" ? "Vor- und Nachname" : "z. B. 808/123456-E";
    if (r.suggestion) inputEl.value = r.suggestion;
    inputEl.setAttribute("aria-label", field === "name" ? "Name des Kunden" : "Versicherungsnehmernummer");
    row.appendChild(inputEl);
    row.appendChild(button("Anlegen und speichern", "btn btn-primary btn-sm", function () { go(); }));
    row.appendChild(cancel(reply));
    nodes.push(row);
    fill(reply, nodes);
    function go() {
      var value = inputEl.value.trim();
      if (!value || pending) { inputEl.focus(); return; }
      var body = { text: r.pending_note, mode: "create", client_ref: newRef() };
      if (field === "name") body.new_name = value;
      else { body.new_vn = value; body.new_name = r.name; }
      act(reply, request("POST", "/kunden/eingabe", body), "Wird angelegt", function (res) { show(reply, res, r.pending_note); });
    }
    inputEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); go(); }
    });
    setTimeout(function () { inputEl.focus(); }, 0);
  }

  function renderContactChoice(reply, r) {
    var nodes = [message(r.message), el("p", "zn-meta", "Notiz: „" + r.note + "“")];
    var ul = el("ul", "zn-candidates");
    function add(label, sub, contactId) {
      var li = el("li");
      var b = button("", "zn-candidate", function () {
        act(reply, request("POST", "/kunden/eingabe", { text: r.note, customer_id: r.customer.id, contact_id: contactId, mode: "note", client_ref: newRef() }),
          "Wird gespeichert", function (res) { show(reply, res, r.note); });
      });
      b.appendChild(el("strong", null, label));
      if (sub) b.appendChild(el("span", "zn-meta", sub));
      li.appendChild(b);
      ul.appendChild(li);
    }
    (r.contacts || []).forEach(function (c) { add(c.topic || "Anlass nicht angegeben", c.status_label + " – " + c.by + ", " + c.when, c.id); });
    add("Neuer Vorgang", null, "new");
    nodes.push(ul, cancel(reply));
    fill(reply, nodes);
  }

  function renderClarify(reply, r, text) {
    var row = el("div", "zn-actions");
    row.appendChild(button("Als Notiz speichern", "btn btn-primary btn-sm", function () {
      act(reply, request("POST", "/kunden/eingabe", { text: text, customer_id: r.customer.id, mode: "note", client_ref: newRef() }),
        "Wird gespeichert", function (res) { show(reply, res, text); });
    }));
    row.appendChild(button("An die KI senden", null, function () {
      if (window.ZentriqChat) window.ZentriqChat.sendInto(text, reply);
    }));
    fill(reply, [message(r.message), row]);
  }

  // Ergebnis in der Antwortblase anzeigen. Rueckgabe false: Eingabe geht an das KI-Modell.
  function show(reply, r, text) {
    switch (r.kind) {
      case "chat":
        return false;
      case "saved":
      case "closed":
        saved(reply, r);
        return true;
      case "card":
        select(r.card);
        fill(reply, [
          r.created ? message(r.message, "ok") : null,
          r.answer ? renderAnswer(r.answer) : null,
          renderCard(r.card, reply, !!(r.answer && !(r.answer.sources || []).length)),
        ]);
        return true;
      case "choose":
      case "conflict":
        select(null);
        renderChoice(reply, r, text, r.kind === "conflict" ? "warn" : null);
        return true;
      case "choose_contact":
        renderContactChoice(reply, r);
        return true;
      case "clarify":
        renderClarify(reply, r, text);
        return true;
      case "need_name":
      case "need_vn":
        select(null);
        renderMissing(reply, r, r.kind === "need_name" ? "name" : "vn");
        return true;
      case "confirm_create":
        select(null);
        var row = el("div", "zn-actions");
        row.appendChild(createButton(reply, r.create, text));
        row.appendChild(cancel(reply));
        fill(reply, [message(r.message), row]);
        return true;
      case "no_ai":
        fill(reply, [message(r.message, "warn")]);
        return true;
      case "not_found":
        // Suche ohne Treffer: die bisher ausgewaehlte Karte ist nicht das Ergebnis -> Auswahl aufheben.
        if (r.search) select(null);
        var nodes = [message(r.message, "warn")];
        if (r.create) {
          var actions = el("div", "zn-actions");
          actions.appendChild(createButton(reply, r.create, text));
          nodes.push(actions);
        }
        fill(reply, nodes);
        return true;
      default:
        return false;
    }
  }

  // Aufruf aus js/zentriq-ai.js: true = erledigt, false = an das KI-Modell weitergeben.
  // Technischer Fehler: Meldung in der Blase, Promise wird abgelehnt (Eingabe bleibt erhalten).
  function intercept(text, reply) {
    var body = { text: text, client_ref: newRef() };
    if (selected) body.selected_id = selected.id;
    showPending(reply, "Wird geprüft");
    return request("POST", "/kunden/eingabe", body).then(function (r) {
      var handled = show(reply, r, text);
      if (handled) loadCallbacks();
      return handled;
    }, function (err) {
      fill(reply, [message(err.message, err.technical ? "error" : "warn")]);
      throw err;
    });
  }

  // --- Offene Rueckrufe (einklappbar) -------------------------------------------------------------
  function openCustomer(id, label) {
    if (!window.ZentriqChat) return;
    var reply = window.ZentriqChat.addReply(label);
    showPending(reply, "Wird geladen");
    request("GET", "/kunden/" + id).then(function (r) {
      select(r.card);
      fill(reply, [renderCard(r.card, reply)]);
      window.ZentriqChat.focus();
    }, function (err) {
      fill(reply, [message(err.status === 404 ? "Dieser Kunde wurde in Ihrem Büro nicht gefunden." : err.message, err.status === 404 ? "warn" : "error")]);
    });
  }

  function renderCallbacks() {
    listEl.textContent = "";
    var shown = showAll ? callbacks : callbacks.slice(0, CALLBACKS_SHOWN);
    shown.forEach(function (c) {
      var li = el("li", "zn-callback");
      var openBtn = button("", "zn-callback-open", function () { openCustomer(c.customer.id, "Rückruf: " + c.customer.name); });
      openBtn.appendChild(el("strong", null, c.customer.name));
      openBtn.appendChild(el("span", "zn-callback-topic", c.topic || "Anlass nicht angegeben"));
      openBtn.appendChild(el("span", "zn-meta", c.status_label + (c.attempts > 1 ? " · " + c.attempts + " Versuche" : "") + " · " + c.by + ", " + c.when));
      li.appendChild(openBtn);
      li.appendChild(button("Erledigt", "btn btn-secondary btn-sm", function (b) {
        pending = true;
        b.disabled = true;
        request("POST", "/rueckrufe/" + c.id + "/erledigt", { client_ref: newRef() }).then(function (r) {
          pending = false;
          if (window.ZentriqChat) saved(window.ZentriqChat.addReply("Rückruf erledigt: " + c.customer.name), r);
          loadCallbacks();
        }, function (err) {
          pending = false;
          b.disabled = false;
          li.appendChild(message(err.message, err.technical ? "error" : "warn"));
        });
      }));
      listEl.appendChild(li);
    });
    countEl.textContent = String(callbacksTotal);
    countEl.classList.toggle("is-zero", !callbacksTotal);
    emptyEl.hidden = callbacks.length > 0;
    emptyEl.textContent = "Keine offenen Rückrufe.";
    moreBtn.hidden = callbacks.length <= CALLBACKS_SHOWN;
    moreBtn.textContent = showAll ? "Weniger anzeigen"
      : "Alle anzeigen (" + callbacksTotal + ")" + (callbacksTotal > callbacks.length ? " – die " + callbacks.length + " neuesten" : "");
  }
  moreBtn.addEventListener("click", function () { showAll = !showAll; renderCallbacks(); });

  function loadCallbacks() {
    return request("GET", "/rueckrufe").then(function (d) {
      callbacks = d.callbacks || [];
      callbacksTotal = typeof d.total === "number" ? d.total : callbacks.length;
      renderCallbacks();
    }).catch(function () {
      listEl.textContent = "";
      countEl.textContent = "!";
      emptyEl.hidden = false;
      emptyEl.textContent = "Offene Rückrufe konnten gerade nicht geladen werden.";
    });
  }

  // Einstieg aus der globalen Suche (/zentriq?kunde=<id>): Karte dieses Kunden oeffnen. Ob der
  // Kunde zum eigenen Buero gehoert, prueft der Server (sonst 404 -> Meldung, keine Karte).
  function openFromUrl() {
    var params = new URLSearchParams(location.search);
    var id = params.get("kunde");
    if (!id || !/^\d+$/.test(id)) return;
    params.delete("kunde");
    var rest = params.toString();
    history.replaceState(history.state, "", location.pathname + (rest ? "?" + rest : "") + location.hash);
    openCustomer(id, null);
  }

  window.ZentriqNotes = { intercept: intercept, clear: function () { select(null); } };
  loadCallbacks();
  // zentriq-ai.js stellt ZentriqChat erst nach diesem Skript bereit (beide "defer").
  if (document.readyState === "complete") openFromUrl();
  else document.addEventListener("DOMContentLoaded", openFromUrl);
})();
