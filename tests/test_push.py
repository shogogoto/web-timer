import pytest

from app.push import normalize_vapid_subject, send_pause_timeout_notification, send_pause_warning_notification


def test_vapid_subject_adds_mailto_to_plain_email():
    assert normalize_vapid_subject("owner@example.com") == "mailto:owner@example.com"


def test_vapid_subject_accepts_supported_uris():
    assert normalize_vapid_subject("mailto:owner@example.com") == "mailto:owner@example.com"
    assert normalize_vapid_subject("https://example.com/contact") == "https://example.com/contact"


def test_vapid_subject_rejects_invalid_value():
    with pytest.raises(ValueError, match="VAPID_SUBJECT"):
        normalize_vapid_subject("owner")


def test_pause_push_notifications_have_distinct_types(monkeypatch):
    sent = []
    monkeypatch.setattr("app.push.send_push_notification", lambda db, user_id, payload, ttl: sent.append((user_id, payload, ttl)))

    send_pause_warning_notification(object(), 7, 11, 5)
    send_pause_timeout_notification(object(), 7, 11)

    assert sent[0][1]["type"] == "pause-warning"
    assert "5分" in sent[0][1]["title"]
    assert sent[1][1]["type"] == "pause-timeout"
    assert sent[0][1]["tag"] != sent[1][1]["tag"]
