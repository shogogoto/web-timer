from datetime import datetime, timedelta, timezone

from app.models import TimerSession
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
