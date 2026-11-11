/* Anruferkennung (Webex Calling): Hinweis beim Klingeln im geoeffneten Tab.
 *
 * Persoenlicher Live-Kanal (zentriq-live, Kanal user:<id>). Ueber den Kanal kommt nur die Anruf-ID;
 * Name/Treffer holt der Browser angemeldet bei Zentriq (eigenes Buero, eigene Durchwahl).
 *   call     -> Hinweis anzeigen ("… ruft an" / Auswahl / Nummer + Rueckrufnotiz / Unbekannter Anrufer)
 *   call_end -> angenommen oder aufgelegt: Hinweis kurz danach ausblenden
 * Die geoeffnete Seite bzw. Kundenakte bleibt unveraendert - es wird nichts automatisch geoeffnet.
 * "Kundenakte öffnen" nutzt bei Buero-Admins das Seitenfenster (js/workspace.js, data-zq-panel). */
(function () {
  "use strict";
  var box = document.querySelector("[data-zq-calls]");
  if (!box || !window.EventSource || !window.fetch) return;

  var RING_MAX = 120000; // ohne Ende-Meldung nach 2 Minuten ausblenden
  var END_HIDE = 6000;
  var shown = {}; // id -> Element
  var es = null, failures = 0, timer = null, first = true;
  var baseTitle = document.title, titleTimer = null;
  var OFF_KEY = "zq-calls-off", OFF_TTL = 600000; // keine Durchwahl: 10 Minuten nicht erneut fragen

  function offSince() {
    try { return parseInt(window.sessionStorage.getItem(OFF_KEY) || "0", 10) || 0; } catch (e) { return 0; }
  }
  function markOff(on) {
    try {
      if (on) window.sessionStorage.setItem(OFF_KEY, String(Date.now()));
      else window.sessionStorage.removeItem(OFF_KEY);
    } catch (e) { /* Speicher gesperrt: dann eben bei jeder Seite fragen */ }
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json", "X-Zentriq-Fetch": "1" } }).then(function (r) {
      if (!r.ok || (r.headers.get("Content-Type") || "").indexOf("json") < 0) throw new Error("http");
      return r.json();
    });
  }

  function flashTitle(on, text) {
    clearInterval(titleTimer);
    document.title = baseTitle;
    if (!on) return;
    var alt = false;
    titleTimer = setInterval(function () { alt = !alt; document.title = alt ? "☎ " + text : baseTitle; }, 1000);
  }
  function anyRinging() {
    for (var id in shown) if (shown[id] && !shown[id].classList.contains("is-ended")) return true;
    return false;
  }

  function remove(id) {
    var node = shown[id];
    if (!node) return;
    clearTimeout(node._zqTimer);
    if (node.parentNode) node.parentNode.removeChild(node);
    delete shown[id];
    if (!anyRinging()) flashTitle(false);
  }

  function customerLink(c, cls, id) {
    var a = el("a", cls);
    a.href = c.url;
    if (c.panel) a.setAttribute("data-zq-panel", "");
    // Akte gewaehlt: Hinweis schliessen (er laege sonst ueber dem Kopf des Seitenfensters).
    // Erst nach dem Klick-Handler von workspace.js, der das Seitenfenster oeffnet.
    a.addEventListener("click", function () { setTimeout(function () { remove(id); }, 0); });
    return a;
  }

  function render(call) {
    if (!call || call.status !== "ringing") return;
    if (shown[call.id]) return; // doppelte Meldung / mehrere Wege: nur einmal anzeigen
    var card = el("section", "zq-call");
    card.setAttribute("role", "alert");
    card.setAttribute("aria-label", "Eingehender Anruf");
    var x = el("button", "zq-call-close", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Hinweis schließen");
    x.addEventListener("click", function () { remove(call.id); });
    card.appendChild(x);
    card.appendChild(el("p", "zq-call-label", "Eingehender Anruf"));
    card.appendChild(el("p", "zq-call-title", call.title));
    if (call.number && call.kind !== "none" && call.kind !== "multiple") card.appendChild(el("p", "zq-call-number", call.number));

    var actions = el("p", "zq-call-actions");
    if (call.kind === "single") {
      var open = customerLink(call.customers[0], "btn btn-primary btn-sm", call.id);
      open.textContent = "Kundenakte öffnen";
      actions.appendChild(open);
      if (call.customers[0].detail) card.appendChild(el("p", "zq-call-number", call.customers[0].detail));
    } else if (call.kind === "multiple") {
      card.appendChild(el("p", "zq-call-number", call.customers.length + " Kunden mit dieser Nummer – bitte auswählen:"));
      var list = el("ul", "zq-call-list");
      call.customers.forEach(function (c) {
        var li = el("li");
        var a = customerLink(c, "zq-call-pick", call.id);
        a.appendChild(el("strong", null, c.name));
        if (c.detail) a.appendChild(el("span", null, c.detail));
        li.appendChild(a);
        list.appendChild(li);
      });
      card.appendChild(list);
    } else if (call.kind === "none") {
      card.appendChild(el("p", "zq-call-number", "Keine Kundenakte mit dieser Nummer."));
      var note = el("a", "btn btn-primary btn-sm", "Rückrufnotiz anlegen");
      note.href = call.callback_url;
      actions.appendChild(note);
    }
    if (actions.childNodes.length) card.appendChild(actions);

    box.appendChild(card);
    shown[call.id] = card;
    card._zqTimer = setTimeout(function () { remove(call.id); }, RING_MAX);
    flashTitle(true, call.title);
  }

  function ended(id) {
    var node = shown[id];
    if (!node) return;
    node.classList.add("is-ended");
    var label = node.querySelector(".zq-call-label");
    if (label) label.textContent = "Anruf beendet";
    node.setAttribute("role", "status");
    clearTimeout(node._zqTimer);
    node._zqTimer = setTimeout(function () { remove(id); }, END_HIDE);
    if (!anyRinging()) flashTitle(false);
  }

  function load(id) {
    var url = box.getAttribute("data-call-url").replace(/\/0$/, "/" + encodeURIComponent(id));
    getJSON(url).then(function (d) { render(d.call); }).catch(function () {});
  }

  function retry() {
    clearTimeout(timer);
    failures += 1;
    // Weiter versuchen (Neustart des Dienstes, Netzwechsel), aber zunehmend seltener.
    timer = setTimeout(connect, Math.min(60000, 2000 * Math.pow(2, Math.min(failures, 5))));
  }

  function connect() {
    if (es) { es.close(); es = null; }
    getJSON(box.getAttribute("data-token-url")).then(function (d) {
      if (!d.enabled) { markOff(true); return; } // keine Durchwahl / Live-Dienst aus: still beenden
      markOff(false);
      if (first) {
        first = false;
        // Seite waehrend des Klingelns neu geladen: laufenden Anruf gleich anzeigen.
        getJSON(box.getAttribute("data-current-url")).then(function (c) { if (c.call) render(c.call); }).catch(function () {});
      }
      es = new EventSource(d.stream + "?token=" + encodeURIComponent(d.token));
      es.addEventListener("hello", function () { failures = 0; });
      es.addEventListener("call", function (ev) {
        try { load(JSON.parse(ev.data).id); } catch (e) { /* ungueltig */ }
      });
      es.addEventListener("call_end", function (ev) {
        try { ended(JSON.parse(ev.data).id); } catch (e) { /* ungueltig */ }
      });
      es.addEventListener("renew", function () { es.close(); es = null; connect(); });
      es.onerror = function () { if (es) { es.close(); es = null; } retry(); };
    }).catch(retry);
  }

  if (Date.now() - offSince() < OFF_TTL) return;
  connect();
  window.addEventListener("pagehide", function () { if (es) es.close(); });
})();
