import os

from fastapi import Depends, HTTPException, Request, status
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from .db import get_db
from .models import User


password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


def bootstrap_admin(db: Session) -> None:
    username = os.getenv("ADMIN_USERNAME")
    password = os.getenv("ADMIN_PASSWORD")
    if not username or not password:
        return
    if not db.query(User).filter_by(username=username).first():
        db.add(User(username=username, password_hash=hash_password(password), role="admin"))
        db.commit()

