from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.main import last_planned_seconds
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
