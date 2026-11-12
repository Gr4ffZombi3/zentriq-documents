/* Kunde anlegen (customers/new.html): "Kundendaten einfuegen" erkennt Angaben sofort (Server, lokal,
 * ohne KI) und fuellt nur leere bzw. noch nicht von Hand geaenderte Felder. Danach Dublettenpruefung
 * mit "Akte oeffnen". Einfuegen und Pruefen speichern nichts - gespeichert wird nur mit "Speichern". */
(function () {
  "use strict";
  var form = document.querySelector("[data-ci]");
  if (!form || !window.fetch) return;
  var text = form.querySelector("[data-ci-text]");
  var status = form.querySelector("[data-ci-status]");
  var dupes = form.querySelector("[data-ci-dupes]");
  var dupesList = form.querySelector("[data-ci-dupes-list]");
  var confirmBox = form.querySelector("[data-ci-confirm]");
  var fields = {};
  Array.prototype.forEach.call(form.querySelectorAll("[data-ci-field]"), function (input) {
    fields[input.getAttribute("data-ci-field")] = input;
    input.addEventListener("input", function () { input.dataset.manual = "1"; scheduleCheck(); });
  });
  var csrf = form.querySelector('input[name="csrf_token"]').value;
  var LABELS = { first_name: "Vorname", last_name: "Nachname", street: "Straße", house_number: "Hausnr.", postal_code: "PLZ",
    city: "Ort", phone_landline: "Festnetz", phone_private: "Privattelefon", email: "E-Mail", customer_number: "VN-Nummer" };

  function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf } })
      .then(function (r) { if (!r.ok) throw new Error("http"); return r.json(); });
  }

  var detectTimer = null, seq = 0;
  function detect() {
    var value = text.value.trim();
    if (!value) return;
    var mine = ++seq;
    post(form.getAttribute("data-detect-url"), { text: value }).then(function (d) {
      if (mine !== seq) return;
      var filled = [];
      Object.keys(fields).forEach(function (key) { // Reihenfolge der Maske
        var input = fields[key];
        if (!(d.fields || {})[key] || input.dataset.manual === "1") return; // von Hand geaendert: nicht ueberschreiben
        input.value = d.fields[key];
        input.classList.add("ci-filled");
        filled.push(LABELS[key] || key);
      });
      status.textContent = filled.length ? "Übernommen: " + filled.join(", ") + ". Bitte prüfen – noch nichts gespeichert."
        : "Keine eindeutigen Angaben erkannt. Bitte die Felder von Hand ausfüllen.";
      check();
    }).catch(function () { status.textContent = "Erkennung gerade nicht möglich – bitte die Felder von Hand ausfüllen."; });
  }
  text.addEventListener("paste", function () { setTimeout(detect, 0); });
  text.addEventListener("input", function () { clearTimeout(detectTimer); detectTimer = setTimeout(detect, 350); });

  var checkTimer = null;
  function scheduleCheck() { clearTimeout(checkTimer); checkTimer = setTimeout(check, 400); }
  function check() {
    var body = {};
    Object.keys(fields).forEach(function (k) { body[k] = fields[k].value; });
    if (!body.last_name && !body.phone_landline && !body.phone_private && !body.email && !body.customer_number) { render([]); return; }
    post(form.getAttribute("data-check-url"), body).then(function (d) { render(d.duplicates || []); }, function () {});
  }
  function render(items) {
    dupesList.textContent = "";
    items.forEach(function (d) {
      var li = document.createElement("li");
      var strong = document.createElement("strong"); strong.textContent = d.name; li.appendChild(strong);
      var meta = document.createElement("span"); meta.className = "ci-meta"; meta.textContent = " " + d.reason + (d.detail ? " · " + d.detail : "");
      li.appendChild(meta);
      var a = document.createElement("a"); a.className = "btn btn-secondary btn-sm"; a.href = d.url; a.textContent = "Akte öffnen";
      li.appendChild(a);
      dupesList.appendChild(li);
    });
    dupes.hidden = !items.length;
    if (!items.length && confirmBox) confirmBox.checked = false;
  }
  if (text.value.trim()) detect();
})();
