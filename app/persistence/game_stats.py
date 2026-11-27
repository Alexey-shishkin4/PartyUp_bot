from sqlalchemy import select

from app.domain.errors import DomainError

from .models import GameAccount, GameStats, User


def writable_user(session, user_id):
    user = session.get(User, user_id, with_for_update=True)
    if not user or user.banned or user.is_test:
        raise DomainError("Анкета недоступна.")
    return user


def find_stats(session, user_id, platform, game_id):
    return session.scalar(
        select(GameStats).join(GameStats.account).where(
            GameAccount.user_id == user_id, GameAccount.platform == platform, GameStats.game_id == game_id,
        )
    )


def replace_account(session, user, platform, external_id, *, replace=False):
    account = user.game_account(platform)
    if account and (account.external_account_id != external_id or replace):
        user.accounts.remove(account)
        session.flush()
        account = None
    if account is None:
        account = GameAccount(platform=platform, external_account_id=external_id, details={})
        user.accounts.append(account)
    return account


def save_stats(session, account, game_id, current=None):
    if current is not None and current.account is account:
        return current
    row = GameStats(account=account, game_id=game_id, details={})
    session.add(row)
    return row
