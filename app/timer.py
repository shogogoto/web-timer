from datetime import datetime, timezone

from .models import TimerSession


def aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def worked_so_far(session: TimerSession, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    started_at = aware(session.started_at)
    if not started_at:
        return 0
    end = aware(session.ended_at) or aware(session.pause_started_at) or now
    elapsed = int((end - started_at).total_seconds()) - session.paused_seconds
    return max(0, min(session.planned_seconds, elapsed))


def remaining_seconds(session: TimerSession, now: datetime | None = None) -> int:
    return max(0, session.planned_seconds - worked_so_far(session, now))

