"""Gesetzliche Feiertage je Bundesland (ohne externe Bibliothek, ohne KI).

Grundlage: bundesweite Feiertage plus landesweite Feiertage der 16 Bundeslaender. Feiertage, die
nur in einzelnen Gemeinden gelten (z. B. Mariae Himmelfahrt in Teilen Bayerns, Fronleichnam in
Teilen Sachsens/Thueringens), sind bewusst nicht enthalten. An einem Feiertag ist das Tagessoll
0 Stunden - er zaehlt weder als Arbeits- noch als Krankheits- oder Urlaubstag."""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

STATES = {
    "BW": "Baden-Württemberg",
    "BY": "Bayern",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen",
    "RP": "Rheinland-Pfalz",
    "SL": "Saarland",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}
DEFAULT_STATE = "NW"


def easter_sunday(year: int) -> date:
    """Ostersonntag (gregorianisch, Gauss/Anonymer Algorithmus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _repentance_day(year: int) -> date:
    """Buss- und Bettag: Mittwoch vor dem 23. November."""
    day = date(year, 11, 22)
    while day.weekday() != 2:
        day -= timedelta(days=1)
    return day


@lru_cache(maxsize=256)
def holidays_for(year: int, state: str | None) -> dict[date, str]:
    state = state if state in STATES else DEFAULT_STATE
    easter = easter_sunday(year)
    days = {
        date(year, 1, 1): "Neujahr",
        easter - timedelta(days=2): "Karfreitag",
        easter + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        easter + timedelta(days=39): "Christi Himmelfahrt",
        easter + timedelta(days=50): "Pfingstmontag",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
    }
    if state in ("BW", "BY", "ST"):
        days[date(year, 1, 6)] = "Heilige Drei Könige"
    if (state == "BE" and year >= 2019) or (state == "MV" and year >= 2023):
        days[date(year, 3, 8)] = "Internationaler Frauentag"
    if state == "BB":
        days[easter] = "Ostersonntag"
        days[easter + timedelta(days=49)] = "Pfingstsonntag"
    if state in ("BW", "BY", "HE", "NW", "RP", "SL"):
        days[easter + timedelta(days=60)] = "Fronleichnam"
    if state == "SL":
        days[date(year, 8, 15)] = "Mariä Himmelfahrt"
    if state == "TH" and year >= 2019:
        days[date(year, 9, 20)] = "Weltkindertag"
    if (state in ("BB", "MV", "SN", "ST", "TH") or (state in ("HB", "HH", "NI", "SH") and year >= 2018)
            or year == 2017):
        days[date(year, 10, 31)] = "Reformationstag"
    if state in ("BW", "BY", "NW", "RP", "SL"):
        days[date(year, 11, 1)] = "Allerheiligen"
    if state == "SN":
        days[_repentance_day(year)] = "Buß- und Bettag"
    return days


def holidays_between(start: date, end: date, state: str | None) -> dict[date, str]:
    result: dict[date, str] = {}
    for year in range(start.year, end.year + 1):
        result.update({day: name for day, name in holidays_for(year, state).items() if start <= day <= end})
    return result
