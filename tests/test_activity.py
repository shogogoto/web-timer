from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.main import TZ, activity_summary
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
    assert summary["week_minutes"] == 30
    assert summary["month_minutes"] == 30
    assert summary["week"][4]["minutes"] == 30
    assert summary["month_completed"] == 1
    assert summary["month_stopped"] == 1
    assert summary["details"]["2026-08-14"]["seconds"] == 1800
