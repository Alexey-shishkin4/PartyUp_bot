from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StorageKey

from .models import FSMRecord


class DatabaseStorage(BaseStorage):
    """Interaction state survives restarts and is deleted with the profile."""
    def __init__(self, db):
        self.db = db
        self.builder = DefaultKeyBuilder(with_bot_id=True, with_destiny=True)

    async def set_state(self, key: StorageKey, state=None):
        identity = self.builder.build(key)
        with self.db.session.begin() as session:
            row = session.get(FSMRecord, identity)
            if row is None:
                row = FSMRecord(key=identity, telegram_id=key.user_id, data={})
                session.add(row)
            row.state = state.state if hasattr(state, "state") else state

    async def get_state(self, key: StorageKey):
        with self.db.session() as session:
            row = session.get(FSMRecord, self.builder.build(key))
            return row.state if row else None

    async def set_data(self, key: StorageKey, data):
        with self.db.session.begin() as session:
            identity = self.builder.build(key)
            row = session.get(FSMRecord, identity)
            if row is None:
                row = FSMRecord(key=identity, telegram_id=key.user_id)
                session.add(row)
            row.data = dict(data)

    async def get_data(self, key: StorageKey):
        with self.db.session() as session:
            row = session.get(FSMRecord, self.builder.build(key))
            return dict(row.data) if row else {}

    async def close(self):
        pass
