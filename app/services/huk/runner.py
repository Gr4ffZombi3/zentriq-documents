"""Bedienung des oeffentlichen HUK-Pkw-Tarifrechners mit Playwright (headless Chromium).

Grundsaetze:
- Jede Antwort wird ausdruecklich gesetzt und zurueckgelesen (aria-checked / Feldwert) - die
  Vorbelegungen des Rechners werden nie ungeprueft uebernommen.
- Der Ablauf endet auf der Seite "Angebot" (Beitragsberechnung). Antragsdetails, persoenliche
  Daten und Uebersicht werden nie geoeffnet; es wird nichts beantragt oder abgeschickt.
- Ergebnisse stammen ausschliesslich aus der angezeigten Rechnerseite. Ist kein Beitrag lesbar,
  schlaegt die Berechnung fehl - es wird nichts geschaetzt.
- Captcha, Zugriffssperre oder unbekannte Pflichtfragen werden nicht umgangen, sondern als
  "blocked" bzw. "needs_input" gemeldet (manuelle Uebergabe).
- Jeder Auftrag startet einen eigenen Browser mit leerem, eigenem Profilverzeichnis (keine
  geteilten Cookies zwischen Auftraegen oder Bueros); er wird am Ende immer geschlossen und das
  Profil geloescht."""

import re
import time
from dataclasses import dataclass

from app.services.huk.schema import FIELD_BY_KEY

START_URL = "https://www.huk.de/tarifrechner/pkw"
ALLOWED_HOSTS = ("www.huk.de", "pkw.tarifrechner.c.huk.de", "huk.de")
STOP_PATHS = ("antragsdetails", "persoenliche-daten", "uebersicht", "antrag")
BLOCK_PATTERNS = re.compile(r"captcha|zugriff verweigert|access denied|zu viele anfragen|too many requests|ungewöhnliche aktivität", re.I)
PRICE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")


class HukRunError(Exception):
    """code: needs_input | blocked | timeout | cancelled | site_changed | network | no_result"""

    def __init__(self, code: str, message: str, details: dict | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable


@dataclass
class Context:
    page: object
    deadline: float
    should_cancel: object

    def check(self) -> None:
        if self.should_cancel():
            raise HukRunError("cancelled", "Die Berechnung wurde abgebrochen.")
        if time.monotonic() > self.deadline:
            raise HukRunError("timeout", "Die Berechnung hat zu lange gedauert und wurde beendet.", retryable=True)


# --- Hilfen ------------------------------------------------------------------------------------


def _guard(ctx: Context) -> None:
    page = ctx.page
    url = page.url
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    if host not in ALLOWED_HOSTS:
        raise HukRunError("site_changed", "Der Rechner hat auf eine unerwartete Seite weitergeleitet.")
    if any(f"/{part}" in url for part in STOP_PATHS):
        raise HukRunError("site_changed", "Der Rechner ist unerwartet in den Antragsbereich gewechselt – abgebrochen, nichts beantragt.")
    if page.locator("iframe[src*='captcha'], iframe[title*='captcha' i], .g-recaptcha, .h-captcha, .frc-captcha").count():
        raise HukRunError("blocked", "Der Rechner verlangt eine Sicherheitsabfrage (Captcha). Bitte die Berechnung manuell auf huk.de durchführen.")
    body = page.locator("body").inner_text(timeout=5000)[:4000]
    if BLOCK_PATTERNS.search(body) and "Fahrzeugdaten" not in body:
        raise HukRunError("blocked", "Der Rechner hat den Zugriff vorübergehend gesperrt. Bitte später erneut oder manuell auf huk.de versuchen.")


def _group(page, question_regex: str):
    groups = page.locator("fieldset[role=group]").all()
    rx = re.compile(question_regex, re.I)
    for g in groups:
        label = g.get_attribute("aria-label") or ""
        if rx.search(label) and g.is_visible():
            return g
    return None


def _choose(ctx: Context, key: str, question_regex: str, value: str) -> None:
    ctx.check()
    page = ctx.page
    field = FIELD_BY_KEY[key]
    label = next(o.label for o in field.options if o.value == value)
    group = None
    for _ in range(20):
        group = _group(page, question_regex)
        if group is not None:
            break
        page.wait_for_timeout(250)
    if group is None:
        raise HukRunError("site_changed", f"Die Frage „{field.question}“ wurde im Rechner nicht gefunden.", {"field": key})
    item = group.locator("[role=radio], [role=checkbox]").filter(has_text=re.compile(rf"^\s*{re.escape(label)}\s*$"))
    if item.count() == 0:
        options = [t.strip() for t in group.locator("[role=radio], [role=checkbox]").all_inner_texts()]
        raise HukRunError("needs_input", f"Die Antwort „{label}“ wird für „{field.question}“ nicht angeboten.", {"field": key, "options": options})
    target = item.first
    if target.get_attribute("aria-checked") != "true":
        target.click()
        page.wait_for_timeout(250)
    if target.get_attribute("aria-checked") != "true":
        raise HukRunError("site_changed", f"Die Antwort für „{field.question}“ ließ sich nicht setzen.", {"field": key})


def _choose_multi(ctx: Context, question_regex: str, wanted_labels: set[str]) -> None:
    ctx.check()
    group = _group(ctx.page, question_regex)
    if group is None:
        raise HukRunError("site_changed", "Die Frage zum Fahrerkreis wurde nicht gefunden.", {"field": "fahrer"})
    items = group.locator("[role=checkbox], [role=radio]")
    for i in range(items.count()):
        item = items.nth(i)
        text = item.inner_text().strip()
        should = text in wanted_labels
        if (item.get_attribute("aria-checked") == "true") != should:
            item.click()
            ctx.page.wait_for_timeout(250)
        if (item.get_attribute("aria-checked") == "true") != should:
            raise HukRunError("site_changed", "Der Fahrerkreis ließ sich nicht setzen.", {"field": "fahrer"})


def _fill(ctx: Context, name: str, value: str, typed: bool = False) -> None:
    ctx.check()
    field = ctx.page.locator(f"input[name='{name}']:visible").first
    field.click()
    field.fill("")
    if typed:
        field.press_sequentially(value, delay=35)
    else:
        field.fill(value)
    field.press("Tab")
    ctx.page.wait_for_timeout(300)


def _combobox(ctx: Context, index: int, label: str, key: str) -> None:
    ctx.check()
    page = ctx.page
    box = page.locator("input[placeholder='Bitte auswählen']:visible").nth(index)
    box.click()
    page.wait_for_timeout(500)
    option = page.get_by_role("option", name=label, exact=True)
    if option.count() == 0:
        options = [t.strip() for t in page.locator("[role=option]").all_inner_texts()]
        page.keyboard.press("Escape")
        raise HukRunError("needs_input", f"„{label}“ wird im Rechner nicht angeboten.", {"field": key, "options": options})
    option.first.click()
    page.wait_for_timeout(300)
    if box.input_value().strip() != label:
        raise HukRunError("site_changed", "Die Schadenfreiheitsklasse ließ sich nicht setzen.", {"field": key})


def _select(ctx: Context, label_part: str, wanted: str, key: str) -> str:
    """Auswahlfeld des Angebots (s-select) ueber seinen Fragetext setzen und zurueckpruefen.
    Fehlt das Feld oder die Option, wird abgebrochen - nie stillschweigend uebersprungen."""
    ctx.check()
    page = ctx.page
    trigger = page.locator(f"s-select[s-aria-label*='{label_part}'] button[role=combobox]")
    if trigger.count() != 1:
        raise HukRunError("site_changed", f"Das Auswahlfeld „{label_part}“ wurde im Rechner nicht eindeutig gefunden.", {"field": key})
    trigger = trigger.first
    if trigger.inner_text().strip() != wanted:
        trigger.click()
        page.wait_for_timeout(500)
        menu = page.locator(f"#{trigger.get_attribute('aria-controls')}")
        option = menu.get_by_role("option", name=wanted, exact=True)
        if option.count() == 0:
            options = [t.strip() for t in menu.get_by_role("option").all_inner_texts()]
            page.keyboard.press("Escape")
            raise HukRunError("needs_input", f"„{wanted}“ wird für „{label_part}“ nicht angeboten.", {"field": key, "options": options})
        option.first.click()
        page.wait_for_timeout(2500)
    shown = trigger.inner_text().strip()
    if shown != wanted:
        raise HukRunError("site_changed", f"„{wanted}“ ließ sich für „{label_part}“ nicht einstellen.", {"field": key})
    return shown


def _errors(page) -> list[str]:
    texts = page.locator("main [role=alert]:visible, main [class*=error]:visible").all_inner_texts()
    return [t.strip() for t in texts if t.strip()][:5]


def _open_questions(page) -> list[str]:
    """Sichtbare Fragen ohne gewaehlte Antwort (Grund fuer einen gesperrten Weiter-Knopf)."""
    open_ = []
    for g in page.locator("fieldset[role=group]").all():
        if not g.is_visible():
            continue
        items = g.locator("[role=radio], [role=checkbox]")
        if items.count() and not g.locator("[aria-checked=true]").count():
            open_.append((g.get_attribute("aria-label") or "").strip()[:120])
    return [q for q in open_ if q][:3]


def _next(ctx: Context, expected_path: str) -> None:
    ctx.check()
    page = ctx.page
    before = page.url
    button = page.locator("button:visible").filter(has_text=re.compile(r"^\s*Weiter\s*$")).last
    for _ in range(12):  # kurz warten, falls der Rechner noch prueft
        if button.get_attribute("aria-disabled") != "true" and button.is_enabled():
            break
        page.wait_for_timeout(250)
    else:
        questions = _open_questions(page)
        raise HukRunError(
            "needs_input",
            "Der Rechner stellt eine Pflichtfrage, die Zentriq noch nicht kennt: "
            + ("; ".join(f"„{q}“" for q in questions) if questions else "unbekannte Frage")
            + ". Bitte die Berechnung manuell auf huk.de fortsetzen.",
            {"questions": questions, "step": before.rsplit("/", 1)[-1]},
        )
    button.click()
    for _ in range(40):
        page.wait_for_timeout(250)
        if page.url != before:
            break
    _guard(ctx)
    if expected_path not in page.url:
        errors = _errors(page)
        raise HukRunError(
            "needs_input",
            "Der Rechner verlangt weitere Angaben: " + ("; ".join(errors) if errors else "unbekannte Pflichtfrage."),
            {"errors": errors, "step": before.rsplit("/", 1)[-1]},
        )
    page.wait_for_timeout(1200)


# --- Ablauf ------------------------------------------------------------------------------------


def _vehicle(ctx: Context, v: dict) -> str:
    page = ctx.page
    _fill(ctx, "hsn", v["hsn"])
    _fill(ctx, "tsn", v["tsn"])
    description = None
    for _ in range(24):
        description = page.evaluate(
            r"""() => { const l = [...document.querySelectorAll('main *')].find(e => e.childElementCount === 0 && e.textContent.trim() === 'Leistung');
                 if (!l) return null; let n = l; for (let i = 0; i < 4; i++) n = n.parentElement; return n.innerText.replace(/\s+/g, ' ').trim().slice(0, 160); }"""
        )
        if description:
            break
        page.wait_for_timeout(250)
    if not description:
        raise HukRunError("needs_input", "Zu dieser HSN/TSN hat der Rechner kein Fahrzeug gefunden. Bitte HSN und TSN prüfen.", {"field": "tsn"})
    _choose(ctx, "zulassung", r"bereits auf Sie zugelassen", v["zulassung"])
    _fill(ctx, "versicherungsbeginn", v["versicherungsbeginn"])
    _fill(ctx, "erstzulassung", v["erstzulassung"])
    _choose(ctx, "halter", r"Wer ist Fahrzeughalter", v["halter"])
    _fill(ctx, "plz", v["plz"])
    if v.get("zulassung") == "wechsel":
        _fill(ctx, "halterzulassung", v["halterzulassung"])
    _choose(ctx, "kennzeichen", r"Welches Kennzeichen", v["kennzeichen"])
    _choose(ctx, "finanzierung", r"Wie finanzieren Sie", v["finanzierung"])
    # Rueckpruefung der Textfelder (Eingabemasken koennen Zeichen verschlucken).
    for name in ("hsn", "tsn", "versicherungsbeginn", "erstzulassung", "plz") + (("halterzulassung",) if v.get("zulassung") == "wechsel" else ()):
        actual = page.locator(f"input[name='{name}']:visible").first.input_value().strip()
        if actual.upper() != v[name].upper():
            raise HukRunError("site_changed", f"Die Angabe „{FIELD_BY_KEY[name].question}“ wurde vom Rechner nicht übernommen.", {"field": name})
    return description


def _partner(ctx: Context, v: dict) -> None:
    _choose_privat_person(ctx)
    _fill(ctx, "geburtstag", v["geburtsdatum"])
    _choose(ctx, "beschaeftigung", r"Wo sind Sie beschäftigt", v["beschaeftigung"])
    _choose(ctx, "ingenieur", r"Sind Sie Ingenieur", v["ingenieur"])
    _choose(ctx, "wohneigentum", r"Haus- oder Wohneigentum", v["wohneigentum"])


def _choose_privat_person(ctx: Context) -> None:
    group = _group(ctx.page, r"Wer wird Versicherungsnehmer")
    if group is None:
        return
    item = group.locator("[role=radio]").filter(has_text=re.compile(r"^\s*Privatperson\s*$")).first
    if item.get_attribute("aria-checked") != "true":
        item.click()


def _usage(ctx: Context, v: dict) -> None:
    page = ctx.page
    _fill(ctx, "fahrleistungTotal", str(int(v["fahrleistung"]) // 1000), typed=True)
    shown = re.sub(r"\D", "", page.locator("input[name='fahrleistungTotal']").first.input_value())
    if shown != str(int(v["fahrleistung"])):
        raise HukRunError("site_changed", "Die Fahrleistung wurde vom Rechner nicht übernommen.", {"field": "fahrleistung"})
    _choose(ctx, "abstellplatz", r"regelmäßig abgestellt", v["abstellplatz"])
    _choose(ctx, "privat", r"überwiegend privat", v["privat"])
    labels = {o.label for o in FIELD_BY_KEY["fahrer"].options if o.value in v["fahrer"]}
    _choose_multi(ctx, r"Wer fährt mit dem Fahrzeug", labels)
    if any(d != "vn" for d in v["fahrer"]):
        _choose(ctx, "alle_ueber_25", r"mindestens 25 Jahre", v["alle_ueber_25"])
        if v.get("fahrer_ueber_70"):
            _choose(ctx, "fahrer_ueber_70", r"70 Jahre oder älter", v["fahrer_ueber_70"])


def _sf(ctx: Context, v: dict) -> None:
    _combobox(ctx, 0, v["sf_haftpflicht"], "sf_haftpflicht")
    _choose(ctx, "vollkasko_bisher", r"Vollkasko", v["vollkasko_bisher"])
    if v["vollkasko_bisher"] == "ja":
        _combobox(ctx, 1, v["sf_vollkasko"], "sf_vollkasko")


def _offer(ctx: Context, v: dict) -> dict:
    page = ctx.page
    coverage = FIELD_BY_KEY["deckung"]
    cov_label = next(o.label for o in coverage.options if o.value == v["deckung"])
    card = page.locator("main").get_by_text(cov_label, exact=True).first
    if card.count() == 0:
        raise HukRunError("site_changed", "Die Deckungsauswahl wurde nicht gefunden.", {"field": "deckung"})
    # Karte enthaelt "wählen" bzw. "gewählt".
    box = card.locator("xpath=ancestor::*[.//button][1]")
    button = box.locator("button").filter(has_text=re.compile(r"wählen|gewählt"))
    if button.count() and "gewählt" not in button.first.inner_text():
        button.first.click()
        page.wait_for_timeout(2500)
    deductible = v.get("selbstbeteiligung") or v.get("selbstbeteiligung_tk")
    deductible_shown = _select(ctx, "Selbstbeteiligung", deductible, "selbstbeteiligung") if deductible else None
    for key, label_part in (("zahlungsart", "Zahlungsart"), ("zahlungsweise", "Zahlungsweise")):
        wanted = next(o.label for o in FIELD_BY_KEY[key].options if o.value == v[key])
        _select(ctx, label_part, wanted, key)
    ctx.check()
    tariffs = page.evaluate(
        r"""() => {
          const out = [];
          for (const b of document.querySelectorAll('main button')) {
            const t = b.innerText.trim();
            if (t !== 'wählen' && !t.startsWith('gewählt')) continue;
            let card = b.parentElement;
            for (let i = 0; i < 4 && card && !/€/.test(card.innerText); i++) card = card.parentElement;
            if (!card) continue;
            const lines = card.innerText.split('\n').map(s => s.trim()).filter(Boolean);
            const priceLine = lines.find(l => /\d,\d{2}\s*€/.test(l));
            if (!priceLine) continue;
            const idx = lines.indexOf(priceLine);
            out.push({name: lines[idx - 1] || '', price: priceLine, period: lines[idx + 1] || '', selected: t.startsWith('gewählt'),
                      recommended: lines.includes('Empfohlen')});
          }
          return out;
        }"""
    )
    tariffs = [t for t in tariffs if PRICE.search(t["price"]) and t["name"] and "€" not in t["name"]]
    if not tariffs:
        raise HukRunError("no_result", "Der Rechner hat keinen Beitrag angezeigt. Es wurde kein Ergebnis gespeichert.")
    return {
        "coverage": cov_label,
        "deductible": deductible_shown,
        "payment_interval": v["zahlungsweise"],
        "payment_method": next(o.label for o in FIELD_BY_KEY["zahlungsart"].options if o.value == v["zahlungsart"]),
        "tariffs": [{"name": t["name"], "price": PRICE.search(t["price"]).group(1) + " €", "period": t["period"],
                     "selected": t["selected"], "recommended": t["recommended"]} for t in tariffs],
    }


def run(values: dict, *, timeout_s: int = 150, should_cancel=lambda: False, evidence_path: str | None = None,
        headless: bool = True, profile_dir: str | None = None) -> dict:
    """Fuehrt die Berechnung aus. profile_dir: leeres, nur fuer diesen Auftrag angelegtes
    Browserprofil (wird vom Aufrufer danach geloescht); ohne Angabe ein temporaeres Profil."""
    import shutil
    import tempfile

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    deadline = time.monotonic() + timeout_s
    own_profile = profile_dir is None
    profile_dir = profile_dir or tempfile.mkdtemp(prefix="huk-")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile_dir, headless=headless, args=["--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions"],
            locale="de-DE", timezone_id="Europe/Berlin", viewport={"width": 1366, "height": 1000},
            accept_downloads=False, service_workers="block",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            ctx = Context(page=page, deadline=deadline, should_cancel=should_cancel)
            response = page.goto(START_URL, wait_until="domcontentloaded", timeout=45000)
            if response is not None and response.status >= 400:
                raise HukRunError("blocked" if response.status in (403, 429) else "network",
                                  f"Der Rechner ist nicht erreichbar (HTTP {response.status}).", retryable=response.status >= 500)
            page.wait_for_timeout(2500)
            consent = page.get_by_role("button", name="Mit erforderlichen Einstellungen fortfahren")
            if consent.count():
                consent.first.click()
                page.wait_for_timeout(800)
            _guard(ctx)
            vehicle = _vehicle(ctx, values)
            _next(ctx, "/vertragspartner")
            _partner(ctx, values)
            _next(ctx, "/nutzung")
            _usage(ctx, values)
            _next(ctx, "/schadenfreiheitsklasse")
            _sf(ctx, values)
            _next(ctx, "/angebot")
            page.wait_for_timeout(3000)
            _guard(ctx)
            offer = _offer(ctx, values)
            if evidence_path:
                page.locator("main").first.screenshot(path=evidence_path)
            final_path = re.sub(r"^https?://[^/]+", "", page.url)
            if not final_path.endswith("/angebot"):
                raise HukRunError("site_changed", "Die Ergebnisseite konnte nicht eindeutig bestätigt werden.")
            return {"vehicle": vehicle, "page": final_path, **offer}
        except PlaywrightTimeout as exc:
            raise HukRunError("timeout", "Der Rechner hat nicht rechtzeitig reagiert.", retryable=True) from exc
        except PlaywrightError as exc:
            raise HukRunError("network", "Verbindung zum Rechner fehlgeschlagen.", retryable=True) from exc
        finally:
            try:
                context.close()
            finally:
                if own_profile:
                    shutil.rmtree(profile_dir, ignore_errors=True)
