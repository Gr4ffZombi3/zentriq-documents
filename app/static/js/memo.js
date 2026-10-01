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
  var copyButton = root.querySelector("[data-memo-copy]");
  var csrf = form.querySelector("input[name=csrf_token]").value;
  var busy = false;
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
    if (basis) node.appendChild(el("span", "memo-match-basis", BASIS[basis] || ""));
    return node;
  }

  function fact(list, name, value) {
    var row = el("div");
    row.appendChild(el("dt", null, name));
    row.appendChild(el("dd", null, value));
    list.appendChild(row);
  }

  function customerFacts(customer) {
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
    var copy = el("button", "btn btn-secondary btn-sm", "Text kopieren");
    copy.type = "button";
    copy.addEventListener("click", function () { copyTranscript(copy); });
    actions.appendChild(copy);
    matchBox.appendChild(actions);
  }

  // Bewusst ohne Prozentwerte oder Trefferwahrscheinlichkeiten - nur die Grundlage des Treffers.
  function renderMatch(data) {
    matchBox.textContent = "";
    if (data.status === "unique") {
      matchBox.appendChild(title("Kunde erkannt", data.matched_by));
      customerFacts(data.customers[0]);
    } else if (data.status === "possible") {
      var possible = el("p", "memo-match-title memo-match-possible", "Möglicher Kunde ");
      possible.appendChild(el("span", "memo-match-basis", BASIS.name + " – bitte prüfen"));
      matchBox.appendChild(possible);
      customerFacts(data.customers[0]);
    } else if (data.status === "multiple") {
      matchBox.appendChild(title("Mehrere mögliche Kunden gefunden", data.matched_by));
      var ul = el("ul", "memo-candidates");
      data.customers.forEach(function (item) {
        var li = el("li");
        li.appendChild(el("strong", null, item.name));
        var details = [item.customer_number ? "Kundennr. " + item.customer_number : "", item.phone ? "Tel. " + item.phone : "", item.city || ""].filter(Boolean);
        if (details.length) li.appendChild(el("span", null, details.join(" · ")));
        if (item.url) {
          var a = el("a", "table-action", "Öffnen");
          a.href = item.url;
          li.appendChild(a);
        }
        ul.appendChild(li);
      });
      matchBox.appendChild(ul);
    } else {
      matchBox.appendChild(title("Kein vorhandener Kunde eindeutig erkannt"));
      if (data.detected_phones && data.detected_phones.length) {
        matchBox.appendChild(el("p", "form-hint", "Erkannte Telefonnummer: " + data.detected_phones.join(", ")));
      }
    }
  }

  function matchCustomer(transcript) {
    matchBox.textContent = "";
    matchBox.appendChild(el("p", "memo-match-loading", "Kundenbestand wird geprüft …"));
    fetch(root.dataset.matchUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ transcript: transcript }),
    })
      .then(function (resp) {
        return resp.json().then(function (body) { if (!resp.ok) throw new Error(body.error || "Fehler"); return body; });
      })
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
        transcriptBox.textContent = body.transcript;
        metaBox.textContent = body.filename + " · " +
          uploaded.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" }) + ", " +
          uploaded.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) + " Uhr";
        uploadBox.hidden = true;
        resultBox.hidden = false;
        // Erst nach der Anzeige des Transkripts.
        matchCustomer(body.transcript);
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

  function copyTranscript(button) {
    var text = transcriptBox.textContent;
    var done = function () {
      button.textContent = "Kopiert";
      setTimeout(function () { button.textContent = "Text kopieren"; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done);
      return;
    }
    var range = document.createRange();
    range.selectNodeContents(transcriptBox);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand("copy");
    selection.removeAllRanges();
    done();
  }

  copyButton.addEventListener("click", function () { copyTranscript(copyButton); });
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
    showError("");
    resultBox.hidden = true;
    uploadBox.hidden = false;
  });
})();
