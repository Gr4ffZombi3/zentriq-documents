// KI-Assistent (Side-Panel): Schnellaktion waehlen -> Text absenden -> Ergebnis -> Kopieren.
// Gesendet wird nur der eingegebene Text. Eingabe und Ergebnis bleiben nur im Panel, bis die
// Seite verlassen wird - nichts wird im Browser gespeichert (nur die zuletzt gewaehlte Aktion).
// Ergebnisse werden per textContent eingesetzt, nie als HTML.
(function () {
  var panel = document.querySelector("[data-assistant]");
  var toggles = document.querySelectorAll("[data-assistant-toggle]");
  if (!panel || !toggles.length) return;

  var form = panel.querySelector("[data-assistant-form]");
  var input = panel.querySelector("[data-assistant-input]");
  var count = panel.querySelector("[data-assistant-count]");
  var submit = panel.querySelector("[data-assistant-submit]");
  var cancel = panel.querySelector("[data-assistant-cancel]");
  var status = panel.querySelector("[data-assistant-status]");
  var errorBox = panel.querySelector("[data-assistant-error]");
  var resultBox = panel.querySelector("[data-assistant-result-box]");
  var output = panel.querySelector("[data-assistant-result]");
  var actions = Array.prototype.slice.call(panel.querySelectorAll("[data-action]"));
  var maxChars = parseInt(panel.dataset.maxChars, 10) || 8000;
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var STORAGE_KEY = "zentriq-assistant-action";
  var controller = null;

  function selectedAction() {
    var active = actions.filter(function (b) { return b.getAttribute("aria-checked") === "true"; })[0];
    return active ? active.dataset.action : actions[0].dataset.action;
  }

  function selectAction(button, focus) {
    actions.forEach(function (b) {
      var on = b === button;
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    if (focus) button.focus();
    try { sessionStorage.setItem(STORAGE_KEY, button.dataset.action); } catch (e) { /* optional */ }
  }

  try {
    var saved = sessionStorage.getItem(STORAGE_KEY);
    var match = actions.filter(function (b) { return b.dataset.action === saved; })[0];
    if (match) selectAction(match, false);
  } catch (e) { /* optional */ }

  actions.forEach(function (button, index) {
    button.addEventListener("click", function () { selectAction(button, false); });
    button.addEventListener("keydown", function (event) {
      var step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      selectAction(actions[(index + step + actions.length) % actions.length], true);
    });
  });

  function isOpen() { return !panel.hidden; }

  function setOpen(open) {
    panel.hidden = !open;
    document.body.classList.toggle("assistant-open", open);
    toggles.forEach(function (t) {
      t.setAttribute("aria-expanded", open ? "true" : "false");
      t.classList.toggle("is-active", open);
    });
    if (open) input.focus();
  }

  toggles.forEach(function (t) {
    t.addEventListener("click", function () { setOpen(!isOpen()); });
  });
  panel.querySelector("[data-assistant-close]").addEventListener("click", function () {
    setOpen(false);
    toggles[0].focus();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) {
      if (controller) { controller.abort(); return; }
      setOpen(false);
      toggles[0].focus();
    }
  });

  function updateCount() {
    var length = input.value.length;
    count.textContent = length > maxChars * 0.8 ? length + " / " + maxChars : "";
  }
  input.addEventListener("input", updateCount);
  input.addEventListener("keydown", function (event) {
    // Strg/Cmd + Enter sendet ab.
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      send();
    }
  });

  function showError(message) {
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }

  function setBusy(busy) {
    submit.disabled = busy;
    cancel.hidden = !busy;
    status.textContent = busy ? "Wird erstellt …" : "";
    panel.classList.toggle("is-busy", busy);
    input.readOnly = busy;
  }

  function send() {
    if (controller) return;
    var text = input.value.trim();
    if (!text) {
      showError("Bitte einen Text eingeben.");
      input.focus();
      return;
    }
    showError("");
    setBusy(true);
    controller = window.AbortController ? new AbortController() : null;
    fetch(panel.dataset.url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRFToken": csrfMeta ? csrfMeta.getAttribute("content") : "",
      },
      body: JSON.stringify({ action: selectedAction(), text: text }),
      signal: controller ? controller.signal : undefined,
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) {
            if (resp.status === 403 || resp.status === 401) throw new Error("Bitte erneut anmelden.");
            throw new Error(data.error || "Der Assistent ist derzeit nicht erreichbar. Bitte erneut versuchen.");
          }
          return data;
        });
      })
      .then(function (data) {
        output.textContent = data.result || "";
        resultBox.hidden = false;
        resultBox.scrollIntoView({ block: "nearest" });
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        showError(err.message);
      })
      .finally(function () {
        var aborted = controller && controller.signal.aborted;
        controller = null;
        setBusy(false);
        if (aborted) status.textContent = "Abgebrochen.";
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    send();
  });
  cancel.addEventListener("click", function () { if (controller) controller.abort(); });

  // <button data-assistant-insert="#selector">: Text eines Elements (z. B. die anonymisierte
  // Fassung) ins Eingabefeld uebernehmen. Bewusst OHNE Absenden - der Benutzer prueft den Text
  // und entscheidet selbst.
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest ? event.target.closest("[data-assistant-insert]") : null;
    if (!trigger) return;
    var source = document.querySelector(trigger.getAttribute("data-assistant-insert"));
    if (!source || controller) return;
    input.value = (source.matches("textarea, input") ? source.value : source.textContent).slice(0, maxChars);
    output.textContent = "";
    resultBox.hidden = true;
    showError("");
    updateCount();
    setOpen(true);
    input.setSelectionRange(0, 0);
    input.scrollTop = 0;
    status.textContent = "Text übernommen – bitte prüfen und selbst absenden.";
  });

  panel.querySelector("[data-assistant-new]").addEventListener("click", function () {
    if (controller) controller.abort();
    input.value = "";
    output.textContent = "";
    resultBox.hidden = true;
    status.textContent = "";
    showError("");
    updateCount();
    input.focus();
  });
})();
