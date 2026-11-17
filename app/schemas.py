from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ProfileInput(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    age: int = Field(ge=18, le=99)
    purpose: Literal["friends", "romance", "both"] = "both"
    gender: Literal["male", "female", "other"] = "other"
    looking_for: Literal["any", "male", "female", "other"] = "any"
    age_min: int = Field(default=18, ge=18, le=99)
    age_max: int = Field(default=99, ge=18, le=99)
    timezone: str = "Europe/Moscow"
    start_hour: int = Field(default=19, ge=0, le=23)
    end_hour: int = Field(default=23, ge=0, le=24)
    bio: str = Field(default="", max_length=500)
    language: Literal["ru", "en"] = "ru"
    game_ids: list[int] = Field(min_length=1, max_length=10)

    @field_validator("name", "bio")
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("Недопустимые управляющие символы")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Укажите часовой пояс IANA, например Asia/Novosibirsk")
        return value

    @model_validator(mode="after")
    def valid_ranges(self):
        if len(self.name) < 2:
            raise ValueError("Имя должно содержать хотя бы 2 символа")
        if self.age_min > self.age_max:
            raise ValueError("Неверный возрастной диапазон")
        if self.start_hour == self.end_hour or (self.start_hour == 0 and self.end_hour == 24):
            raise ValueError("Выберите интервал меньше суток")
        self.game_ids = list(dict.fromkeys(self.game_ids))
        return self


class ReactionInput(BaseModel):
    target_id: int = Field(gt=0)
    kind: Literal["like", "skip", "block", "report"]
    reason: str = Field(default="Другое", min_length=1, max_length=500)
