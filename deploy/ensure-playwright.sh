#!/usr/bin/env bash
# Stellt sicher, dass Playwright-Chromium (fuer die HUK-Rueckruf-Automation) installiert und
# startfaehig ist. Idempotent - wird von deploy/install.sh und deploy/update.sh aufgerufen.
#
# - Browser-Download nur, wenn fuer die installierte Playwright-Version noch nicht vorhanden
#   (nach einem Playwright-Update aendert sich die benoetigte Chromium-Revision).
# - System-Bibliotheken (apt) nur, wenn der Browser ohne sie nicht startet.
# - Die Pruefung startet Chromium headless mit einer leeren Seite: KEIN Netzwerkzugriff,
#   insbesondere kein Aufruf des HUK-Formulars.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

PYTHON="$APP_DIR/.venv/bin/python"
PLAYWRIGHT="$APP_DIR/.venv/bin/playwright"

if [ ! -x "$PLAYWRIGHT" ]; then
    echo "    FEHLER: $PLAYWRIGHT fehlt (requirements.txt installiert?)." >&2
    exit 1
fi

check_chromium() {
    "$PYTHON" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content("<p>ok</p>")
    assert page.inner_text("p") == "ok"
    print(f"    Chromium {browser.version} startet headless.")
    browser.close()
PY
}

echo "    Installiere Playwright-Chromium (falls noch nicht vorhanden)..."
"$PLAYWRIGHT" install chromium

if ! check_chromium; then
    echo "    Chromium startet nicht - installiere System-Abhaengigkeiten..."
    "$PLAYWRIGHT" install-deps chromium
    check_chromium
fi
