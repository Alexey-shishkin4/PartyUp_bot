from collections import defaultdict

from sqlalchemy import select

from app.domain.riot import RiotGame
from app.persistence.models import Game, GameAccount, GameStats

RIOT_GAME_NAMES = {RiotGame.VALORANT: "Valorant", RiotGame.LEAGUE: "League of Legends"}


def read_game_labels(session, user_ids):
    labels = defaultdict(dict)
    if not user_ids:
        return labels
    query = (
        select(GameAccount.user_id, GameStats.game_id, Game.name, GameStats.rank)
        .join(GameStats, GameStats.account_id == GameAccount.id)
        .join(Game, Game.app_id == GameStats.game_id)
        .where(GameAccount.user_id.in_(user_ids))
        .order_by(GameAccount.user_id, Game.name)
    )
    for user_id, game_id, name, rank in session.execute(query):
        labels[user_id][game_id] = (name, rank)
    return labels
