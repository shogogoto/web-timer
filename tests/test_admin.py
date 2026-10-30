import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import admin_user, hash_password
from app.db import Base
from app.main import admin_login, create_account, login, pause_policy, update_pause_policy
from app.models import TimerPolicy, User


def request(path: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": [], "session": {}})


def test_regular_user_logs_in_without_password_and_admin_cannot_use_user_login():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="user", password_hash=hash_password("obsolete-password"), role="user")
        admin = User(username="admin", password_hash=hash_password("admin-password"), role="admin")
        db.add_all([user, admin])
        db.commit()

        user_request = request("/login")
        response = login(user_request, "user", db)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert user_request.session["user_id"] == user.id

        admin_request = request("/login")
        response = login(admin_request, "admin", db)
        assert response.status_code == 400
        assert "user_id" not in admin_request.session


def test_admin_uses_separate_password_login():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        admin = User(username="admin", password_hash=hash_password("admin-password"), role="admin")
        user = User(username="user", password_hash=hash_password("user-password"), role="user")
        db.add_all([admin, user])
        db.commit()

        admin_request = request("/admin/login")
        response = admin_login(admin_request, "admin", "admin-password", db)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"
        assert admin_request.session["user_id"] == admin.id

        wrong_role_request = request("/admin/login")
        response = admin_login(wrong_role_request, "user", "user-password", db)
        assert response.status_code == 400
        assert "user_id" not in wrong_role_request.session


def test_admin_creates_passwordless_user_and_updates_pause_policy():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        admin = User(username="admin", password_hash=hash_password("admin-password"), role="admin")
        db.add(admin)
        db.commit()

        response = create_account("new-user", admin, db)
        account = db.query(User).filter_by(username="new-user").one()
        assert response.status_code == 303
        assert account.password_hash == "!"

        response = update_pause_policy(8, 4, admin, db)
        assert response.headers["location"] == "/admin?settings_saved=1"
        assert pause_policy(db)["warning_minutes"] == 8
        assert pause_policy(db)["max_pause_count"] == 4
        assert db.get(TimerPolicy, 1) is not None

        with pytest.raises(HTTPException) as invalid:
            update_pause_policy(0, 11, admin, db)
        assert invalid.value.status_code == 422

        with pytest.raises(HTTPException) as denied:
            admin_user(account)
        assert denied.value.status_code == 403
