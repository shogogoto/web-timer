import asyncio
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
def timer_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = active_session(db, user.id)
    state = None
    if session:
        state = {"id": session.id, "status": session.status, "planned": session.planned_seconds, "remaining": remaining_seconds(session)}
    return templates.TemplateResponse(
        request,
        "timer.html",
        {
            "user": user,
            "state": state,
            "default_seconds": last_planned_seconds(db, user.id, ALLOW_SHORT_TIMERS),
            "totals": totals(db, user.id),
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


@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})
