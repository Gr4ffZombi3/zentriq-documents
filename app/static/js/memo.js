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
    var list = el("dl", "fact-list fact-list-compact");
    fact(list, "Name", customer.name);
    // Nur vorhandene Daten anzeigen.
    if (customer.customer_number) fact(list, "Kundennummer", customer.customer_number);
    if (customer.phone) fact(list, "Telefonnummer", customer.phone);
    matchBox.appendChild(list);
    if (customer.url) {
      var link = el("a", "btn btn-secondary btn-sm", "Kundendaten öffnen");
      link.href = customer.url;
      matchBox.appendChild(link);
    }
  }

  function renderMatch(data) {
    matchBox.textContent = "";
    if (data.status === "unique") {
      matchBox.appendChild(title("Kunde erkannt", data.matched_by));
      customerFacts(data.customers[0]);
    } else if (data.status === "possible") {
      matchBox.appendChild(el("p", "memo-match-title memo-match-possible", "Möglicher Treffer – nicht sicher zugeordnet"));
      matchBox.appendChild(el("p", "form-hint", "Nur der Name stimmt überein. Bitte vor der Verwendung prüfen."));
      customerFacts(data.customers[0]);
    } else if (data.status === "multiple") {
      matchBox.appendChild(title("Mehrere mögliche Kunden gefunden.", data.matched_by));
      matchBox.appendChild(el("p", "form-hint", "Es wurde bewusst kein Kunde automatisch zugeordnet."));
      var ul = el("ul", "memo-candidates");
      data.customers.forEach(function (item) {
        var li = el("li");
        if (item.url) {
          var a = el("a", "table-link", item.name);
          a.href = item.url;
          li.appendChild(a);
        } else {
          li.appendChild(el("strong", null, item.name));
        }
        var details = [item.customer_number ? "Kundennr. " + item.customer_number : "", item.phone ? "Tel. " + item.phone : "", item.city || ""].filter(Boolean);
        if (details.length) li.appendChild(el("span", null, details.join(" · ")));
        ul.appendChild(li);
      });
      matchBox.appendChild(ul);
    } else {
      matchBox.appendChild(title("Kein vorhandener Kunde eindeutig erkannt."));
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

  function upload(file) {
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

  copyButton.addEventListener("click", function () {
    var text = transcriptBox.textContent;
    var done = function () {
      copyButton.textContent = "Kopiert";
      setTimeout(function () { copyButton.textContent = "Text kopieren"; }, 1500);
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
