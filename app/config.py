from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "app" / "resources"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    miniapp_url: str = ""
    miniapp_port: int = Field(default=8766, ge=1, le=65535)
    miniapp_debug: bool = False
    miniapp_debug_telegram_id: int | None = Field(default=None, gt=0, lt=2**52)
    miniapp_test_feed_telegram_id: int | None = Field(default=None, gt=0, lt=2**52)

    @field_validator("miniapp_url")
    @classmethod
    def valid_miniapp_url(cls, value):
        value = value.strip().rstrip("/")
        if value:
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("MINIAPP_URL должен быть публичным HTTPS-адресом Mini App")
        return value
    steam_api_key: str = ""
    api_fortnite_key: SecretStr = SecretStr("")
    api_fortnite_daily_limit: int = Field(default=10_000, ge=1)
    fortnite_api_key: SecretStr = SecretStr("")
    admin_ids: str = ""
    developer_ids: str = ""
    enable_test_profiles: bool = False
    data_location: str = "локальный компьютер владельца"
    operator_contact: str = "администратор бота"
    postgres_host: str = "db"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "partyup"
    postgres_user: str = "partyup"
    postgres_password: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_miniapp_debug(self):
        if self.miniapp_debug and self.miniapp_debug_telegram_id is None:
            raise ValueError("Для MINIAPP_DEBUG укажите MINIAPP_DEBUG_TELEGRAM_ID")
        return self

    @property
    def miniapp_local_url(self) -> str:
        return f"http://127.0.0.1:{self.miniapp_port}/miniapp"

    @field_validator("admin_ids", "developer_ids")
    @classmethod
    def valid_operator_ids(cls, value):
        try:
            identifiers = {int(part.strip()) for part in value.split(",") if part.strip()}
        except ValueError:
            raise ValueError("Права задаются числовыми Telegram ID через запятую") from None
        if any(identifier <= 0 or identifier >= 2**63 for identifier in identifiers):
            raise ValueError("Telegram ID оператора должен быть положительным числом")
        return ",".join(str(identifier) for identifier in sorted(identifiers))

    @property
    def admins(self) -> set[int]:
        return {int(value) for value in self.admin_ids.split(",") if value}

    @property
    def developers(self) -> set[int]:
        return {int(value) for value in self.developer_ids.split(",") if value} | self.admins

    @property
    def db_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )
