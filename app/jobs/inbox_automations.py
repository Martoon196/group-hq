"""Poll only the inbox accounts whose owner has explicitly started keyword replies."""
import logging
from contextlib import contextmanager
from threading import Event, Thread

from sqlalchemy import select

from ..db import SessionLocal
from ..models import AppSetting, User
from ..services import inbox, inbox_automations as automation

logger = logging.getLogger(__name__)


def tick(stopping):
    with SessionLocal() as db:
        rows = db.scalars(select(AppSetting.value).where(AppSetting.key.startswith(automation.PREFIX),
            AppSetting.value["enabled"].as_boolean().is_(True))).all()
        if not rows:
            return
        from ..services.inbox_triage import analyse

        accounts = {a.key: a for a in inbox.accounts(db)}
        for rulebook in rows:
            if stopping():
                return
            user = db.get(User, rulebook["owner_id"], populate_existing=True)
            account = accounts.get(rulebook["key"])
            if (user is None or not user.is_active or user.must_change_password or account is None
                    or rulebook["binding"] != account.binding or not automation.available(account, user)):
                continue
            try:
                automation.poll_account(db, user, account, analyse, stopping)
            except Exception:
                db.rollback()
                logger.exception("Inbox keyword poll failed for account %s", account.key)


@contextmanager
def polling():
    stop = Event()

    def run():
        while not stop.wait(30):
            try:
                tick(stop.is_set)
            except Exception:
                logger.exception("Inbox keyword polling unavailable")

    worker = Thread(name="inbox-keyword-poll", target=run, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=5)
