from threading import RLock

from app.persistence.database import Database

from .deadlock import DeadlockProfiles
from .discovery import DiscoveryService
from .faceit import FaceitProfiles
from .fortnite import FortniteProfiles
from .moderation import ModerationService
from .profiles import ProfileService
from .riot import RiotProfiles
from .steam import SteamLibrary


class Services:
    def __init__(self, db: Database, *, include_test_profiles: bool = False):
        self.db = db
        # Writes that span multiple queries share a lock in the polling process.
        self.lock = RLock()
        self.steam = SteamLibrary(db, self.lock)
        self.deadlock = DeadlockProfiles(db, self.lock)
        self.fortnite = FortniteProfiles(db, self.lock)
        self.riot = RiotProfiles(db, self.lock)
        self.faceit = FaceitProfiles(db, self.lock)
        self.profiles = ProfileService(db, self.lock, self.steam)
        self.discovery = DiscoveryService(
            db, self.lock, self.profiles, include_test_profiles=include_test_profiles
        )
        self.moderation = ModerationService(db, self.lock)
