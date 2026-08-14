import asyncio
import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from sqlalchemy import select

from .models import PushSubscription


PRIVATE_KEY_PATH = Path(os.getenv("VAPID_PRIVATE_KEY", "data/vapid_private.pem"))
logger = logging.getLogger(__name__)


def normalize_vapid_subject(value: str | None) -> str:
    subject = (value or "mailto:admin@example.com").strip()
    if not subject:
        return "mailto:admin@example.com"
    if "@" in subject and ":" not in subject:
        subject = f"mailto:{subject}"
    if not subject.startswith(("mailto:", "https://")):
        raise ValueError("VAPID_SUBJECTはmailto:メールアドレス、またはhttps:// URLで設定してください")
    return subject


VAPID_SUBJECT = normalize_vapid_subject(os.getenv("VAPID_SUBJECT"))


def ensure_vapid_key() -> None:
    if PRIVATE_KEY_PATH.exists():
        return
    PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    PRIVATE_KEY_PATH.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    PRIVATE_KEY_PATH.chmod(0o600)


def application_server_key() -> str:
    ensure_vapid_key()
    key = serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def send_timer_notification(db, user_id: int) -> None:
    subscriptions = db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()
    payload = json.dumps({"title": "時間になりました", "body": "タイマーが終了しました", "url": "/"})
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={"endpoint": subscription.endpoint, "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}},
                data=payload,
                vapid_private_key=str(PRIVATE_KEY_PATH),
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=60,
                timeout=10,
            )
        except WebPushException as exc:
            if exc.response is not None and exc.response.status_code in (404, 410):
                db.delete(subscription)
                db.commit()
            else:
                logger.warning("Web Push delivery failed for subscription %s: %s", subscription.id, exc)


async def send_timer_notification_async(db_factory, user_id: int) -> None:
    def send() -> None:
        with db_factory() as db:
            send_timer_notification(db, user_id)

    await asyncio.to_thread(send)
