from collections import defaultdict

from sqlalchemy import select

from app.domain.deadlock import rank_name
from app.domain.pubg import MODES
from app.domain.riot import RiotGame
from app.domain.steam import CS2_APP_ID, DEADLOCK_APP_ID, PUBG_APP_ID
from app.persistence.models import DeadlockProfile, FaceitProfile, FortniteAccount, PubgProfile, RiotProfile, User

RIOT_GAME_NAMES = {RiotGame.VALORANT: "Valorant", RiotGame.LEAGUE: "League of Legends"}


def read_game_labels(session, user_ids):
    labels = defaultdict(dict)
    if not user_ids:
        return labels
    for row in session.scalars(select(FaceitProfile).where(FaceitProfile.user_id.in_(user_ids))):
        rank = f"FACEIT {row.skill_level}" if row.skill_level is not None else None
        labels[row.user_id][CS2_APP_ID] = ("Counter-Strike 2", rank)
    deadlock = select(DeadlockProfile).join(User, User.id == DeadlockProfile.user_id).where(
        DeadlockProfile.user_id.in_(user_ids), DeadlockProfile.steam_id == User.steam_id,
    )
    for row in session.scalars(deadlock):
        labels[row.user_id][DEADLOCK_APP_ID] = ("Deadlock", rank_name(row.rank_badge))
    for row in session.scalars(select(PubgProfile).where(PubgProfile.user_id.in_(user_ids))):
        ranks = row.ranks or {}
        seconds = row.mode_seconds or {}
        mode = max((mode for mode in MODES if mode in ranks), key=lambda mode: seconds.get(mode, 0), default=None)
        rank = ranks[mode].split(" · ", 1)[0] if mode else None
        labels[row.user_id][PUBG_APP_ID] = ("PUBG", rank)
    for row in session.scalars(select(FortniteAccount).where(FortniteAccount.user_id.in_(user_ids))):
        ranks = {mode.mode: mode.rank_name for mode in row.modes if mode.rank_name}
        rank = ranks.get(row.favorite_mode)
        if not rank and row.favorite_mode in {"reload", "reload_zero_build"}:
            rank = ranks.get("reload_combined")
        labels[row.user_id]["fortnite"] = ("Fortnite", rank)
    riot = select(RiotProfile).where(RiotProfile.user_id.in_(user_ids)).order_by(RiotProfile.game)
    for row in session.scalars(riot):
        if row.game in RIOT_GAME_NAMES:
            labels[row.user_id][row.game] = (RIOT_GAME_NAMES[row.game], row.rank)
    return labels
