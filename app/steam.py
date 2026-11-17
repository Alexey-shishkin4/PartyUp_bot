import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from .service import DomainError

OPENID = "https://steamcommunity.com/openid/login"
NAMESPACE = "http://specs.openid.net/auth/2.0"
STEAM_ID = re.compile(r"https://steamcommunity\.com/openid/id/(7656119\d{10})$")


class SteamClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient):
        self.api_key, self.client = api_key, client

    @staticmethod
    def login_url(return_to: str, realm: str):
        return OPENID + "?" + urlencode({
            "openid.ns": NAMESPACE, "openid.mode": "checkid_setup",
            "openid.return_to": return_to, "openid.realm": realm,
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        })

    async def verify(self, params: dict, expected_return_to: str) -> str:
        if params.get("openid.mode") != "id_res" or params.get("openid.ns") != NAMESPACE:
            raise DomainError("Steam не подтвердил вход")
        if params.get("openid.op_endpoint") != OPENID or params.get("openid.return_to") != expected_return_to:
            raise DomainError("Неверный адрес подтверждения Steam")
        claimed = params.get("openid.claimed_id", "")
        match = STEAM_ID.fullmatch(claimed)
        if not match or params.get("openid.identity") != claimed:
            raise DomainError("Неверный идентификатор Steam")
        signed = set(params.get("openid.signed", "").split(","))
        if not {"op_endpoint", "claimed_id", "identity", "return_to", "response_nonce"} <= signed:
            raise DomainError("Steam не подписал обязательные поля")
        try:
            nonce_time = datetime.strptime(params["openid.response_nonce"][:20], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if abs(datetime.now(timezone.utc) - nonce_time) > timedelta(minutes=10):
                raise ValueError
        except (KeyError, ValueError):
            raise DomainError("Подтверждение Steam истекло")
        form = {key: value for key, value in params.items() if key.startswith("openid.")}
        form["openid.mode"] = "check_authentication"
        try:
            response = await self.client.post(OPENID, data=form)
            response.raise_for_status()
        except httpx.HTTPError:
            raise DomainError("Steam временно недоступен. Попробуйте снова") from None
        verified = dict(line.split(":", 1) for line in response.text.splitlines() if ":" in line)
        if verified.get("is_valid") != "true":
            raise DomainError("Steam отклонил подтверждение входа")
        return match.group(1)

    async def owned_games(self, steam_id: str):
        if not self.api_key:
            raise DomainError("Вход Steam подтверждён; импорт включится после настройки STEAM_API_KEY")
        try:
            response = await self.client.get(
                "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
                params={"key": self.api_key, "steamid": steam_id, "include_appinfo": 1,
                        "include_played_free_games": 1},
            )
            response.raise_for_status()
            payload = response.json().get("response", {})
            if "game_count" not in payload:
                raise DomainError("Библиотека скрыта или недоступна. Откройте «Доступ к игровой информации» в Steam или выберите игры вручную")
            return payload.get("games", [])
        except (httpx.HTTPError, ValueError, AttributeError):
            raise DomainError("Не удалось получить библиотеку Steam. Сохранённые игры остаются доступны") from None
