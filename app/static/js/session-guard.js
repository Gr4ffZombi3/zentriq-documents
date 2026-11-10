// Sitzungs- und Verbindungsschutz fuer alle Seiten (components/_head_common.html, als erstes Skript).
//
// 1. Sicherheitstoken (CSRF): Meldet der Server "Seite veraltet", holt das Skript einmalig ein
//    frisches Token (GET /auth/sitzung) und wiederholt die Anfrage - die Pruefung selbst bleibt.
// 2. Kein Doppel-Speichern: Jede schreibende Anfrage bekommt einen Schluessel (Idempotency-Key,
//    bei Formularen das Feld _idem). Dieselbe Aktion behaelt ihn, bis eine eindeutige Antwort
//    da ist; der Server fuehrt sie dann hoechstens einmal aus (app/services/idempotency.py).
// 3. Abgelaufene Anmeldung: Die nicht gespeicherte Aktion wird im Browser fuer genau diesen
//    Benutzer gesichert. Nach erneuter Anmeldung (auch in einem anderen Tab) erscheint
//    "Jetzt speichern" - mit demselben Schluessel, also ohne Duplikat. Beim Abmelden geloescht.
// 4. Verbindung: Bei Netzwerkabbruch ein kleiner Hinweis "Verbindung wird wiederhergestellt";
//    danach das Ereignis "zq:reconnected" fuer Seiten, die ihre Daten nachladen.
// Kein Takt-Abruf: Der Server wird nur bei Fehlern, beim Absenden nach laengerer Pause und bei
// der Rueckkehr in den Tab gefragt.
(function () {
  "use strict";
  if (window.ZentriqSession || !window.fetch) return;

  var nativeFetch = window.fetch.bind(window);
  var UNSAFE = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };
  var KEY_TTL = 30 * 60 * 1000;          // Schluessel einer unbeantworteten Aktion
  var UNSENT_TTL = 24 * 3600 * 1000;     // gesicherte Aktion nach abgelaufener Anmeldung
  var CHECK_AFTER_IDLE = 10 * 60 * 1000; // vor klassischem Absenden: Sitzung pruefen
  var lastContact = Date.now();
  var expired = false;

  function meta(name) { return document.querySelector('meta[name="' + name + '"]'); }
  function token() { var m = meta("csrf-token"); return m ? m.getAttribute("content") : ""; }
  function setToken(value) {
    var m = meta("csrf-token");
    if (m && value) m.setAttribute("content", value);
    var fields = document.querySelectorAll('input[name="csrf_token"]');
    for (var i = 0; i < fields.length; i++) if (value) fields[i].value = value;
  }
  function userId() { var m = meta("zq-user"); return m ? m.getAttribute("content") : ""; }
  function store(kind) { try { var s = window[kind]; s.getItem("x"); return s; } catch (e) { return null; } }
  function readJSON(kind, key, fallback) {
    var s = store(kind); if (!s) return fallback;
    try { var v = JSON.parse(s.getItem(key)); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function writeJSON(kind, key, value) {
    var s = store(kind); if (!s) return;
    try { if (value === null) s.removeItem(key); else s.setItem(key, JSON.stringify(value)); } catch (e) { /* nur Komfort */ }
  }
  function newKey() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, ""); } catch (e) {}
    return (Date.now().toString(36) + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)).slice(0, 32);
  }
  function sameOrigin(url) {
    try { return new URL(url, location.href).origin === location.origin; } catch (e) { return false; }
  }
  function pathOf(url) { try { return new URL(url, location.href).pathname; } catch (e) { return String(url); } }

  // ---------------------------------------------------------------------------------------
  // Schluessel je Aktion: gleiche Methode + Adresse + Inhalt = dieselbe Aktion, solange noch
  // keine eindeutige Antwort kam (Abbruch, 5xx, abgelaufene Anmeldung, "wird gespeichert").
  // ---------------------------------------------------------------------------------------
  function bodySignature(body) {
    if (body === undefined || body === null) return "";
    if (typeof body === "string") return body;
    if (window.URLSearchParams && body instanceof URLSearchParams) return body.toString();
    if (window.FormData && body instanceof FormData) {
      var parts = [];
      body.forEach(function (v, k) {
        if (k === "csrf_token" || k === "_idem") return;
        parts.push(k + "=" + (typeof v === "string" ? v : "[file:" + (v.name || "") + ":" + (v.size || 0) + "]"));
      });
      return parts.join("&");
    }
    return null; // Blob/Stream: kein stabiler Vergleich moeglich
  }
  function hashString(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36) + ":" + s.length;
  }
  function keyFor(method, url, body) {
    var sig = bodySignature(body);
    if (sig === null) return { key: newKey(), sig: null };
    sig = method + " " + pathOf(url) + " " + hashString(sig);
    var keys = readJSON("sessionStorage", "zq-idem", {}), now = Date.now();
    for (var s in keys) if (now - keys[s].t > KEY_TTL) delete keys[s];
    if (!keys[sig]) keys[sig] = { k: newKey(), t: now };
    writeJSON("sessionStorage", "zq-idem", keys);
    return { key: keys[sig].k, sig: sig };
  }
  function forgetKey(sig) {
    if (!sig) return;
    var keys = readJSON("sessionStorage", "zq-idem", {});
    if (keys[sig]) { delete keys[sig]; writeJSON("sessionStorage", "zq-idem", keys); }
  }

  // ---------------------------------------------------------------------------------------
  // Hinweisleiste (Verbindung, Anmeldung, gesicherte Aktionen)
  // ---------------------------------------------------------------------------------------
  var bar = null;
  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement("div");
    bar.className = "zq-conn";
    bar.setAttribute("role", "status");
    bar.setAttribute("aria-live", "polite");
    bar.hidden = true;
    (document.body || document.documentElement).appendChild(bar);
    return bar;
  }
  function showBar(kind, text, actions) {
    var b = ensureBar();
    b.className = "zq-conn zq-conn--" + kind;
    b.textContent = "";
    var dot = document.createElement("span");
    dot.className = "zq-conn-dot";
    dot.setAttribute("aria-hidden", "true");
    b.appendChild(dot);
    var label = document.createElement("span");
    label.className = "zq-conn-text";
    label.textContent = text;
    b.appendChild(label);
    (actions || []).forEach(function (a) {
      var btn = document.createElement(a.href ? "a" : "button");
      btn.className = "zq-conn-btn";
      btn.textContent = a.label;
      if (a.href) { btn.href = a.href; btn.target = "_blank"; btn.rel = "noopener"; }
      else { btn.type = "button"; btn.addEventListener("click", a.onClick); }
      b.appendChild(btn);
    });
    b.hidden = false;
    b.setAttribute("data-kind", kind);
  }
  function hideBar(kind) {
    if (bar && !bar.hidden && (!kind || bar.getAttribute("data-kind") === kind)) bar.hidden = true;
  }

  // ---------------------------------------------------------------------------------------
  // Verbindung
  // ---------------------------------------------------------------------------------------
  var offline = false;
  function markOffline() {
    if (offline) return;
    offline = true;
    if (!expired) showBar("offline", "Verbindung wird wiederhergestellt …");
  }
  function markOnline() {
    if (!offline) return;
    offline = false;
    hideBar("offline");
    try { document.dispatchEvent(new CustomEvent("zq:reconnected")); } catch (e) {}
  }
  window.addEventListener("offline", markOffline);
  window.addEventListener("online", function () {
    // "online" heisst nur: Netzwerk da. Erst eine echte Antwort des Servers zaehlt.
    checkSession().then(function () {}, function () {});
  });

  // ---------------------------------------------------------------------------------------
  // Sitzung
  // ---------------------------------------------------------------------------------------
  var checking = null;
  function checkSession() {
    if (checking) return checking;
    checking = nativeFetch("/auth/sitzung", { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (s) {
        markOnline();
        if (s.csrf_token) setToken(s.csrf_token);
        var me = userId();
        s.sameUser = !!(s.authenticated && me && String(s.user_id) === me);
        if (s.sameUser) lastContact = Date.now();
        if (me) {
          if (s.sameUser) { if (expired) { expired = false; hideBar("expired"); } offerUnsent(); }
          else if (!s.authenticated) markExpired();
        }
        return s;
      }, function (err) { markOffline(); throw err; })
      .then(function (s) { checking = null; return s; }, function (err) { checking = null; throw err; });
    return checking;
  }

  function markExpired() {
    if (!userId()) return;
    expired = true;
    var n = loadUnsent().length;
    showBar("expired",
      n ? "Ihre Anmeldung ist abgelaufen. " + (n === 1 ? "Eine Aktion ist" : n + " Aktionen sind") + " gesichert und kann nach der Anmeldung gespeichert werden."
        : "Ihre Anmeldung ist abgelaufen. Ihre Eingaben bleiben auf der Seite stehen.",
      [{ label: "Neu anmelden", href: "/auth/login" }]);
  }

  // ---------------------------------------------------------------------------------------
  // Gesicherte Aktionen nach abgelaufener Anmeldung (nur derselbe Benutzer, max. 24 h)
  // ---------------------------------------------------------------------------------------
  function unsentKey(uid) { return "zq-unsent:" + (uid || userId()); }
  function loadUnsent(uid) {
    var now = Date.now();
    return readJSON("localStorage", unsentKey(uid), []).filter(function (a) { return now - a.t < UNSENT_TTL; });
  }
  function saveUnsent(list, uid) { writeJSON("localStorage", unsentKey(uid), list.length ? list.slice(-10) : null); }

  function serializableBody(body) {
    if (body === undefined || body === null) return { type: "none" };
    if (typeof body === "string") return { type: "text", value: body };
    if (window.URLSearchParams && body instanceof URLSearchParams) return { type: "urlencoded", value: body.toString() };
    if (window.FormData && body instanceof FormData) {
      var entries = [], hasFile = false;
      body.forEach(function (v, k) { if (typeof v === "string") entries.push([k, v]); else hasFile = true; });
      if (hasFile) return null; // Dateien lassen sich nicht sicher aufheben
      return { type: "form", value: entries };
    }
    return null;
  }
  function restoreBody(b) {
    if (!b || b.type === "none") return undefined;
    if (b.type === "text") return b.value;
    if (b.type === "urlencoded") return new URLSearchParams(b.value);
    var fd = new FormData();
    b.value.forEach(function (e) { fd.append(e[0], e[1]); });
    return fd;
  }

  function stashUnsent(url, method, headers, body, key) {
    var uid = userId();
    if (!uid || !key) return false;
    var sb = serializableBody(body);
    if (!sb) return false;
    var keep = {};
    ["Content-Type", "Accept"].forEach(function (h) { var v = headers.get(h); if (v) keep[h] = v; });
    var list = loadUnsent(uid).filter(function (a) { return a.key !== key; });
    list.push({ url: pathOf(url) + (new URL(url, location.href).search || ""), method: method, headers: keep, body: sb, key: key,
                page: document.title, t: Date.now() });
    saveUnsent(list, uid);
    return true;
  }

  function offerUnsent() {
    var list = loadUnsent();
    if (!list.length) { hideBar("unsent"); return; }
    var text = list.length === 1 ? "Eine Aktion wurde wegen der abgelaufenen Anmeldung nicht gespeichert."
      : list.length + " Aktionen wurden wegen der abgelaufenen Anmeldung nicht gespeichert.";
    showBar("unsent", text, [
      { label: "Jetzt speichern", onClick: resendUnsent },
      { label: "Verwerfen", onClick: function () { saveUnsent([]); hideBar("unsent"); } },
    ]);
  }

  function resendUnsent() {
    var list = loadUnsent();
    var done = 0, failed = [];
    showBar("unsent", "Wird gespeichert …");
    var chain = Promise.resolve();
    list.forEach(function (a) {
      chain = chain.then(function () {
        var headers = new Headers(a.headers || {});
        headers.set("Idempotency-Key", a.key);
        return guardedFetch(a.url, { method: a.method, headers: headers, body: restoreBody(a.body), credentials: "same-origin" }, { noStash: true })
          .then(function (r) {
            if (r.ok && !(r.redirected && /\/auth\/login/.test(r.url))) done++;
            else failed.push({ action: a, status: r.status });
          }, function () { failed.push({ action: a, status: 0 }); });
      });
    });
    chain.then(function () {
      // Nur Fehler, die eine Wiederholung sinnvoll machen, bleiben gesichert.
      saveUnsent(failed.filter(function (f) { return f.status === 0 || f.status >= 500 || f.status === 401; })
        .map(function (f) { return f.action; }));
      var rejected = failed.filter(function (f) { return f.status >= 400 && f.status < 500 && f.status !== 401; }).length;
      if (!failed.length) {
        showBar("saved", done === 1 ? "Gespeichert." : done + " Aktionen gespeichert.",
          [{ label: "Seite aktualisieren", onClick: function () { location.reload(); } }]);
      } else if (rejected) {
        showBar("unsent", "Nicht alles konnte gespeichert werden (z. B. Termin inzwischen belegt). Bitte auf der Seite prüfen.",
          [{ label: "Seite aktualisieren", onClick: function () { location.reload(); } }]);
      } else {
        offerUnsent();
      }
    });
  }

  // ---------------------------------------------------------------------------------------
  // fetch-Huelle
  // ---------------------------------------------------------------------------------------
  function readCode(resp) {
    var type = resp.headers.get("Content-Type") || "";
    if (type.indexOf("json") < 0) return Promise.resolve(null);
    return resp.clone().json().then(function (d) { return d && d.code; }, function () { return null; });
  }
  function isLoginRedirect(resp) { return resp.redirected && /\/auth\/login/.test(resp.url || ""); }

  function guardedFetch(url, init, opts) {
    opts = opts || {};
    var method = String(init.method || "GET").toUpperCase();
    var headers = new Headers(init.headers || {});
    headers.set("X-Zentriq-Fetch", "1");
    var unsafe = !!UNSAFE[method];
    var ident = null;
    if (unsafe) {
      if (!headers.has("Idempotency-Key")) {
        ident = keyFor(method, url, init.body);
        headers.set("Idempotency-Key", ident.key);
      }
      var t = token();
      if (t) headers.set("X-CSRFToken", t);
      if (window.FormData && init.body instanceof FormData && init.body.has("csrf_token") && t) init.body.set("csrf_token", t);
    }
    var sig = ident ? ident.sig : opts.sig;
    var sendInit = {};
    for (var k in init) sendInit[k] = init[k];
    sendInit.headers = headers;

    return nativeFetch(url, sendInit).then(function (resp) {
      markOnline();
      if (resp.status === 401 || isLoginRedirect(resp)) {
        return readCode(resp).then(function (code) {
          if (resp.status === 401 && code && code !== "login_required") return resp;
          if (unsafe && !opts.noStash) stashUnsent(url, method, headers, init.body, headers.get("Idempotency-Key"));
          markExpired();
          return resp;
        });
      }
      // Antwort mit gueltiger Anmeldung: Sitzung lebt.
      if (userId()) lastContact = Date.now();
      if (!unsafe) return resp;
      if (resp.status === 400 && !opts.retried) {
        return readCode(resp).then(function (code) {
          if (code !== "csrf") { forgetKey(sig); return resp; }
          // Token veraltet: frisch holen und genau einmal wiederholen (gleicher Schluessel).
          return checkSession().then(function (s) {
            if (!s.sameUser) {
              if (!opts.noStash) stashUnsent(url, method, headers, init.body, headers.get("Idempotency-Key"));
              if (!s.authenticated) markExpired();
              return resp;
            }
            var again = {};
            for (var k2 in init) again[k2] = init[k2];
            again.headers = new Headers(headers);
            again.headers.set("Idempotency-Key", headers.get("Idempotency-Key"));
            return guardedFetch(url, again, { retried: true, noStash: opts.noStash, sig: sig });
          }, function () { return resp; });
        });
      }
      if (resp.status === 409) {
        return readCode(resp).then(function (code) {
          if (code !== "in_progress") { forgetKey(sig); return resp; }
          // Dieselbe Aktion laeuft schon (z. B. Doppelklick): kurz warten und deren Ergebnis holen.
          var waits = opts.waits || 0;
          if (waits >= 15) return resp;
          return new Promise(function (resolve) { setTimeout(resolve, 700); }).then(function () {
            var again = {};
            for (var k3 in init) again[k3] = init[k3];
            again.headers = new Headers(headers);
            return guardedFetch(url, again, { retried: opts.retried, noStash: opts.noStash, sig: sig, waits: waits + 1 });
          });
        });
      }
      // Eindeutige Antwort (Erfolg oder fachlicher Fehler): naechste gleiche Eingabe ist eine neue Aktion.
      if (resp.status < 500) forgetKey(sig);
      return resp;
    }, function (err) {
      if (!(err && err.name === "AbortError")) markOffline();
      throw err;
    });
  }

  window.fetch = function (input, init) {
    // Request-Objekte und fremde Adressen unveraendert durchreichen.
    if (typeof input !== "string" && !(window.URL && input instanceof URL)) return nativeFetch(input, init);
    var url = String(input);
    if (!sameOrigin(url) || /\/auth\/(sitzung|login|logout)(\?|$)/.test(pathOf(url))) return nativeFetch(input, init);
    return guardedFetch(url, init || {});
  };

  // ---------------------------------------------------------------------------------------
  // Klassische Formulare (POST ohne fetch)
  // ---------------------------------------------------------------------------------------
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.tagName !== "FORM" || event.defaultPrevented) return;
    var method = String(form.getAttribute("method") || "GET").toUpperCase();
    if (method !== "POST" || !sameOrigin(form.action || location.href)) return;
    var action = pathOf(form.action || location.href);
    if (/\/auth\/logout$/.test(action)) {
      // Ausdrueckliches Abmelden: gesicherte Aktionen dieses Benutzers verwerfen.
      saveUnsent([]);
      writeJSON("sessionStorage", "zq-idem", null);
      return;
    }
    if (/^\/auth\//.test(action)) return; // Anmeldung, 2FA, Passwort: unveraendert
    // Jede Formularinstanz behaelt ihren Schluessel (Doppelklick, Zurueck + erneut Senden).
    var idem = form.querySelector('input[name="_idem"]');
    if (!idem) {
      idem = document.createElement("input");
      idem.type = "hidden"; idem.name = "_idem"; idem.value = newKey();
      form.appendChild(idem);
    }
    var csrfField = form.querySelector('input[name="csrf_token"]');
    if (csrfField && token()) csrfField.value = token();
    if (!userId() || (!expired && Date.now() - lastContact < CHECK_AFTER_IDLE)) return;
    // Nach laengerer Pause erst pruefen, ob die Anmeldung noch gilt - sonst gingen die Eingaben
    // beim Weiterleiten zur Anmeldung verloren.
    event.preventDefault();
    var submitter = event.submitter || null;
    checkSession().then(function (s) {
      if (s.sameUser) {
        if (csrfField && s.csrf_token) csrfField.value = s.csrf_token;
        // form.submit() loest kein zweites submit-Ereignis aus (Rueckfragen der Seite liefen
        // schon); die Eingabepruefung des Browsers hat vor dem ersten Ereignis stattgefunden.
        // Den gedrueckten Knopf (name/value) als verstecktes Feld mitgeben.
        if (submitter && submitter.name && submitter.form === form) {
          var carry = document.createElement("input");
          carry.type = "hidden"; carry.name = submitter.name; carry.value = submitter.value || "";
          form.appendChild(carry);
        }
        HTMLFormElement.prototype.submit.call(form);
      }
      // Sonst: Anmeldung abgelaufen bzw. anderer Benutzer -> Hinweis steht, Formular bleibt ausgefuellt.
    }, function () {
      showBar("offline", "Keine Verbindung – das Formular wurde nicht gesendet. Ihre Eingaben bleiben stehen.");
      offline = true;
    });
  }, false);

  // ---------------------------------------------------------------------------------------
  // Rueckkehr in den Tab / Seitenstart
  // ---------------------------------------------------------------------------------------
  var hiddenSince = 0;
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { hiddenSince = Date.now(); return; }
    if (userId() && (expired || offline || Date.now() - Math.max(hiddenSince, lastContact) > 5 * 60 * 1000 || loadUnsent().length)) {
      checkSession().then(function () {}, function () {});
    }
  });
  // Anmeldung in einem anderen Tab: Hinweis sofort aktualisieren.
  window.addEventListener("storage", function (e) {
    if (e.key === "zq-login-ok" && (expired || loadUnsent().length)) checkSession().then(function () {}, function () {});
  });
  function onReady() {
    if (!userId()) return;
    // Andere Tabs erfahren so von einer neuen Anmeldung (storage-Ereignis) - ohne Abfrage.
    writeJSON("localStorage", "zq-login-ok", userId() + ":" + Date.now());
    if (loadUnsent().length) offerUnsent();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", onReady);
  else onReady();

  window.ZentriqSession = { check: checkSession, fetch: guardedFetch, offerUnsent: offerUnsent, isExpired: function () { return expired; } };
})();
