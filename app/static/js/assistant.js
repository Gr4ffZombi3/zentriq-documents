// Assistent (Werkzeuge -> Assistent): Auftrag oder Text -> "Text erstellen" -> Ergebnis ->
// Kopieren bzw. Kuerzer / Freundlicher / Professioneller / Neu formulieren (ueberarbeitet das
// Ergebnis). Gesendet wird nur der jeweilige Text; nichts wird im Browser gespeichert.
// Ergebnisse werden per textContent eingesetzt, nie als HTML.
(function () {
  var root = document.querySelector("[data-assistant]");
  if (!root) return;

  var form = root.querySelector("[data-assistant-form]");
  var input = root.querySelector("[data-assistant-input]");
  var count = root.querySelector("[data-assistant-count]");
  var submit = root.querySelector("[data-assistant-submit]");
  var status = root.querySelector("[data-assistant-status]");
  var errorBox = root.querySelector("[data-assistant-error]");
  var resultBox = root.querySelector("[data-assistant-result-box]");
  var output = root.querySelector("[data-assistant-result]");
  var refineButtons = Array.prototype.slice.call(root.querySelectorAll("[data-assistant-refine]"));
  var maxChars = parseInt(root.dataset.maxChars, 10) || 8000;
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var busy = false;

  function updateCount() {
    var length = input.value.length;
    count.textContent = length > maxChars * 0.8 ? length + " / " + maxChars : "";
  }

  function showError(message) {
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }

  function setBusy(state, text) {
    busy = state;
    submit.disabled = state;
    refineButtons.forEach(function (b) { b.disabled = state; });
    input.readOnly = state;
    root.classList.toggle("is-busy", state);
    status.textContent = state ? text : "";
  }

  function request(action, text, busyText) {
    if (busy) return;
    showError("");
    setBusy(true, busyText);
    fetch(root.dataset.url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRFToken": csrfMeta ? csrfMeta.getAttribute("content") : "",
      },
      body: JSON.stringify({ action: action, text: text }),
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) {
            if (resp.status === 401) throw new Error("Die Anmeldung ist abgelaufen. Bitte die Seite neu laden.");
            throw new Error(data.error || "Der Assistent ist derzeit nicht erreichbar. Bitte erneut versuchen.");
          }
          return data;
        });
      }, function () {
        throw new Error("Keine Verbindung zum Server. Bitte erneut versuchen.");
      })
      .then(function (data) {
        output.textContent = data.result || "";
        resultBox.hidden = false;
        resultBox.scrollIntoView({ block: "nearest" });
      })
      .catch(function (err) { showError(err.message); })
      .finally(function () { setBusy(false); });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text) {
      showError("Bitte einen Auftrag oder Text eingeben.");
      input.focus();
      return;
    }
    request("erstellen", text, "Text wird erstellt …");
  });

  input.addEventListener("input", updateCount);
  input.addEventListener("keydown", function (event) {
    // Strg/Cmd + Enter: Text erstellen.
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      form.requestSubmit ? form.requestSubmit() : submit.click();
    }
  });

  // Ueberarbeitungen beziehen sich auf das angezeigte Ergebnis.
  refineButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      var text = output.textContent.trim();
      if (text) request(button.dataset.assistantRefine, text, "Text wird überarbeitet …");
    });
  });

  // Uebergabe aus einem anderen Werkzeug (z. B. "Im Assistenten verwenden" beim Anonymisieren):
  // Text uebernehmen, sofort aus dem Speicher loeschen, NICHT absenden.
  try {
    var handoff = sessionStorage.getItem("zentriq-assistant-handoff");
    if (handoff) {
      sessionStorage.removeItem("zentriq-assistant-handoff");
      input.value = handoff.slice(0, maxChars);
      status.textContent = "Text übernommen – bitte prüfen und dann „Text erstellen“ wählen.";
    }
  } catch (e) { /* ohne sessionStorage: leeres Feld */ }
  updateCount();
})();
