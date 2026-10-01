// Memo: Upload per Dateiauswahl oder Drag & Drop -> Transkript sofort anzeigen -> Kundenabgleich
// als zweite, nachgelagerte Anfrage (blockiert die Anzeige des Transkripts nicht).
// Alle Daten werden per textContent eingesetzt, nie als HTML.
(function () {
  var root = document.querySelector("[data-memo]");
  if (!root) return;
  var form = root.querySelector("[data-memo-form]");
  var drop = root.querySelector("[data-memo-drop]");
  var input = root.querySelector("[data-memo-input]");
  var label = root.querySelector("[data-memo-label]");
  var errorBox = root.querySelector("[data-memo-error]");
  var uploadBox = root.querySelector("[data-memo-upload]");
  var resultBox = root.querySelector("[data-memo-result]");
  var transcriptBox = root.querySelector("[data-memo-transcript]");
  var metaBox = root.querySelector("[data-memo-meta]");
  var matchBox = root.querySelector("[data-memo-match]");
  var copyMainButton = root.querySelector("[data-memo-copy]");
  var csrf = form.querySelector("input[name=csrf_token]").value;
  var busy = false;
  // Aktuelles Transkript und Token der Transkription (berechtigt zum Zuordnen genau dieses Textes).
  var current = { transcript: "", token: "" };
  var BASIS = {
    phone: "über die Telefonnummer",
    customer_number: "über die Kundennummer",
    name: "nur der Name stimmt überein",
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function button(label, className, onClick) {
    var node = el("button", className || "btn btn-secondary btn-sm", label);
    node.type = "button";
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function setBusy(state, filename) {
    busy = state;
    drop.setAttribute("aria-busy", state ? "true" : "false");
    drop.classList.toggle("is-busy", state);
    label.textContent = state ? "„" + filename + "“ wird transkribiert …" : "Datei hier ablegen";
  }

  function title(text, basis) {
    var node = el("p", "memo-match-title", text + " ");
    if (basis) node.appendChild(el("span", "memo-match-basis", BASIS[basis] || basis));
    return node;
  }

  function fact(list, name, value) {
    var row = el("div");
    row.appendChild(el("dt", null, name));
    row.appendChild(el("dd", null, value));
    list.appendChild(row);
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(body),
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        data._status = resp.status;
        if (!resp.ok && resp.status !== 409) throw new Error(data.error || "Die Aktion ist fehlgeschlagen.");
        return data;
      });
    });
  }

  function copyButton() {
    return button("Text kopieren", "btn btn-secondary btn-sm", function (event) { copyTranscript(event.currentTarget); });
  }

  function customerFacts(customer, extra) {
    matchBox.appendChild(el("p", "memo-match-name", customer.name));
    var list = el("dl", "memo-match-facts");
    // Nur vorhandene Daten anzeigen.
    if (customer.customer_number) fact(list, "Kundennummer:", customer.customer_number);
    if (customer.phone) fact(list, "Telefon:", customer.phone);
    if (list.childNodes.length) matchBox.appendChild(list);
    var actions = el("div", "form-actions");
    if (customer.url) {
      var link = el("a", "btn btn-secondary btn-sm", "Kunde öffnen");
      link.href = customer.url;
      actions.appendChild(link);
    }
    (extra || []).forEach(function (node) { actions.appendChild(node); });
    actions.appendChild(copyButton());
    matchBox.appendChild(actions);
  }

  function assignedNote(text) {
    return el("p", "memo-match-note", text);
  }

  function assign(customer, trigger) {
    trigger.disabled = true;
    postJson(root.dataset.assignUrl, { transcript: current.transcript, token: current.token, customer_id: customer.id })
      .then(function (data) {
        matchBox.textContent = "";
        matchBox.appendChild(title("Memo zugeordnet"));
        customerFacts(data.customer);
      })
      .catch(function (err) { trigger.disabled = false; showMatchError(err.message); });
  }

  function assignButton(customer) {
    return button("Diesem Kunden zuordnen", "btn btn-primary btn-sm", function (event) { assign(customer, event.currentTarget); });
  }

  function candidateList(customers) {
    var ul = el("ul", "memo-candidates");
    customers.forEach(function (item) {
      var li = el("li");
      li.appendChild(el("strong", null, item.name));
      var details = [item.customer_number ? "Kundennr. " + item.customer_number : "", item.phone ? "Tel. " + item.phone : "", item.city || ""].filter(Boolean);
      if (details.length) li.appendChild(el("span", null, details.join(" · ")));
      var actions = el("span", "memo-candidate-actions");
      if (item.url) {
        var a = el("a", "table-action", "Öffnen");
        a.href = item.url;
        actions.appendChild(a);
      }
      if (current.token) actions.appendChild(button("Zuordnen", "btn btn-secondary btn-sm", function (event) { assign(item, event.currentTarget); }));
      li.appendChild(actions);
      ul.appendChild(li);
    });
    return ul;
  }

  function showMatchError(message) {
    var old = matchBox.querySelector(".memo-match-error");
    if (old) old.remove();
    var node = el("p", "form-error memo-match-error", message);
    node.setAttribute("role", "alert");
    matchBox.appendChild(node);
  }

  function field(name, labelText, value, type) {
    var wrap = el("label", "memo-field");
    wrap.appendChild(el("span", "field-label", labelText));
    var input = el("input", "field-surface");
    input.type = type || "text";
    input.name = name;
    input.value = value || "";
    input.maxLength = name === "name" ? 120 : 50;
    wrap.appendChild(input);
    return wrap;
  }

  // "Neuer Kunde erkannt": Werte vorbelegt und editierbar; gespeichert wird nur per Klick.
  function renderSuggestion(suggestion) {
    matchBox.appendChild(title("Neuer Kunde erkannt"));
    matchBox.appendChild(el("p", "form-hint", "Im Kundenbestand wurde niemand mit diesen Angaben gefunden. Bitte prüfen und nur speichern, wenn die Angaben stimmen."));
    var formEl = el("form", "memo-new-customer");
    formEl.appendChild(field("name", "Name", suggestion.name));
    formEl.appendChild(field("phone", "Telefon", suggestion.phone, "tel"));
    formEl.appendChild(field("customer_number", "Kundennummer", suggestion.customer_number));
    var actions = el("div", "form-actions");
    var save = el("button", "btn btn-primary btn-sm", "Als Kunde speichern");
    save.type = "submit";
    actions.appendChild(save);
    actions.appendChild(copyButton());
    formEl.appendChild(actions);
    formEl.addEventListener("submit", function (event) {
      event.preventDefault();
      createCustomer(formEl, save, false);
    });
    matchBox.appendChild(formEl);
  }

  function createCustomer(formEl, save, confirmSameName) {
    save.disabled = true;
    postJson(root.dataset.createUrl, {
      transcript: current.transcript,
      token: current.token,
      name: formEl.elements.name.value,
      phone: formEl.elements.phone.value,
      customer_number: formEl.elements.customer_number.value,
      confirm_same_name: confirmSameName,
    })
      .then(function (data) {
        if (data._status === 409) {
          save.disabled = false;
          renderDuplicates(data, formEl, save);
          return;
        }
        matchBox.textContent = "";
        matchBox.appendChild(title("Kunde angelegt"));
        matchBox.appendChild(assignedNote("Das Memo wurde dem neuen Kunden zugeordnet."));
        customerFacts(data.customer);
      })
      .catch(function (err) { save.disabled = false; showMatchError(err.message); });
  }

  function renderDuplicates(data, formEl, save) {
    var old = matchBox.querySelector(".memo-duplicates");
    if (old) old.remove();
    var box = el("div", "memo-duplicates");
    box.appendChild(el("p", "memo-match-title memo-match-possible",
      data.blocking ? "Kunde mit dieser Telefon- oder Kundennummer bereits vorhanden" : "Kunde mit gleichem Namen bereits vorhanden"));
    box.appendChild(el("p", "form-hint", data.blocking
      ? "Es wird kein neuer Kunde angelegt. Bitte den vorhandenen Kunden zuordnen."
      : "Bitte prüfen, ob es dieselbe Person ist. Zwei Personen können gleich heißen."));
    box.appendChild(candidateList(data.customers));
    if (!data.blocking) {
      box.appendChild(button("Trotzdem als neuen Kunden anlegen", "btn btn-ghost btn-sm", function () { createCustomer(formEl, save, true); }));
    }
    matchBox.appendChild(box);
  }

  // Bewusst ohne Prozentwerte oder Trefferwahrscheinlichkeiten - nur die Grundlage des Treffers.
  function renderMatch(data) {
    matchBox.textContent = "";
    if (data.status === "unique") {
      matchBox.appendChild(title("Kunde erkannt", data.matched_by));
      if (data.assigned) matchBox.appendChild(assignedNote("Das Memo wurde diesem Kunden zugeordnet."));
      customerFacts(data.customers[0], !data.assigned && current.token ? [assignButton(data.customers[0])] : []);
    } else if (data.status === "possible") {
      var possible = el("p", "memo-match-title memo-match-possible", "Möglicher Kunde ");
      possible.appendChild(el("span", "memo-match-basis", BASIS.name + " – bitte prüfen"));
      matchBox.appendChild(possible);
      customerFacts(data.customers[0], current.token ? [assignButton(data.customers[0])] : []);
    } else if (data.status === "multiple") {
      matchBox.appendChild(title("Mehrere mögliche Kunden gefunden", data.matched_by));
      matchBox.appendChild(el("p", "form-hint", "Bitte den passenden Kunden auswählen."));
      matchBox.appendChild(candidateList(data.customers));
    } else if (data.suggestion && current.token) {
      renderSuggestion(data.suggestion);
    } else {
      matchBox.appendChild(title("Kein vorhandener Kunde eindeutig erkannt"));
      if (data.detected_phones && data.detected_phones.length) {
        matchBox.appendChild(el("p", "form-hint", "Erkannte Telefonnummer: " + data.detected_phones.join(", ")));
      }
    }
  }

  function matchCustomer() {
    matchBox.textContent = "";
    matchBox.appendChild(el("p", "memo-match-loading", "Kundenbestand wird geprüft …"));
    postJson(root.dataset.matchUrl, { transcript: current.transcript, token: current.token })
      .then(renderMatch)
      .catch(function () {
        matchBox.textContent = "";
        matchBox.appendChild(el("p", "form-hint", "Der Kundenabgleich ist derzeit nicht möglich. Das Transkript ist davon nicht betroffen."));
      });
  }

  function transcribe(file) {
    if (!file || busy) return;
    showError("");
    setBusy(true, file.name);
    var data = new FormData(form);
    data.set("file", file, file.name);
    fetch(form.action, { method: "POST", body: data, headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (resp) {
        return resp.json().catch(function () { return { error: "Die Transkription ist fehlgeschlagen. Bitte erneut versuchen." }; })
          .then(function (body) { if (!resp.ok) throw new Error(body.error || "Die Transkription ist fehlgeschlagen."); return body; });
      })
      .then(function (body) {
        var uploaded = new Date(body.uploaded_at);
        current = { transcript: body.transcript, token: body.token || "" };
        transcriptBox.textContent = body.transcript;
        metaBox.textContent = body.filename + " · " +
          uploaded.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" }) + ", " +
          uploaded.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) + " Uhr";
        uploadBox.hidden = true;
        resultBox.hidden = false;
        resetSummary();
        // Erst nach der Anzeige des Transkripts.
        matchCustomer();
      })
      .catch(function (err) { showError(err.message); })
      .finally(function () { setBusy(false); input.value = ""; });
  }

  // --- Universal-Upload (nur mit data-detect-url): Dateityp erkennen, anzeigen, bestaetigen ---
  var detectUrl = root.dataset.detectUrl;
  var detectedBox = root.querySelector("[data-intake-detected]");
  var pending = null;

  function extensionOf(name) {
    var dot = (name || "").lastIndexOf(".");
    return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
  }

  function showDetected(label, file, actionLabel, action) {
    detectedBox.querySelector("[data-intake-label]").textContent = label;
    detectedBox.querySelector("[data-intake-file]").textContent = file.name;
    var confirm = detectedBox.querySelector("[data-intake-confirm]");
    confirm.textContent = actionLabel;
    confirm.hidden = !action;
    pending = action;
    drop.hidden = true;
    detectedBox.hidden = false;
  }

  function resetDetected() {
    pending = null;
    detectedBox.hidden = true;
    drop.hidden = false;
  }

  function importList(file) {
    busy = true;
    var data = new FormData();
    data.set("csrf_token", csrf);
    data.set("file", file, file.name);
    var confirm = detectedBox.querySelector("[data-intake-confirm]");
    confirm.disabled = true;
    confirm.textContent = "Wird hochgeladen …";
    fetch(root.dataset.listUploadUrl, {
      method: "POST", body: data, credentials: "same-origin",
      headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (body) {
          if (!resp.ok) throw new Error(body.error || "Der Upload ist fehlgeschlagen.");
          return body;
        });
      })
      .then(function () {
        uploadBox.hidden = true;
        var done = root.querySelector("[data-intake-done]");
        done.querySelector("[data-intake-done-text]").textContent = "„" + file.name + "“ wurde hochgeladen und wird jetzt mit dem bestehenden Leipziger-Import ausgewertet. Das dauert in der Regel nur wenige Sekunden.";
        done.hidden = false;
      })
      .catch(function (err) { resetDetected(); showError(err.message); })
      .finally(function () { busy = false; confirm.disabled = false; input.value = ""; });
  }

  function intake(file) {
    if (!file || busy) return;
    showError("");
    var ext = extensionOf(file.name);
    var audio = (root.dataset.audioExtensions || "").split(",");
    if (audio.indexOf(ext) !== -1) {
      showDetected("Sprachnachricht erkannt", file, "Transkribieren", function () { resetDetected(); transcribe(file); });
      return;
    }
    if (ext !== "pdf") {
      showError("Diese Datei kann Zentriq nicht verarbeiten.");
      input.value = "";
      return;
    }
    busy = true;
    label.textContent = "Datei wird geprüft …";
    var data = new FormData();
    data.set("csrf_token", csrf);
    data.set("file", file, file.name);
    fetch(detectUrl, { method: "POST", body: data, credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        if (!body.allowed || body.kind !== "leipziger" || !root.dataset.listUploadUrl) {
          showError(body.label || body.error || "Diese Datei kann Zentriq nicht verarbeiten.");
          return;
        }
        showDetected(body.label, file, "Importieren", function () { importList(file); });
      })
      .catch(function () { showError("Die Datei konnte nicht geprüft werden. Bitte erneut versuchen."); })
      .finally(function () { busy = false; label.textContent = "Datei hier ablegen"; input.value = ""; });
  }

  function upload(file) {
    if (detectUrl) intake(file);
    else transcribe(file);
  }

  if (detectedBox) {
    detectedBox.querySelector("[data-intake-confirm]").addEventListener("click", function () { if (pending) pending(); });
    detectedBox.querySelector("[data-intake-cancel]").addEventListener("click", function () { resetDetected(); input.value = ""; });
  }

  input.addEventListener("change", function () { upload(input.files[0]); });
  ["dragenter", "dragover"].forEach(function (name) {
    drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.add("is-dragover"); });
  });
  ["dragleave", "dragend", "drop"].forEach(function (name) {
    drop.addEventListener(name, function () { drop.classList.remove("is-dragover"); });
  });
  drop.addEventListener("drop", function (event) {
    event.preventDefault();
    upload(event.dataTransfer && event.dataTransfer.files[0]);
  });

  // --- Fachliche Kurzfassung: nur auf Klick, sendet ausschliesslich das Transkript ---------
  var summary = root.querySelector("[data-memo-summary]");

  function resetSummary() {
    if (!summary) return;
    var text = summary.querySelector("[data-memo-summary-text]");
    text.textContent = "";
    text.hidden = true;
    summary.querySelector("[data-memo-summary-hint]").hidden = false;
    summary.querySelector("[data-memo-summary-copy]").hidden = true;
    var create = summary.querySelector("[data-memo-summary-create]");
    create.disabled = false;
    create.textContent = "Kurzfassung erstellen";
    var error = summary.querySelector("[data-memo-summary-error]");
    error.textContent = "";
    error.hidden = true;
  }

  if (summary) {
    summary.querySelector("[data-memo-summary-create]").addEventListener("click", function (event) {
      var create = event.currentTarget;
      var transcript = transcriptBox.textContent.trim();
      var error = summary.querySelector("[data-memo-summary-error]");
      if (!transcript) return;
      create.disabled = true;
      create.textContent = "Wird erstellt …";
      error.hidden = true;
      postJson(summary.dataset.url, { action: "memo_kurzfassung", text: transcript })
        .then(function (data) {
          var text = summary.querySelector("[data-memo-summary-text]");
          text.textContent = data.result || "";
          text.hidden = false;
          summary.querySelector("[data-memo-summary-hint]").hidden = true;
          summary.querySelector("[data-memo-summary-copy]").hidden = false;
          create.textContent = "Neu erstellen";
        })
        .catch(function (err) {
          error.textContent = err.message;
          error.hidden = false;
          create.textContent = "Kurzfassung erstellen";
        })
        .finally(function () { create.disabled = false; });
    });
  }

  function copyTranscript(trigger) {
    window.Zentriq.copyText(transcriptBox.textContent, trigger);
  }

  copyMainButton.addEventListener("click", function () { copyTranscript(copyMainButton); });
  // Kopieren-Schaltflaeche im serverseitig gerenderten Kundenabgleich (ohne JS-Abgleich).
  matchBox.addEventListener("click", function (event) {
    var button = event.target.closest("[data-memo-copy-again]");
    if (button) copyTranscript(button);
  });

  root.querySelector("[data-memo-reset]").addEventListener("click", function (event) {
    event.preventDefault();
    transcriptBox.textContent = "";
    metaBox.textContent = "";
    matchBox.textContent = "";
    current = { transcript: "", token: "" };
    resetSummary();
    showError("");
    resultBox.hidden = true;
    uploadBox.hidden = false;
  });
})();
