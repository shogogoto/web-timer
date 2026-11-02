from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest
from fastapi import HTTPException
from datetime import datetime, timedelta, timezone

from app.db import Base
from app.main import last_planned_seconds, save_goals, activity_summary, initialize_database
from app.models import TimerSession, User


def test_last_planned_seconds_is_kept_per_user():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first = User(username="first", password_hash="hash", role="user")
        second = User(username="second", password_hash="hash", role="user")
        db.add_all([first, second])
        db.flush()
        db.add_all([
            TimerSession(user_id=first.id, planned_seconds=1500, status="completed"),
            TimerSession(user_id=second.id, planned_seconds=3600, status="completed"),
            TimerSession(user_id=first.id, planned_seconds=2400, status="completed"),
        ])
        db.commit()

        assert last_planned_seconds(db, first.id) == 2400
        assert last_planned_seconds(db, second.id) == 3600


def test_debug_timer_is_kept_as_default():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        db.add_all([
            TimerSession(user_id=user.id, planned_seconds=1500, status="completed"),
            TimerSession(user_id=user.id, planned_seconds=5, status="completed"),
        ])
        db.commit()

        assert last_planned_seconds(db, user.id, allow_short=True) == 5


def test_goals_are_saved_per_user_and_validated():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first = User(username="first", password_hash="!", role="user")
        other = User(username="other", password_hash="!", role="user")
        db.add_all([first, other])
        db.commit()
        save_goals(1, 30, 3, first, db)
        assert first.daily_goal_minutes == 90
        assert first.weekly_goal_days == 3
        assert other.daily_goal_minutes == 0
        for values in [(24, 1, 5), (-1, 0, 5), (0, 60, 5), (1, 0, 8)]:
            with pytest.raises(HTTPException):
                save_goals(*values, first, db)


def test_daily_and_weekly_goal_achievement_at_month_boundary():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    with Session(engine) as db:
        user = User(username="user", password_hash="!", daily_goal_minutes=60, weekly_goal_days=2)
        db.add(user)
        db.flush()
        for day, seconds in [(30, 3600), (31, 3600)]:
            end = datetime(2026, 10, day, 12, tzinfo=timezone.utc)
            db.add(TimerSession(user_id=user.id, planned_seconds=3600, worked_seconds=seconds, ended_at=end, status="completed"))
        db.commit()
        summary = activity_summary(db, user.id, now, target_month=now.date(), target_week=now.date() - timedelta(days=7))
        assert summary["achieved_days"] == 2
        assert summary["week_goal_achieved"] is True
        boundary_week = summary["month_weeks"][0]
        assert boundary_week[0]["week_goal_achieved"] is True
        assert next(day for day in boundary_week if day["date"] == "2026-10-31")["goal_achieved"] is True
        assert next(day for day in boundary_week if day["date"] == "2026-11-01")["goal_achieved"] is False
        save_goals(0, 0, 2, user, db)
        disabled = activity_summary(db, user.id, now)
        assert not disabled["week_goal_achieved"]
        assert not any(day["goal_achieved"] for day in disabled["week"])


def test_goal_columns_are_added_to_existing_sqlite_users():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO users (id) VALUES (1)")
    initialize_database(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT daily_goal_minutes, weekly_goal_days FROM users").fetchone() == (0, 5)
