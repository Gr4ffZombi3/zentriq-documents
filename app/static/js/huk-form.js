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

    // --- Datum / Monat: mit oder ohne Punkte, Formatierung erst beim Verlassen des Felds -----
    // Waehrend der Eingabe wird nichts umgestellt (Einfuegen, Korrigieren, Loeschen bleiben frei).
    const pad = (n) => String(n).padStart(2, "0");
    const normalizeDay = (v) => {
      v = v.trim();
      let m = v.match(/^(\d{2})(\d{2})(\d{4})$/) || v.match(/^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$/);
      return m ? `${pad(m[1])}.${pad(m[2])}.${m[3]}` : v;
    };
    const normalizeMonth = (v) => {
      v = v.trim();
      let m = v.match(/^(\d{2})(\d{4})$/) || v.match(/^(\d{1,2})[./-](\d{4})$/);
      return m ? `${pad(m[1])}.${m[2]}` : v;
    };
    const dayError = (v) => {
      if (!v) return "";
      const m = v.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
      if (!m) return "Bitte ein vollständiges Datum eingeben, z. B. 09.11.2026 oder 09112026.";
      const d = new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
      return d.getFullYear() === Number(m[3]) && d.getMonth() === Number(m[2]) - 1 && d.getDate() === Number(m[1]) ? "" : "Dieses Datum gibt es nicht.";
    };
    const monthError = (v) => {
      if (!v) return "";
      const m = v.match(/^(\d{2})\.(\d{4})$/);
      return m && Number(m[1]) >= 1 && Number(m[1]) <= 12 ? "" : "Bitte Monat und Jahr eingeben, z. B. 11.2026 oder 112026.";
    };
    const showError = (input, text) => {
      const box = input.closest(".huk-field");
      let el = box.querySelector(".form-error");
      if (!text) {
        if (el && el.dataset.client) el.remove();
        if (!box.querySelector(".form-error")) { box.classList.remove("huk-field-bad"); input.removeAttribute("aria-invalid"); }
        return;
      }
      if (!el) { el = document.createElement("p"); el.className = "form-error"; el.id = `${input.id}-err`; box.appendChild(el); }
      el.dataset.client = "1";
      el.textContent = text;
      box.classList.add("huk-field-bad");
      input.setAttribute("aria-invalid", "true");
      input.setAttribute("aria-describedby", el.id);
    };
    const checkFormatted = (input) => {
      const kind = input.dataset.hukFormat;
      input.value = kind === "day" ? normalizeDay(input.value) : normalizeMonth(input.value);
      const err = kind === "day" ? dayError(input.value) : monthError(input.value);
      showError(input, err);
      return !err;
    };
    form.querySelectorAll("[data-huk-format]").forEach((input) => {
      input.addEventListener("blur", () => checkFormatted(input));
      input.addEventListener("input", () => {
        // Fehlermeldung verschwindet beim Korrigieren; neue Pruefung erst beim Verlassen.
        const el = input.closest(".huk-field").querySelector(".form-error");
        if (el) { el.remove(); input.closest(".huk-field").classList.remove("huk-field-bad"); input.removeAttribute("aria-invalid"); }
      });
    });
    const scrollToFirstError = () => {
      const bad = [...form.querySelectorAll(".huk-field-bad")].find((el) => !el.hidden);
      if (!bad) return false;
      bad.scrollIntoView({ block: "center", behavior: "smooth" });
      const target = bad.querySelector("input:not([type=hidden]), select, textarea");
      if (target) setTimeout(() => target.focus({ preventScroll: true }), 300);
      return true;
    };
    // Vom Server zurueckgegebenes Formular mit Fehlern: zum ersten betroffenen Feld (Eingaben bleiben).
    if (form.querySelector(".huk-field-bad")) scrollToFirstError();

    // Waehrend der Eingabe auf dem Handy: schwebende Zeiterfassung ausblenden (verdeckt sonst Felder).
    form.addEventListener("focusin", (e) => { if (e.target.matches("input, select, textarea")) document.body.classList.add("huk-typing"); });
    form.addEventListener("focusout", () => setTimeout(() => {
      if (!form.contains(document.activeElement) || !document.activeElement.matches("input, select, textarea")) document.body.classList.remove("huk-typing");
    }, 0));

    form.addEventListener("submit", (e) => {
      if (e.submitter && e.submitter.value === "ki") return;
      const bad = [...form.querySelectorAll("[data-huk-format]")].filter((i) => !i.disabled && !checkFormatted(i));
      if (bad.length) { e.preventDefault(); scrollToFirstError(); return; }
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
