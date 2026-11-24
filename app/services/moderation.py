from sqlalchemy import select, update

from app.persistence.models import Report, User


class ModerationService:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def pending_reports(self, limit=20):
        with self.db.session() as session:
            return list(session.scalars(select(Report).where(Report.resolved.is_(False)).limit(limit)))

    def set_banned(self, user_id, *, banned: bool):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                return False
            user.banned = banned
            if banned:
                session.execute(update(Report).where(Report.target_id == user.id).values(resolved=True))
            return True
