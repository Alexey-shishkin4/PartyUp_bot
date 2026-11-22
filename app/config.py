from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    bot_token: str = ""
    force_demo: bool = False
    steam_api_key: str = ""
    steam_mode: Literal["link", "openid"] = "link"
    public_url: str = "http://127.0.0.1:8765"
    admin_ids: str = ""
    developer_ids: str = ""
    data_location: str = "локальный компьютер владельца"
    operator_contact: str = "администратор бота"
    database_url: str = ""
    data_dir: Path = ROOT / "data"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    @field_validator("admin_ids","developer_ids")
    @classmethod
    def valid_operator_ids(cls,value):
        try:
            identifiers={int(part.strip()) for part in value.split(",") if part.strip()}
        except ValueError:
            raise ValueError("Права задаются числовыми Telegram ID через запятую") from None
        if any(identifier<=0 or identifier>=2**63 for identifier in identifiers):
            raise ValueError("Telegram ID оператора должен быть положительным числом")
        return ",".join(str(identifier) for identifier in sorted(identifiers))

    @model_validator(mode="after")
    def validate_url(self):
        self.public_url = self.public_url.rstrip("/")
        try:
            parsed = urlsplit(self.public_url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
            if parsed.port is not None and not 0 < parsed.port <= 65535:
                valid = False
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("PUBLIC_URL должен быть HTTP(S)-адресом без пароля, query-параметров и фрагмента")
        if self.steam_mode == "openid" and parsed.scheme != "https":
            raise ValueError("Для Steam OpenID укажите PUBLIC_URL с HTTPS в .env")
        return self

    @property
    def demo(self) -> bool:
        return self.force_demo or not bool(self.bot_token)

    @property
    def admins(self) -> set[int]:
        return {int(value.strip()) for value in self.admin_ids.split(",") if value.strip()}

    @property
    def developers(self) -> set[int]:
        return {int(value.strip()) for value in self.developer_ids.split(",") if value.strip()} | self.admins

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        directory = self.data_dir.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return "sqlite:///" + (directory / ("demo.sqlite3" if self.demo else "bot.sqlite3")).as_posix()
