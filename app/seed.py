from sqlalchemy import select

from .models import Reaction, User
from .schemas import ProfileInput


def seed_demo(service):
    if service.user_by_telegram(-1):
        return
    examples = [
        (-1, "Ты", 25, "Asia/Novosibirsk", 20, 24, "both", [105600, 548430, 892970], "Ищу своих людей для вечернего коопа. Без спешки и с хорошими разговорами."),
        (-2, "Мира", 24, "Asia/Novosibirsk", 20, 24, "both", [105600, 548430, 413150], "Строю красивые базы, иногда случайно взрываю. За кооп, мемы и разговоры после полуночи."),
        (-3, "Никита", 27, "Asia/Krasnoyarsk", 19, 24, "friends", [548430, 892970, 550], "Rock and stone! Ищу постоянную пати. Спокойно объясню новичкам."),
        (-4, "Ася", 23, "Europe/Moscow", 18, 23, "both", [105600, 413150, 1086940], "Люблю истории, пошаговые бои и уютные фермы. Хочу пройти BG3 с кем-то на одной волне."),
        (-5, "Даня", 26, "Asia/Novosibirsk", 21, 2, "both", [892970, 105600, 322330], "Викинг на полставки. Могу весь вечер искать место для дома и быть совершенно счастлив."),
        (-6, "Лера", 25, "Asia/Yekaterinburg", 20, 24, "romance", [1245620, 1091500, 292030], "Обсудим любимых персонажей и музыку из игр. С Elden Ring всё сложно, но это любовь."),
        (-7, "Илья", 28, "Europe/Moscow", 20, 24, "friends", [730, 570, 1172470], "Люблю командные игры и понятные коллы. Ищу сыгранность и спокойную атмосферу."),
        (-8, "Соня", 22, "Asia/Novosibirsk", 19, 23, "both", [739630, 1966720, 3241660], "Кричу в хоррорах, смеюсь над собой. Если тоже боишься — пойдём вместе."),
    ]
    for telegram, name, age, zone, start, end, purpose, games, bio in examples:
        gender = "female" if telegram in {-2, -4, -6, -8} else "male" if telegram != -1 else "other"
        service.save_profile(telegram, ProfileInput(name=name, age=age, gender=gender, timezone=zone, start_hour=start,
                                                   end_hour=end, purpose=purpose, game_ids=games, bio=bio), demo=True)
    viewer = service.user_by_telegram(-1)
    # Synthetic incoming likes exist only in the separate demo database.
    with service.db.session.begin() as session:
        for peer in session.scalars(select(User).where(User.telegram_id.in_([-2, -5]))):
            session.add(Reaction(actor_id=peer.id, target_id=viewer.id, kind="like"))
