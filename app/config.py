from pathlib import Path
from typing import Literal

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
        if self.steam_mode == "openid" and not self.public_url.startswith("https://"):
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
        directory = ROOT / "data"
        directory.mkdir(exist_ok=True)
        return "sqlite:///" + (directory / ("demo.sqlite3" if self.demo else "bot.sqlite3")).as_posix()
