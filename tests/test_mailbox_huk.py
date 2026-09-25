from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.mailbox.huk import (
    build_huk_form_data,
    is_huk_service_open,
    seconds_until_huk_service_open,
)

BERLIN = ZoneInfo("Europe/Berlin")


def test_build_huk_form_data_splits_phone_and_sets_fixed_concern():
    data = build_huk_form_data("+495211234567", "Kfz-Versicherung")
    assert data == {
        "area_code": "0521",
        "subscriber_number": "1234567",
        "concern": "Schadenanliegen",
        "damage_type": "Kfz-Versicherung",
    }


def test_build_huk_form_data_rejects_sonstiges():
    with pytest.raises(ValueError):
        build_huk_form_data("+495211234567", "Sonstiges")


def test_build_huk_form_data_rejects_unknown_damage_type():
    with pytest.raises(ValueError):
        build_huk_form_data("+495211234567", "Erfundene Schadenart")


def test_is_huk_service_open_during_weekday_business_hours():
    monday_morning = datetime(2026, 9, 21, 9, 0, tzinfo=BERLIN)
    assert monday_morning.weekday() == 0
    assert is_huk_service_open(monday_morning) is True


def test_is_huk_service_open_before_opening():
    monday_early = datetime(2026, 9, 21, 7, 59, tzinfo=BERLIN)
    assert is_huk_service_open(monday_early) is False


def test_is_huk_service_open_at_exact_close_time():
    monday_close = datetime(2026, 9, 21, 18, 0, tzinfo=BERLIN)
    assert is_huk_service_open(monday_close) is False


def test_is_huk_service_open_closed_on_weekend():
    saturday_noon = datetime(2026, 9, 26, 12, 0, tzinfo=BERLIN)
    sunday_noon = datetime(2026, 9, 27, 12, 0, tzinfo=BERLIN)
    assert saturday_noon.weekday() == 5
    assert sunday_noon.weekday() == 6
    assert is_huk_service_open(saturday_noon) is False
    assert is_huk_service_open(sunday_noon) is False


def test_seconds_until_huk_service_open_same_day_before_opening():
    monday_early = datetime(2026, 9, 21, 6, 0, tzinfo=BERLIN)
    seconds = seconds_until_huk_service_open(monday_early)
    assert seconds == 2 * 60 * 60


def test_seconds_until_huk_service_open_skips_weekend():
    friday_evening = datetime(2026, 9, 25, 19, 0, tzinfo=BERLIN)
    assert friday_evening.weekday() == 4
    seconds = seconds_until_huk_service_open(friday_evening)
    target = friday_evening + timedelta(seconds=seconds)
    assert target.weekday() == 0
    assert target.hour == 8
