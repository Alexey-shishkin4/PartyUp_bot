DOTA_APP_ID = 570
RANK_NAMES = ("Рекрут", "Страж", "Рыцарь", "Герой", "Легенда", "Властелин", "Божество", "Титан")
STARS = ("I", "II", "III", "IV", "V")


def dota_rank_name(rank_tier):
    if rank_tier is None:
        return None
    medal, stars = divmod(rank_tier, 10)
    if not 1 <= medal <= len(RANK_NAMES) or stars > len(STARS):
        return None
    rank = RANK_NAMES[medal - 1]
    if stars and medal != 8:
        rank += " " + STARS[stars - 1]
    return rank


def dota_game_name(app_id, name, rank_tier):
    rank = dota_rank_name(rank_tier) if app_id == DOTA_APP_ID else None
    return f"{name} ({rank})" if rank else name
