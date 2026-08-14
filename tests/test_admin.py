import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import admin_user, hash_password, verify_password
from app.db import Base
from app.main import reset_account_password
from app.models import User


def test_admin_can_reset_user_password_but_user_cannot():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        admin = User(username="admin", password_hash=hash_password("admin-password"), role="admin")
        user = User(username="user", password_hash=hash_password("old-password"), role="user")
        db.add_all([admin, user])
        db.commit()

        with pytest.raises(HTTPException) as denied:
            admin_user(user)
        assert denied.value.status_code == 403

        changed = reset_account_password(user.id, "new-password", admin, db)
        db.refresh(user)
        assert changed.status_code == 303
        assert changed.headers["location"] == "/admin?password_reset=1"
        assert verify_password("new-password", user.password_hash)
        assert not verify_password("old-password", user.password_hash)
