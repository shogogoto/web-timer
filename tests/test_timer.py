from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.main import close_work_segment, open_work_segment, pause, resume, set_timer
from app.models import TimerSession, User, WorkSegment
from app.timer import worked_so_far


def test_pause_time_is_not_worked_time():
    start = datetime(2026, 8, 14, tzinfo=timezone.utc)
    session = TimerSession(planned_seconds=2400, started_at=start, pause_started_at=start + timedelta(minutes=18), paused_seconds=0, status="paused")
    assert worked_so_far(session, start + timedelta(minutes=30)) == 18 * 60


def test_resumed_pause_time_is_excluded():
    start = datetime(2026, 8, 14, tzinfo=timezone.utc)
    session = TimerSession(planned_seconds=2400, started_at=start, paused_seconds=10 * 60, status="running")
    assert worked_so_far(session, start + timedelta(minutes=30)) == 20 * 60


def test_work_is_capped_at_planned_duration():
    start = datetime(2026, 8, 14, tzinfo=timezone.utc)
    session = TimerSession(planned_seconds=1500, started_at=start, paused_seconds=0, status="running")
    assert worked_so_far(session, start + timedelta(minutes=30)) == 1500


def test_work_segments_open_and_close_around_pause():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    start = datetime(2026, 8, 14, tzinfo=timezone.utc)
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.flush()
        session = TimerSession(user_id=user.id, planned_seconds=2400, started_at=start, status="running")
        db.add(session)
        db.flush()

        open_work_segment(db, session, start)
        db.flush()
        close_work_segment(db, session, start + timedelta(minutes=18))
        open_work_segment(db, session, start + timedelta(minutes=30))
        db.flush()
        close_work_segment(db, session, start + timedelta(minutes=50), target_worked_seconds=30 * 60)
        db.commit()

        segments = db.query(WorkSegment).order_by(WorkSegment.started_at).all()

    assert len(segments) == 2
    assert int((segments[0].ended_at - segments[0].started_at).total_seconds()) == 18 * 60
    assert int((segments[1].ended_at - segments[1].started_at).total_seconds()) == 12 * 60


def test_timer_api_records_segments_on_start_pause_and_resume():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="user", password_hash="hash", role="user")
        db.add(user)
        db.commit()

        result = set_timer(300, user, db)
        session = db.get(TimerSession, result["id"])
        segments = db.query(WorkSegment).filter_by(session_id=session.id).all()
        assert len(segments) == 1
        assert segments[0].ended_at is None

        pause(session.id, user, db)
        db.refresh(segments[0])
        assert segments[0].ended_at is not None

        resume(session.id, user, db)
        segments = db.query(WorkSegment).filter_by(session_id=session.id).order_by(WorkSegment.id).all()
        assert len(segments) == 2
        assert segments[1].ended_at is None
