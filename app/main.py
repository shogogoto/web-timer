import asyncio
import calendar
import hashlib
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import admin_user, bootstrap_admin, current_user, hash_password, verify_password
from .db import Base, SessionLocal, engine, get_db
from .models import PushSubscription, Reminder, TimerSession, User, WorkSegment
from .push import (
    application_server_key,
    ensure_vapid_key,
    send_reminder_notification_async,
    send_timer_notification_async,
)
from .timer import remaining_seconds, worked_so_far

BASE_DIR = Path(__file__).parent
TZ = ZoneInfo(os.getenv("TZ", "Asia/Tokyo"))
ALLOW_SHORT_TIMERS = os.getenv("ALLOW_SHORT_TIMERS", "false").lower() == "true"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Path("data").mkdir(exist_ok=True)
    initialize_database()
    with SessionLocal() as db:
        bootstrap_admin(db)
    ensure_vapid_key()
    notifier = asyncio.create_task(notification_loop())
    try:
        yield
    finally:
        notifier.cancel()


def initialize_database(database_engine=engine) -> None:
    Base.metadata.create_all(database_engine)
    if database_engine.dialect.name != "sqlite":
        return
    with database_engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(reminders)")}
        if "message" not in columns:
            connection.exec_driver_sql("ALTER TABLE reminders ADD COLUMN message VARCHAR(120)")


async def notification_loop() -> None:
    while True:
        await asyncio.sleep(1)
        completed_user_ids: list[int] = []
        reminder_notifications: list[dict] = []
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            sessions = db.scalars(select(TimerSession).where(TimerSession.status == "running")).all()
            for session in sessions:
                if worked_so_far(session, now) >= session.planned_seconds:
                    session.worked_seconds = session.planned_seconds
                    session.ended_at = now
                    session.status = "completed"
                    close_work_segment(db, session, now, session.planned_seconds)
                    completed_user_ids.append(session.user_id)
            reminder_notifications = collect_due_reminders(db, now)
            db.commit()
        for user_id in completed_user_ids:
            await send_timer_notification_async(SessionLocal, user_id)
        for notification in reminder_notifications:
            await send_reminder_notification_async(SessionLocal, notification)


app = FastAPI(title="Focus Timer", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "change-me-in-production"),
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
STATIC_VERSION = hashlib.sha256(b"".join(
    path.read_bytes() for path in sorted((BASE_DIR / "static").iterdir()) if path.is_file()
)).hexdigest()[:12]
templates.env.globals["static_version"] = STATIC_VERSION


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def format_duration_ja(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}時間")
    if minutes:
        parts.append(f"{minutes}分")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def active_session(db: Session, user_id: int) -> TimerSession | None:
    return db.scalars(
        select(TimerSession)
        .where(TimerSession.user_id == user_id, TimerSession.status.in_(["ready", "running", "paused"]))
        .order_by(TimerSession.id.desc())
    ).first()


def open_work_segment(db: Session, session: TimerSession, started_at: datetime) -> None:
    existing = db.scalars(select(WorkSegment).where(
        WorkSegment.session_id == session.id,
        WorkSegment.ended_at.is_(None),
    )).first()
    if existing is None:
        db.add(WorkSegment(session_id=session.id, started_at=started_at))


def segment_seconds(segment: WorkSegment) -> int:
    if segment.ended_at is None:
        return 0
    started_at = segment.started_at
    ended_at = segment.ended_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)
    return max(0, int((ended_at - started_at).total_seconds()))


def close_work_segment(
    db: Session,
    session: TimerSession,
    ended_at: datetime,
    target_worked_seconds: int | None = None,
) -> None:
    segment = db.scalars(select(WorkSegment).where(
        WorkSegment.session_id == session.id,
        WorkSegment.ended_at.is_(None),
    ).order_by(WorkSegment.id.desc())).first()
    if segment is None:
        return
    if target_worked_seconds is not None:
        closed_segments = db.scalars(select(WorkSegment).where(
            WorkSegment.session_id == session.id,
            WorkSegment.id != segment.id,
            WorkSegment.ended_at.is_not(None),
        )).all()
        remaining = max(0, target_worked_seconds - sum(segment_seconds(item) for item in closed_segments))
        started_at = segment.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        ended_at = min(ended_at, started_at + timedelta(seconds=remaining))
    segment.ended_at = ended_at


def collect_due_reminders(db: Session, now: datetime) -> list[dict]:
    local_now = now.astimezone(TZ)
    today = local_now.date()
    reminders = db.scalars(select(Reminder).where(
        Reminder.enabled.is_(True),
        or_(Reminder.weekday == -1, Reminder.weekday == local_now.weekday()),
        Reminder.minute_of_day == local_now.hour * 60 + local_now.minute,
        or_(Reminder.last_notified_on.is_(None), Reminder.last_notified_on != today),
    )).all()
    notifications = []
    for reminder in reminders:
        reminder.last_notified_on = today
        notifications.append({
            "user_id": reminder.user_id,
            "reminder_id": reminder.id,
            "planned_seconds": reminder.planned_seconds,
            "message": reminder.message,
            "active": active_session(db, reminder.user_id) is not None,
            "occurrence": today.isoformat(),
        })
    return notifications


def last_planned_seconds(db: Session, user_id: int, allow_short: bool = False) -> int:
    statement = select(TimerSession.planned_seconds).where(TimerSession.user_id == user_id)
    if not allow_short:
        statement = statement.where(TimerSession.planned_seconds >= 300)
    planned_seconds = db.scalar(statement.order_by(TimerSession.id.desc()).limit(1))
    return planned_seconds or 40 * 60


def totals(db: Session, user_id: int, now: datetime | None = None) -> dict[str, int]:
    summary = activity_summary(db, user_id, now=now)
    today_seconds = summary["today_seconds"]
    week_seconds = summary["week_seconds"]
    return {
        "today": today_seconds // 60,
        "week": week_seconds // 60,
        "today_seconds": today_seconds,
        "week_seconds": week_seconds,
    }


def activity_summary(
    db: Session,
    user_id: int,
    now: datetime | None = None,
    target_month=None,
    target_week=None,
) -> dict:
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)
    today = now.date()
    current_week_start = today - timedelta(days=today.weekday())
    viewed_week_date = target_week or today
    week_start = viewed_week_date - timedelta(days=viewed_week_date.weekday())
    week_end = week_start + timedelta(days=6)
    previous_week = week_start - timedelta(days=7)
    next_week = week_start + timedelta(days=7)
    month_start = (target_month or today).replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    previous_month = (month_start - timedelta(days=1)).replace(day=1)
    today_start_utc = datetime.combine(today, datetime.min.time(), TZ).astimezone(timezone.utc)
    today_end_utc = datetime.combine(today + timedelta(days=1), datetime.min.time(), TZ).astimezone(timezone.utc)
    week_start_utc = datetime.combine(week_start, datetime.min.time(), TZ).astimezone(timezone.utc)
    week_end_utc = datetime.combine(week_start + timedelta(days=7), datetime.min.time(), TZ).astimezone(timezone.utc)
    month_start_utc = datetime.combine(month_start, datetime.min.time(), TZ).astimezone(timezone.utc)
    month_end_utc = datetime.combine(next_month, datetime.min.time(), TZ).astimezone(timezone.utc)
    ranges = sorted([
        (today_start_utc, today_end_utc),
        (week_start_utc, week_end_utc),
        (month_start_utc, month_end_utc),
    ])
    merged_ranges: list[tuple[datetime, datetime]] = []
    for range_start, range_end in ranges:
        if merged_ranges and range_start <= merged_ranges[-1][1]:
            merged_ranges[-1] = (merged_ranges[-1][0], max(merged_ranges[-1][1], range_end))
        else:
            merged_ranges.append((range_start, range_end))
    period_filter = or_(*(
        and_(TimerSession.ended_at >= range_start, TimerSession.ended_at < range_end)
        for range_start, range_end in merged_ranges
    ))
    sessions = db.scalars(select(TimerSession).where(
        TimerSession.user_id == user_id,
        period_filter,
        TimerSession.status.in_(["completed", "stopped"]),
    )).all()
    daily_seconds: dict = {}
    daily_details: dict = {}

    def detail_for(day) -> dict:
        return daily_details.setdefault(day.isoformat(), {
            "seconds": 0,
            "completed": 0,
            "stopped": 0,
            "sessions": set(),
            "hourly": {},
        })

    for session in sessions:
        ended_at = session.ended_at
        if ended_at is None:
            continue
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)
        day = ended_at.astimezone(TZ).date()
        detail = detail_for(day)
        detail[session.status] += 1

    def add_interval(started_at: datetime, ended_at: datetime, status: str, session_id: int) -> None:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)
        if ended_at <= started_at:
            return
        local_start = started_at.astimezone(TZ)
        local_end = ended_at.astimezone(TZ)
        chunks = []
        cursor = local_start
        while cursor < local_end:
            hour_end = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            chunk_end = min(local_end, hour_end)
            chunks.append([cursor.date(), cursor.hour, max(0, int((chunk_end - cursor).total_seconds()))])
            cursor = chunk_end
        expected_seconds = max(0, int((ended_at - started_at).total_seconds()))
        if chunks:
            chunks[-1][2] += expected_seconds - sum(chunk[2] for chunk in chunks)
        for day, hour, seconds in chunks:
            if seconds <= 0:
                continue
            daily_seconds[day] = daily_seconds.get(day, 0) + seconds
            detail = detail_for(day)
            detail["seconds"] += seconds
            detail["sessions"].add(session_id)
            bucket = detail["hourly"].setdefault(hour, {
                "hour": hour,
                "seconds": 0,
                "completed_sessions": set(),
                "stopped_sessions": set(),
            })
            bucket["seconds"] += seconds
            bucket[f"{status}_sessions"].add(session_id)

    segment_period_filter = or_(*(
        and_(WorkSegment.started_at < range_end, WorkSegment.ended_at > range_start)
        for range_start, range_end in merged_ranges
    ))
    segment_rows = db.execute(select(WorkSegment, TimerSession.status).join(
        TimerSession, TimerSession.id == WorkSegment.session_id,
    ).where(
        TimerSession.user_id == user_id,
        TimerSession.status.in_(["completed", "stopped"]),
        WorkSegment.ended_at.is_not(None),
        segment_period_filter,
    )).all()
    for segment, status in segment_rows:
        segment_start = segment.started_at
        segment_end = segment.ended_at
        if segment_start.tzinfo is None:
            segment_start = segment_start.replace(tzinfo=timezone.utc)
        if segment_end.tzinfo is None:
            segment_end = segment_end.replace(tzinfo=timezone.utc)
        for range_start, range_end in merged_ranges:
            clipped_start = max(segment_start, range_start)
            clipped_end = min(segment_end, range_end)
            if clipped_start < clipped_end:
                add_interval(clipped_start, clipped_end, status, segment.session_id)

    session_ids = [session.id for session in sessions]
    recorded_session_ids = set(db.scalars(select(WorkSegment.session_id).where(
        WorkSegment.session_id.in_(session_ids),
    )).all()) if session_ids else set()
    for session in sessions:
        if session.id in recorded_session_ids or not session.ended_at or session.worked_seconds <= 0:
            continue
        legacy_end = session.ended_at
        if legacy_end.tzinfo is None:
            legacy_end = legacy_end.replace(tzinfo=timezone.utc)
        legacy_start = legacy_end - timedelta(seconds=session.worked_seconds)
        for range_start, range_end in merged_ranges:
            clipped_start = max(legacy_start, range_start)
            clipped_end = min(legacy_end, range_end)
            if clipped_start < clipped_end:
                add_interval(clipped_start, clipped_end, session.status, session.id)

    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    week = []
    week_max_minutes = max((daily_seconds.get(week_start + timedelta(days=i), 0) // 60 for i in range(7)), default=0)
    for index, label in enumerate(weekday_labels):
        day = week_start + timedelta(days=index)
        seconds = daily_seconds.get(day, 0)
        minutes = seconds // 60
        week.append({
            "label": label,
            "date": day.day,
            "minutes": minutes,
            "percent": round(minutes / week_max_minutes * 100) if week_max_minutes else 0,
            "is_today": day == today,
        })

    month_max = max((seconds for day, seconds in daily_seconds.items() if day.month == month_start.month and day.year == month_start.year), default=0)
    month_weeks = []
    for dates in calendar.Calendar(firstweekday=0).monthdatescalendar(month_start.year, month_start.month):
        month_weeks.append([{
            "day": day.day,
            "date": day.isoformat(),
            "minutes": daily_seconds.get(day, 0) // 60,
            "has_activity": daily_seconds.get(day, 0) > 0,
            "bubble_size": round(8 + (daily_seconds.get(day, 0) / month_max * 28)) if daily_seconds.get(day, 0) and month_max else 7,
            "in_month": day.month == month_start.month,
            "is_today": day == today,
            "is_viewed_week": week_start <= day <= week_end,
        } for day in dates])

    month_seconds = sum(seconds for day, seconds in daily_seconds.items() if day.month == month_start.month and day.year == month_start.year)
    month_prefix = f"{month_start.year:04d}-{month_start.month:02d}-"
    month_completed = sum(detail["completed"] for date, detail in daily_details.items() if date.startswith(month_prefix))
    month_stopped = sum(detail["stopped"] for date, detail in daily_details.items() if date.startswith(month_prefix))
    for detail in daily_details.values():
        hourly_items = list(detail["hourly"].values())
        maximum = max((bucket["seconds"] for bucket in hourly_items), default=0)
        first_hour = min((bucket["hour"] for bucket in hourly_items), default=0)
        last_hour = max((bucket["hour"] for bucket in hourly_items), default=0)
        axis_start = max(0, first_hour - 1)
        axis_end = min(24, last_hour + 2)
        missing_hours = max(0, 6 - (axis_end - axis_start))
        left_extension = min(axis_start, (missing_hours + 1) // 2)
        axis_start -= left_extension
        missing_hours -= left_extension
        right_extension = min(24 - axis_end, missing_hours)
        axis_end += right_extension
        missing_hours -= right_extension
        axis_start = max(0, axis_start - missing_hours)
        axis_span = axis_end - axis_start
        detail["ticks"] = [{
            "label": f"{hour + 1:02d}",
            "left": round((hour - axis_start + 0.5) / axis_span * 100, 2),
        } for hour in range(axis_start, axis_end)]
        for bucket in hourly_items:
            bucket["label"] = f"{bucket['hour']:02d}:00–{bucket['hour']:02d}:59"
            bucket["left"] = round((bucket["hour"] - axis_start + 0.5) / axis_span * 100, 2)
            bucket["height"] = round(bucket["seconds"] / maximum * 100) if maximum else 0
            bucket["completed"] = len(bucket.pop("completed_sessions"))
            bucket["stopped"] = len(bucket.pop("stopped_sessions"))
            bucket["only_stopped"] = bucket["completed"] == 0
        hourly_items.sort(key=lambda bucket: bucket["hour"])
        detail["hourly"] = hourly_items
        detail["sessions"] = sorted(detail["sessions"])
    return {
        "today_seconds": daily_seconds.get(today, 0),
        "today_minutes": daily_seconds.get(today, 0) // 60,
        "week": week,
        "week_label": "今週" if week_start == current_week_start else f"{week_start.month}月{week_start.day}日〜{week_end.month}月{week_end.day}日",
        "week_start": week_start.isoformat(),
        "previous_week": previous_week.isoformat(),
        "previous_week_month": previous_week.strftime("%Y-%m"),
        "next_week": next_week.isoformat(),
        "next_week_month": next_week.strftime("%Y-%m"),
        "week_seconds": sum(daily_seconds.get(week_start + timedelta(days=i), 0) for i in range(7)),
        "week_minutes": sum(item["minutes"] for item in week),
        "month_label": f"{month_start.year}年{month_start.month}月",
        "month_value": month_start.strftime("%Y-%m"),
        "previous_month": previous_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "month_seconds": month_seconds,
        "month_minutes": month_seconds // 60,
        "month_completed": month_completed,
        "month_stopped": month_stopped,
        "month_weeks": month_weeks,
        "details": daily_details,
        "selected_date": (today if today.year == month_start.year and today.month == month_start.month else month_start).isoformat(),
    }


@app.exception_handler(401)
async def unauthorized(_: Request, __: HTTPException):
    return redirect("/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def login(request: Request, username: str = Form(), password: str = Form(), db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.username == username.strip())).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html", {"error": "ユーザー名またはパスワードが違います"}, status_code=400)
    request.session.clear()
    request.session["user_id"] = user.id
    return redirect("/admin" if user.role == "admin" else "/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/", response_class=HTMLResponse)
def timer_page(
    request: Request,
    month: str | None = None,
    week: str | None = None,
    reminder: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    session = active_session(db, user.id)
    state = None
    if session:
        state = {"id": session.id, "status": session.status, "planned": session.planned_seconds, "remaining": remaining_seconds(session)}
    target_month = None
    if month:
        try:
            target_month = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            target_month = None
    target_week = None
    if week:
        try:
            target_week = datetime.strptime(week, "%Y-%m-%d").date()
        except ValueError:
            target_week = None
    default_seconds = last_planned_seconds(db, user.id, ALLOW_SHORT_TIMERS)
    reminder_minutes = None
    if reminder is not None and session is None:
        selected_reminder = db.get(Reminder, reminder)
        if selected_reminder and selected_reminder.user_id == user.id:
            default_seconds = selected_reminder.planned_seconds
            reminder_minutes = selected_reminder.planned_seconds // 60
    return templates.TemplateResponse(
        request,
        "timer.html",
        {
            "user": user,
            "state": state,
            "default_seconds": default_seconds,
            "reminder_minutes": reminder_minutes,
            "activity": activity_summary(db, user.id, target_month=target_month, target_week=target_week),
            "allow_short_timers": ALLOW_SHORT_TIMERS,
        },
    )


WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]
REMINDER_DURATIONS = [5, 10, 15, 30, 45, 60, 90, 120]


def owned_reminder(reminder_id: int, user: User, db: Session) -> Reminder:
    reminder = db.get(Reminder, reminder_id)
    if not reminder or reminder.user_id != user.id:
        raise HTTPException(404, "リマインダーが見つかりません")
    return reminder


@app.get("/reminders", response_class=HTMLResponse)
def reminder_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    reminders = db.scalars(select(Reminder).where(Reminder.user_id == user.id).order_by(
        Reminder.weekday, Reminder.minute_of_day,
    )).all()
    rows = [{
        "reminder": reminder,
        "weekday": "毎日" if reminder.weekday == -1 else f"{WEEKDAY_LABELS[reminder.weekday]}曜日",
        "time": f"{reminder.minute_of_day // 60:02d}:{reminder.minute_of_day % 60:02d}",
        "minutes": reminder.planned_seconds // 60,
    } for reminder in reminders]
    return templates.TemplateResponse(request, "reminders.html", {
        "user": user,
        "rows": rows,
        "weekday_labels": WEEKDAY_LABELS,
        "reminder_durations": REMINDER_DURATIONS,
    })


def reminder_values(
    weekday: int,
    reminder_hour: int,
    reminder_minute: int,
    planned_minutes: int,
    notification_message: str,
) -> dict:
    message = notification_message.strip()
    if (
        weekday not in range(-1, 7)
        or reminder_hour not in range(24)
        or reminder_minute not in range(60)
        or planned_minutes < 5
        or planned_minutes > 240
        or len(message) > 120
    ):
        raise HTTPException(422, "設定できる範囲外です")
    return {
        "weekday": weekday,
        "minute_of_day": reminder_hour * 60 + reminder_minute,
        "planned_seconds": planned_minutes * 60,
        "message": message or None,
    }


@app.post("/reminders")
def create_reminder(
    weekday: int = Form(),
    reminder_hour: int = Form(),
    reminder_minute: int = Form(),
    planned_minutes: int = Form(),
    notification_message: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    values = reminder_values(weekday, reminder_hour, reminder_minute, planned_minutes, notification_message)
    db.add(Reminder(user_id=user.id, **values))
    db.commit()
    return redirect("/reminders")


@app.post("/reminders/{reminder_id}")
def update_reminder(
    reminder_id: int,
    weekday: int = Form(),
    reminder_hour: int = Form(),
    reminder_minute: int = Form(),
    planned_minutes: int = Form(),
    notification_message: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    reminder = owned_reminder(reminder_id, user, db)
    values = reminder_values(weekday, reminder_hour, reminder_minute, planned_minutes, notification_message)
    schedule_changed = (reminder.weekday, reminder.minute_of_day) != (values["weekday"], values["minute_of_day"])
    for key, value in values.items():
        setattr(reminder, key, value)
    if schedule_changed:
        reminder.last_notified_on = None
    db.commit()
    return redirect("/reminders")


@app.post("/reminders/{reminder_id}/toggle")
def toggle_reminder(reminder_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    reminder = owned_reminder(reminder_id, user, db)
    reminder.enabled = not reminder.enabled
    db.commit()
    return redirect("/reminders")


@app.post("/reminders/{reminder_id}/delete")
def delete_reminder(reminder_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    reminder = owned_reminder(reminder_id, user, db)
    db.delete(reminder)
    db.commit()
    return redirect("/reminders")


@app.post("/api/sessions")
def set_timer(planned_seconds: int = Form(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    if active_session(db, user.id):
        raise HTTPException(409, "進行中のタイマーがあります")
    minimum_seconds = 1 if ALLOW_SHORT_TIMERS else 300
    if planned_seconds < minimum_seconds or planned_seconds > 4 * 3600:
        raise HTTPException(422, "設定できる時間の範囲外です")
    session = TimerSession(
        user_id=user.id,
        planned_seconds=planned_seconds,
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(session)
    db.flush()
    open_work_segment(db, session, session.started_at)
    db.commit()
    return {"id": session.id, "status": session.status, "remaining": planned_seconds}


def owned_session(session_id: int, user: User, db: Session) -> TimerSession:
    session = db.get(TimerSession, session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404)
    return session


@app.post("/api/sessions/{session_id}/start")
def start(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = owned_session(session_id, user, db)
    if session.status != "ready":
        raise HTTPException(409)
    session.started_at = datetime.now(timezone.utc)
    session.status = "running"
    open_work_segment(db, session, session.started_at)
    db.commit()
    return {"status": session.status, "remaining": session.planned_seconds}


@app.post("/api/sessions/{session_id}/pause")
def pause(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = owned_session(session_id, user, db)
    if session.status != "running":
        raise HTTPException(409)
    session.pause_started_at = datetime.now(timezone.utc)
    session.status = "paused"
    close_work_segment(db, session, session.pause_started_at)
    db.commit()
    return {"status": session.status, "remaining": remaining_seconds(session)}


@app.post("/api/sessions/{session_id}/resume")
def resume(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = owned_session(session_id, user, db)
    if session.status != "paused" or not session.pause_started_at:
        raise HTTPException(409)
    now = datetime.now(timezone.utc)
    paused_at = session.pause_started_at
    if paused_at.tzinfo is None:
        paused_at = paused_at.replace(tzinfo=timezone.utc)
    session.paused_seconds += max(0, int((now - paused_at).total_seconds()))
    session.pause_started_at = None
    session.status = "running"
    open_work_segment(db, session, now)
    db.commit()
    return {"status": session.status, "remaining": remaining_seconds(session, now)}


@app.post("/api/sessions/{session_id}/finish")
def finish(session_id: int, background_tasks: BackgroundTasks, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = owned_session(session_id, user, db)
    if session.status in ("completed", "stopped"):
        return {"status": session.status, "worked_seconds": session.worked_seconds}
    if session.status not in ("running", "paused"):
        raise HTTPException(409)
    now = datetime.now(timezone.utc)
    session.worked_seconds = worked_so_far(session, now)
    session.ended_at = now
    session.pause_started_at = None
    session.status = "completed" if session.worked_seconds >= session.planned_seconds else "stopped"
    close_work_segment(db, session, now, session.worked_seconds)
    db.commit()
    if session.status == "completed":
        background_tasks.add_task(send_timer_notification_async, SessionLocal, user.id)
    return {"status": session.status, "worked_seconds": session.worked_seconds}


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_app_manifest():
    return FileResponse(
        BASE_DIR / "static" / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/push/config")
def push_config(_: User = Depends(current_user)):
    return {"enabled": True, "application_server_key": application_server_key()}


@app.post("/api/push/subscriptions")
def save_push_subscription(subscription: dict, user: User = Depends(current_user), db: Session = Depends(get_db)):
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys", {})
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(422, "Push購読情報が不正です")
    saved = db.scalars(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
    if saved:
        saved.user_id = user.id
        saved.p256dh = keys["p256dh"]
        saved.auth = keys["auth"]
    else:
        db.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh=keys["p256dh"], auth=keys["auth"]))
    db.commit()
    return {"status": "subscribed"}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    users = db.scalars(select(User).where(User.role != "admin").order_by(User.username)).all()
    rows = [{"user": account, **totals(db, account.id)} for account in users]
    return templates.TemplateResponse(request, "admin.html", {"rows": rows})


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_activity(
    request: Request,
    user_id: int,
    month: str | None = None,
    week: str | None = None,
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    if not account or account.role == "admin":
        raise HTTPException(404, "利用者が見つかりません")
    target_month = None
    if month:
        try:
            target_month = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            pass
    target_week = None
    if week:
        try:
            target_week = datetime.strptime(week, "%Y-%m-%d").date()
        except ValueError:
            pass
    activity = activity_summary(db, account.id, target_month=target_month, target_week=target_week)
    report_text = "\n".join([
        f"{account.username} 集中時間",
        f"今日: {format_duration_ja(activity['today_seconds'])}",
        f"{activity['week_label']}: {format_duration_ja(activity['week_seconds'])}",
        f"{activity['month_label']}: {format_duration_ja(activity['month_seconds'])}",
        f"完了: {activity['month_completed']}回",
        f"途中終了: {activity['month_stopped']}回",
    ])
    return templates.TemplateResponse(request, "admin_user.html", {
        "account": account,
        "activity": activity,
        "report_text": report_text,
    })


@app.post("/admin/users")
def create_account(username: str = Form(), password: str = Form(), _: User = Depends(admin_user), db: Session = Depends(get_db)):
    username = username.strip()
    if len(username) < 2 or len(password) < 8:
        raise HTTPException(422, "ユーザー名2文字以上、パスワード8文字以上が必要です")
    if db.scalars(select(User).where(User.username == username)).first():
        raise HTTPException(409, "同じユーザー名が存在します")
    db.add(User(username=username, password_hash=hash_password(password), role="user"))
    db.commit()
    return redirect("/admin")


@app.post("/admin/users/{user_id}/password")
def reset_account_password(
    user_id: int,
    password: str = Form(),
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    if not account or account.role == "admin":
        raise HTTPException(404, "利用者が見つかりません")
    if len(password) < 8:
        raise HTTPException(422, "パスワードは8文字以上が必要です")
    account.password_hash = hash_password(password)
    db.commit()
    return redirect("/admin?password_reset=1")


@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})
