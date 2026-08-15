from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.main import TZ, activity_summary, format_duration_ja
from app.models import TimerSession, User


def test_activity_is_grouped_by_day_for_one_user():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 14, 12, tzinfo=TZ)
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        other = User(username="other", password_hash="hash", role="user")
        db.add_all([user, other])
        db.flush()
        ended_at = datetime(2026, 8, 14, 1, tzinfo=timezone.utc)
        db.add_all([
            TimerSession(user_id=user.id, planned_seconds=1500, worked_seconds=1500, ended_at=ended_at, status="completed"),
            TimerSession(user_id=user.id, planned_seconds=600, worked_seconds=300, ended_at=ended_at, status="stopped"),
            TimerSession(user_id=other.id, planned_seconds=3600, worked_seconds=3600, ended_at=ended_at, status="completed"),
        ])
        db.commit()

        summary = activity_summary(db, user.id, now)

    assert summary["today_minutes"] == 30
    assert summary["today_seconds"] == 1800
    assert summary["week_minutes"] == 30
    assert summary["month_minutes"] == 30
    assert summary["month_seconds"] == 1800
    assert summary["week"][4]["minutes"] == 30
    assert summary["month_completed"] == 1
    assert summary["month_stopped"] == 1
    assert summary["month_weeks"][1][0]["has_activity"] is False
    assert next(day for week in summary["month_weeks"] for day in week if day["date"] == "2026-08-14")["has_activity"] is True
    assert summary["details"]["2026-08-14"]["seconds"] == 1800
    assert summary["details"]["2026-08-14"]["ticks"][0]["label"] == "08"
    assert summary["details"]["2026-08-14"]["ticks"][-1]["label"] == "13"
    assert len(summary["details"]["2026-08-14"]["hourly"]) == 1
    assert summary["details"]["2026-08-14"]["hourly"][0]["seconds"] == 1800


def test_report_duration_keeps_seconds():
    assert format_duration_ja(0) == "0秒"
    assert format_duration_ja(2043) == "34分3秒"
    assert format_duration_ja(7500) == "2時間5分"


def test_week_chart_hides_activity_under_one_minute():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 14, 12, tzinfo=TZ)
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        db.add_all([
            TimerSession(
                user_id=user.id,
                planned_seconds=5,
                worked_seconds=5,
                ended_at=datetime(2026, 8, 14, 1, tzinfo=timezone.utc),
                status="completed",
            ),
            TimerSession(
                user_id=user.id,
                planned_seconds=60,
                worked_seconds=60,
                ended_at=datetime(2026, 8, 13, 1, tzinfo=timezone.utc),
                status="completed",
            ),
        ])
        db.commit()

        summary = activity_summary(db, user.id, now)

    assert summary["week"][4]["minutes"] == 0
    assert summary["week"][4]["percent"] == 0
    assert summary["week"][3]["minutes"] == 1
    assert summary["week"][3]["percent"] == 100


def test_activity_can_display_another_month():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 14, 12, tzinfo=TZ)
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        db.add(TimerSession(
            user_id=user.id,
            planned_seconds=1200,
            worked_seconds=1200,
            ended_at=datetime(2026, 7, 10, 1, tzinfo=timezone.utc),
            status="completed",
        ))
        db.commit()

        summary = activity_summary(db, user.id, now, target_month=datetime(2026, 7, 1).date())

    assert summary["month_label"] == "2026年7月"
    assert summary["month_minutes"] == 20
    assert summary["previous_month"] == "2026-06"
    assert summary["next_month"] == "2026-08"
    assert summary["selected_date"] == "2026-07-01"
