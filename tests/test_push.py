import pytest

from app.push import normalize_vapid_subject


def test_vapid_subject_adds_mailto_to_plain_email():
    assert normalize_vapid_subject("owner@example.com") == "mailto:owner@example.com"


def test_vapid_subject_accepts_supported_uris():
    assert normalize_vapid_subject("mailto:owner@example.com") == "mailto:owner@example.com"
    assert normalize_vapid_subject("https://example.com/contact") == "https://example.com/contact"


def test_vapid_subject_rejects_invalid_value():
    with pytest.raises(ValueError, match="VAPID_SUBJECT"):
        normalize_vapid_subject("owner")
