import pytest

from app.services.mailbox.phone import (
    extract_explicit_callback_phone,
    extract_phone_candidates,
    normalize_callback_phone,
    split_huk_phone,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0521 1234567", "+495211234567"),
        ("+49 521 1234567", "+495211234567"),
        ("0049 521 1234567", "+495211234567"),
        ("0170 1234567", "+491701234567"),
    ],
)
def test_normalize_callback_phone_accepts_valid_german_numbers(raw, expected):
    assert normalize_callback_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not a phone number",
        "+1 555 1234567",  # nicht deutsch
        "0521 1",  # zu kurz / ungueltig
    ],
)
def test_normalize_callback_phone_rejects_invalid_numbers(raw):
    assert normalize_callback_phone(raw) is None


def test_extract_phone_candidates_finds_multiple_numbers_in_text():
    text = "Rufen Sie mich unter 0521 1234567 zurück, alternativ 0170 7654321."
    candidates = extract_phone_candidates(text)
    assert candidates == ["+495211234567", "+491707654321"]


def test_extract_phone_candidates_ignores_text_without_numbers():
    assert extract_phone_candidates("Kein Anruf nötig.") == []
    assert extract_phone_candidates(None) == []


def test_extract_explicit_callback_phone_requires_callback_marker_nearby():
    transcript = "Meine Vertragsnummer ist 0521 1234567 falls Sie das brauchen."
    assert extract_explicit_callback_phone(transcript) is None


def test_extract_explicit_callback_phone_finds_number_near_marker():
    transcript = "Bitte rufen Sie mich zurück unter 0521 1234567, danke."
    assert extract_explicit_callback_phone(transcript) == "+495211234567"


def test_extract_explicit_callback_phone_returns_none_without_transcript():
    assert extract_explicit_callback_phone(None) is None
    assert extract_explicit_callback_phone("") is None


def test_split_huk_phone_splits_area_code_and_subscriber_number():
    area_code, subscriber_number = split_huk_phone("+495211234567")
    assert area_code == "0521"
    assert subscriber_number == "1234567"


def test_split_huk_phone_raises_for_invalid_number():
    with pytest.raises(ValueError):
        split_huk_phone("not-a-number")
