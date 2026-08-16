import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db import Base
from app.main import (
    collect_due_reminders,
    create_reminder,
    delete_reminder,
    initialize_database,
    templates,
    timer_page,
    toggle_reminder,
    update_reminder,
)
from app.models import PushSubscription, Reminder, TimerSession, User
from app.push import send_reminder_notification


def test_reminder_minute_ui_uses_five_minute_presets_and_manual_input():
    html = templates.env.get_template("reminders.html").render(
        user=type("User", (), {"username": "user"})(),
        rows=[],
        weekday_labels=["月", "火", "水", "木", "金", "土", "日"],
        reminder_durations=[5, 10, 15, 30, 45, 60, 90, 120],
        static_version="test",
    )

    assert '<option value="55">55</option>' in html
    assert '<option value="custom">手動入力…</option>' in html
    assert '<option value="59">59</option>' not in html
    assert 'name="reminder_minute"' in html
    assert '<option value="-1">毎日</option>' in html
    assert 'name="notification_message"' in html


def test_existing_reminder_renders_prefilled_edit_form():
    reminder = SimpleNamespace(
        id=12,
        weekday=-1,
        minute_of_day=19 * 60 + 3,
        planned_seconds=2700,
        message="数学を始める",
        enabled=True,
    )
    html = templates.env.get_template("reminders.html").render(
        user=SimpleNamespace(username="user"),
        rows=[{"reminder": reminder, "weekday": "毎日", "time": "19:03", "minutes": 45}],
        weekday_labels=["月", "火", "水", "木", "金", "土", "日"],
        reminder_durations=[5, 10, 15, 30, 45, 60, 90, 120],
        static_version="test",
    )

    assert 'action="/reminders/12"' in html
    assert 'value="-1" selected' in html
    assert 'value="19" selected' in html
    assert 'value="custom" selected' in html
    assert 'value="45" selected' in html
    assert 'value="数学を始める"' in html


def test_due_reminder_is_collected_once_without_changing_active_timer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        reminder = Reminder(user_id=user.id, weekday=0, minute_of_day=9 * 60 + 30, planned_seconds=3600)
        timer = TimerSession(user_id=user.id, planned_seconds=1500, started_at=datetime.now(timezone.utc), status="running")
        db.add_all([reminder, timer])
        db.commit()

        now = datetime(2026, 8, 17, 0, 30, tzinfo=timezone.utc)  # 09:30 JST
        notifications = collect_due_reminders(db, now)
        db.commit()

        assert notifications == [{
            "user_id": user.id,
            "reminder_id": reminder.id,
            "planned_seconds": 3600,
            "message": None,
            "active": True,
            "occurrence": "2026-08-17",
        }]
        assert collect_due_reminders(db, now) == []
        db.refresh(timer)
        assert timer.status == "running"
        assert timer.planned_seconds == 1500


def test_reminder_can_be_created_toggled_and_deleted_by_owner():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = User(username="owner", password_hash="hash", role="user")
        other = User(username="other", password_hash="hash", role="user")
        db.add_all([owner, other])
        db.commit()

        response = create_reminder(2, 19, 3, 45, "数学を始める", owner, db)
        reminder = db.query(Reminder).one()
        assert response.headers["location"] == "/reminders"
        assert (reminder.weekday, reminder.minute_of_day, reminder.planned_seconds) == (2, 19 * 60 + 3, 2700)
        assert reminder.message == "数学を始める"

        reminder.last_notified_on = date(2026, 8, 18)
        db.commit()
        update_reminder(reminder.id, 2, 19, 3, 45, "理科を始める", owner, db)
        db.refresh(reminder)
        assert reminder.message == "理科を始める"
        assert reminder.last_notified_on == date(2026, 8, 18)

        update_reminder(reminder.id, -1, 20, 7, 60, "", owner, db)
        db.refresh(reminder)
        assert (reminder.weekday, reminder.minute_of_day, reminder.planned_seconds) == (-1, 20 * 60 + 7, 3600)
        assert reminder.message is None
        assert reminder.last_notified_on is None

        with pytest.raises(HTTPException) as denied_update:
            update_reminder(reminder.id, 1, 10, 0, 30, "", other, db)
        assert denied_update.value.status_code == 404

        toggle_reminder(reminder.id, owner, db)
        db.refresh(reminder)
        assert reminder.enabled is False

        with pytest.raises(HTTPException) as denied:
            delete_reminder(reminder.id, other, db)
        assert denied.value.status_code == 404

        delete_reminder(reminder.id, owner, db)
        assert db.get(Reminder, reminder.id) is None


def test_reminder_push_has_distinct_type_and_does_not_claim_timer_finished(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sent = []
    monkeypatch.setattr("app.push.webpush", lambda **kwargs: sent.append(kwargs))
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        db.add(PushSubscription(user_id=user.id, endpoint="https://push.example/1", p256dh="key", auth="auth"))
        db.commit()

        send_reminder_notification(db, user.id, 7, 3600, True, "2026-08-17")

    payload = json.loads(sent[0]["data"])
    assert payload["type"] == "reminder"
    assert payload["url"] == "/?reminder=7"
    assert payload["body"] == "現在のタイマーを続けてください"
    assert payload["tag"] == "reminder-7-2026-08-17"


def test_daily_reminder_is_due_on_any_weekday_and_uses_custom_message(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sent = []
    monkeypatch.setattr("app.push.webpush", lambda **kwargs: sent.append(kwargs))
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        reminder = Reminder(
            user_id=user.id,
            weekday=-1,
            minute_of_day=9 * 60 + 30,
            planned_seconds=1800,
            message="英単語の時間です",
        )
        db.add(reminder)
        db.flush()
        db.add(PushSubscription(user_id=user.id, endpoint="https://push.example/2", p256dh="key", auth="auth"))
        db.commit()

        notifications = collect_due_reminders(db, datetime(2026, 8, 18, 0, 30, tzinfo=timezone.utc))
        assert len(notifications) == 1
        send_reminder_notification(db, **notifications[0])

    payload = json.loads(sent[0]["data"])
    assert payload["body"] == "英単語の時間です"


def test_existing_sqlite_reminder_table_gets_message_column():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE reminders (id INTEGER PRIMARY KEY, user_id INTEGER)")

    initialize_database(engine)

    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(reminders)")}
    assert "message" in columns


def test_reminder_link_selects_duration_only_when_no_timer_is_active():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        reminder = Reminder(user_id=user.id, weekday=0, minute_of_day=600, planned_seconds=3600)
        db.add(reminder)
        db.commit()

        response = timer_page(request, reminder=reminder.id, user=user, db=db)
        assert response.context["state"] is None
        assert response.context["default_seconds"] == 3600
        assert response.context["reminder_minutes"] == 60

        timer = TimerSession(user_id=user.id, planned_seconds=1500, started_at=datetime.now(timezone.utc), status="running")
        db.add(timer)
        db.commit()

        response = timer_page(request, reminder=reminder.id, user=user, db=db)
        assert response.context["state"]["id"] == timer.id
        assert response.context["state"]["planned"] == 1500
        assert response.context["reminder_minutes"] is None
        db.refresh(timer)
        assert timer.status == "running"
