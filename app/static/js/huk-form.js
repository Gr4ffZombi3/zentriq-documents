// Kfz-Berechnung (HUK): Folgefragen ein-/ausblenden, Vorschlaege uebernehmen, Kunde suchen,
// Startknopf gegen Doppelklick sichern und den Status des Hintergrundauftrags abfragen.
// Alles funktioniert auch ohne Skript (Server prueft und zeigt Folgefragen nach dem Speichern).
(function () {
  "use strict";

  const form = document.querySelector("[data-huk-form]");
  if (form) {
    const valuesOf = (name) => [...form.querySelectorAll(`[name="${name}"]`)]
      .filter((el) => (el.type === "radio" || el.type === "checkbox") ? el.checked : true)
      .map((el) => el.value);
    const update = () => {
      form.querySelectorAll("[data-show-if]").forEach((box) => {
        const rule = box.dataset.showIf;
        let show;
        if (rule.includes("!=")) {
          const [name, value] = rule.split("!=");
          show = valuesOf(name).some((v) => v !== value);
        } else {
          const [name, value] = rule.split("=");
          show = valuesOf(name).includes(value);
        }
        box.hidden = !show;
        box.querySelectorAll("input, select, textarea").forEach((el) => { el.disabled = !show; });
      });
    };
    form.addEventListener("change", update);
    update();

    form.querySelectorAll("[data-huk-proposal]").forEach((button) => {
      button.addEventListener("click", () => {
        const input = document.getElementById(button.dataset.hukProposal);
        if (input) { input.value = button.dataset.value; input.focus(); }
        button.remove();
      });
    });

    form.addEventListener("submit", () => {
      form.querySelectorAll("button[type=submit]").forEach((b) => {
        setTimeout(() => { b.disabled = true; }, 0);
      });
    });

    const box = form.querySelector("[data-huk-customer]");
    if (box) {
      const idField = box.querySelector("[data-huk-customer-id]");
      const current = box.querySelector("[data-huk-customer-current]");
      const nameField = box.querySelector("[data-huk-customer-name]");
      const search = box.querySelector("[data-huk-customer-search]");
      const input = box.querySelector("[data-huk-customer-input]");
      const results = box.querySelector("[data-huk-customer-results]");
      let timer = null;
      let seq = 0;
      box.querySelector("[data-huk-customer-clear]").addEventListener("click", () => {
        idField.value = "";
        current.hidden = true;
        search.hidden = false;
        input.focus();
      });
      input.addEventListener("input", () => {
        clearTimeout(timer);
        const q = input.value.trim();
        if (q.length < 2) { results.replaceChildren(); return; }
        timer = setTimeout(async () => {
          const mine = ++seq;
          try {
            const res = await fetch(`${box.dataset.api}?q=${encodeURIComponent(q)}`, { headers: { Accept: "application/json" } });
            if (!res.ok || mine !== seq) return;
            const data = await res.json();
            results.replaceChildren(...data.customers.map((c) => {
              const li = document.createElement("li");
              const b = document.createElement("button");
              b.type = "button";
              b.className = "huk-customer-pick";
              b.textContent = c.detail ? `${c.name} · ${c.detail}` : c.name;
              b.addEventListener("click", () => {
                idField.value = c.id;
                nameField.textContent = c.name;
                current.hidden = false;
                search.hidden = true;
                results.replaceChildren();
                input.value = "";
              });
              li.append(b);
              return li;
            }));
            if (!data.customers.length) {
              const li = document.createElement("li");
              li.className = "form-hint";
              li.textContent = "Kein Kunde gefunden.";
              results.append(li);
            }
          } catch (_e) { /* Netz: Suche einfach erneut moeglich */ }
        }, 250);
      });
    }
  }

  const start = document.querySelector("[data-huk-start]");
  if (start) {
    start.addEventListener("submit", (event) => {
      if (start.dataset.sent) { event.preventDefault(); return; }
      start.dataset.sent = "1";
      const button = start.querySelector("[data-huk-start-button]");
      setTimeout(() => { button.disabled = true; button.textContent = "Wird gestartet …"; }, 0);
    });
  }

  const job = document.querySelector("[data-huk-job]");
  if (job && (job.dataset.status === "queued" || job.dataset.status === "running")) {
    const message = job.querySelector("[data-huk-message]");
    let delay = 2500;
    const poll = async () => {
      try {
        const res = await fetch(job.dataset.statusUrl, { headers: { Accept: "application/json" } });
        if (res.ok) {
          const data = await res.json();
          if (data.finished) { window.location.reload(); return; }
          if (message && data.message) message.textContent = data.message;
        }
      } catch (_e) { /* naechster Versuch */ }
      delay = Math.min(delay + 500, 6000);
      setTimeout(poll, delay);
    };
    setTimeout(poll, delay);
  }
})();
