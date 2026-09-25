import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import current_app

from app.services.mailbox.phone import split_huk_phone
from app.services.mailbox.schemas import HUK_DAMAGE_TYPES

BERLIN = ZoneInfo("Europe/Berlin")


class HukAutomationError(RuntimeError):
    pass


class CaptchaDetectedError(HukAutomationError):
    pass


def is_huk_service_open(now: datetime | None = None) -> bool:
    local_now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    return local_now.weekday() < 5 and time(8, 0) <= local_now.time() < time(18, 0)


def seconds_until_huk_service_open(now: datetime | None = None) -> int:
    local_now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    candidate = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
    if local_now.weekday() < 5 and local_now < candidate:
        return max(60, int((candidate - local_now).total_seconds()))
    candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(60, int((candidate - local_now).total_seconds()))


def build_huk_form_data(callback_phone: str, damage_type: str) -> dict[str, str]:
    if damage_type not in HUK_DAMAGE_TYPES or damage_type == "Sonstiges":
        raise ValueError("Die Schadenart ist für eine automatische Übermittlung nicht eindeutig.")
    area_code, subscriber_number = split_huk_phone(callback_phone)
    return {
        "area_code": area_code,
        "subscriber_number": subscriber_number,
        "concern": "Schadenanliegen",
        "damage_type": damage_type,
    }


def run_huk_automation(callback_phone: str, damage_type: str, *, submit: bool) -> dict:
    from playwright.sync_api import sync_playwright

    form_data = build_huk_form_data(callback_phone, damage_type)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=current_app.config["HUK_BROWSER_HEADLESS"])
        try:
            page = browser.new_page()
            page.set_default_timeout(current_app.config["HUK_BROWSER_TIMEOUT_MS"])
            return fill_huk_form(page, form_data, submit=submit)
        finally:
            browser.close()


def fill_huk_form(page, form_data: dict[str, str], *, submit: bool) -> dict:
    page.goto(current_app.config["HUK_FORM_URL"], wait_until="domcontentloaded")
    _accept_required_cookies(page)
    _raise_for_captcha(page)

    page.get_by_role("textbox", name="Vorwahl", exact=True).fill(form_data["area_code"])
    page.get_by_role("textbox", name="Rufnummer", exact=True).fill(form_data["subscriber_number"])
    page.get_by_role("radio", name="Schadenanliegen", exact=True).check()
    damage_select = page.get_by_role(
        "combobox", name="Um welches Schadenanliegen geht es?", exact=True
    )
    damage_select.click()
    page.get_by_role("menuitem", name=form_data["damage_type"], exact=True).click()
    _raise_for_captcha(page)

    result = {
        "prepared": True,
        "submitted": False,
        "form_url": page.url,
        "concern": form_data["concern"],
        "damage_type": form_data["damage_type"],
    }
    if not submit:
        return result

    page.get_by_role("button", name="Rückruf ausführen", exact=True).click()
    _raise_for_captcha(page)
    confirmation = page.get_by_text(
        re.compile(r"Vielen Dank|Wir rufen Sie|Rückruf (?:wurde|ist) angefordert", re.IGNORECASE)
    ).first
    try:
        confirmation.wait_for(state="visible", timeout=current_app.config["HUK_BROWSER_TIMEOUT_MS"])
    except Exception as exc:
        raise HukAutomationError("Das HUK-Formular hat keine eindeutige Bestätigung geliefert.") from exc
    result.update(
        submitted=True,
        confirmation=confirmation.inner_text(),
        confirmation_url=page.url,
    )
    return result


def _accept_required_cookies(page) -> None:
    button = page.get_by_role("button", name="Mit erforderlichen Einstellungen fortfahren", exact=True)
    try:
        if button.is_visible():
            button.click()
    except Exception:
        return


def _raise_for_captcha(page) -> None:
    selectors = (
        'iframe[src*="captcha" i]',
        '[class*="captcha" i]',
        '[id*="captcha" i]',
    )
    for selector in selectors:
        try:
            if page.locator(selector).filter(visible=True).count():
                raise CaptchaDetectedError("Captcha erkannt; automatische Verarbeitung abgebrochen.")
        except CaptchaDetectedError:
            raise
        except Exception:
            continue

