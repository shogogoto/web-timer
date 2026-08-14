import asyncio
import calendar
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import admin_user, bootstrap_admin, current_user, hash_password, verify_password
from .db import Base, SessionLocal, engine, get_db
from .models import PushSubscription, TimerSession, User
from .push import application_server_key, ensure_vapid_key, send_timer_notification_async
from .timer import remaining_seconds, worked_so_far

BASE_DIR = Path(__file__).parent
TZ = ZoneInfo(os.getenv("TZ", "Asia/Tokyo"))
ALLOW_SHORT_TIMERS = os.getenv("ALLOW_SHORT_TIMERS", "false").lower() == "true"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Path("data").mkdir(exist_ok=True)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        bootstrap_admin(db)
    ensure_vapid_key()
    notifier = asyncio.create_task(notification_loop())
    try:
        yield
    finally:
        notifier.cancel()


async def notification_loop() -> None:
    while True:
        await asyncio.sleep(1)
        completed_user_ids: list[int] = []
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            sessions = db.scalars(select(TimerSession).where(TimerSession.status == "running")).all()
            for session in sessions:
                if worked_so_far(session, now) >= session.planned_seconds:
                    session.worked_seconds = session.planned_seconds
                    session.ended_at = now
                    session.status = "completed"
                    completed_user_ids.append(session.user_id)
            db.commit()
        for user_id in completed_user_ids:
            await send_timer_notification_async(SessionLocal, user_id)


app = FastAPI(title="Focus Timer", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "change-me-in-production"),
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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


def last_planned_seconds(db: Session, user_id: int, allow_short: bool = False) -> int:
    statement = select(TimerSession.planned_seconds).where(TimerSession.user_id == user_id)
    if not allow_short:
        statement = statement.where(TimerSession.planned_seconds >= 300)
    planned_seconds = db.scalar(statement.order_by(TimerSession.id.desc()).limit(1))
    return planned_seconds or 40 * 60


def totals(db: Session, user_id: int) -> dict[str, int]:
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def total(since: datetime) -> int:
        return db.scalar(select(func.coalesce(func.sum(TimerSession.worked_seconds), 0)).where(
            TimerSession.user_id == user_id,
            TimerSession.ended_at >= since,
            TimerSession.status.in_(["completed", "stopped"]),
        )) or 0

    today_seconds = total(today)
    week_seconds = total(week)
    return {
        "today": today_seconds // 60,
        "week": week_seconds // 60,
        "today_seconds": today_seconds,
        "week_seconds": week_seconds,
    }


def activity_summary(db: Session, user_id: int, now: datetime | None = None, target_month=None) -> dict:
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = (target_month or today).replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    previous_month = (month_start - timedelta(days=1)).replace(day=1)
    query_start = min(week_start, month_start)
    query_end = max(week_start + timedelta(days=7), next_month)
    query_start_utc = datetime.combine(query_start, datetime.min.time(), TZ).astimezone(timezone.utc)
    query_end_utc = datetime.combine(query_end, datetime.min.time(), TZ).astimezone(timezone.utc)
    sessions = db.scalars(select(TimerSession).where(
        TimerSession.user_id == user_id,
        TimerSession.ended_at >= query_start_utc,
        TimerSession.ended_at < query_end_utc,
        TimerSession.status.in_(["completed", "stopped"]),
    )).all()
    daily_seconds: dict = {}
    daily_details: dict = {}
    for session in sessions:
        ended_at = session.ended_at
        if ended_at is None:
            continue
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)
        day = ended_at.astimezone(TZ).date()
        daily_seconds[day] = daily_seconds.get(day, 0) + session.worked_seconds
        started_at = session.started_at or session.ended_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        local_start = started_at.astimezone(TZ) if started_at is not None else ended_at.astimezone(TZ)
        detail = daily_details.setdefault(day.isoformat(), {"seconds": 0, "completed": 0, "stopped": 0, "sessions": []})
        detail["seconds"] += session.worked_seconds
        detail[session.status] += 1
        detail["sessions"].append({
            "time": local_start.strftime("%H:%M"),
            "start_minute": local_start.hour * 60 + local_start.minute,
            "seconds": session.worked_seconds,
            "status": session.status,
        })

    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    week = []
    week_max = max((daily_seconds.get(week_start + timedelta(days=i), 0) for i in range(7)), default=0)
    for index, label in enumerate(weekday_labels):
        day = week_start + timedelta(days=index)
        seconds = daily_seconds.get(day, 0)
        week.append({
            "label": label,
            "date": day.day,
            "minutes": seconds // 60,
            "percent": round(seconds / week_max * 100) if week_max else 0,
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
        } for day in dates])

    month_seconds = sum(seconds for day, seconds in daily_seconds.items() if day.month == month_start.month and day.year == month_start.year)
    month_prefix = f"{month_start.year:04d}-{month_start.month:02d}-"
    month_completed = sum(detail["completed"] for date, detail in daily_details.items() if date.startswith(month_prefix))
    month_stopped = sum(detail["stopped"] for date, detail in daily_details.items() if date.startswith(month_prefix))
    for detail in daily_details.values():
        hourly: dict = {}
        for session in detail["sessions"]:
            hour = session["start_minute"] // 60
            bucket = hourly.setdefault(hour, {"hour": hour, "seconds": 0, "completed": 0, "stopped": 0})
            bucket["seconds"] += session["seconds"]
            bucket[session["status"]] += 1
        hourly_items = list(hourly.values())
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
            bucket["only_stopped"] = bucket["completed"] == 0
        hourly_items.sort(key=lambda bucket: bucket["hour"])
        detail["hourly"] = hourly_items
        detail["sessions"].sort(key=lambda session: session["time"])
    return {
        "today_seconds": daily_seconds.get(today, 0),
        "today_minutes": daily_seconds.get(today, 0) // 60,
        "week": week,
        "week_seconds": sum(daily_seconds.get(week_start + timedelta(days=i), 0) for i in range(7)),
        "week_minutes": sum(item["minutes"] for item in week),
        "month_label": f"{month_start.year}年{month_start.month}月",
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
def timer_page(request: Request, month: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
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
    return templates.TemplateResponse(
        request,
        "timer.html",
        {
            "user": user,
            "state": state,
            "default_seconds": last_planned_seconds(db, user.id, ALLOW_SHORT_TIMERS),
            "activity": activity_summary(db, user.id, target_month=target_month),
            "allow_short_timers": ALLOW_SHORT_TIMERS,
        },
    )


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
    db.commit()
    return {"status": session.status, "remaining": session.planned_seconds}


@app.post("/api/sessions/{session_id}/pause")
def pause(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = owned_session(session_id, user, db)
    if session.status != "running":
        raise HTTPException(409)
    session.pause_started_at = datetime.now(timezone.utc)
    session.status = "paused"
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
    activity = activity_summary(db, account.id, target_month=target_month)
    report_text = "\n".join([
        f"{account.username} 集中時間",
        f"今日: {format_duration_ja(activity['today_seconds'])}",
        f"今週: {format_duration_ja(activity['week_seconds'])}",
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
