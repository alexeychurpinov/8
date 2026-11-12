import re
import unicodedata
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class LeadCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=7, max_length=32)
    email: EmailStr = Field(max_length=254)
    service: str = Field(min_length=1, max_length=160)
    comment: str = Field(default="", max_length=1500)
    source: str = Field(min_length=1, max_length=200)

    @field_validator("*", mode="before")
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str) and any(
            unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\r\t"
            for char in value
        ):
            raise ValueError("Control characters are not allowed")
        return value

    @field_validator("phone")
    @classmethod
    def phone_number(cls, value: str) -> str:
        if not re.fullmatch(r"\+?[0-9 ()-]+", value):
            raise ValueError("Use digits, spaces, parentheses, hyphens and optional leading +")
        digits = re.sub(r"[^0-9]", "", value)
        if not 7 <= len(digits) <= 15:
            raise ValueError("Phone must contain 7–15 digits")
        return ("+" if value.startswith("+") else "") + digits


class LeadReceipt(BaseModel):
    id: int
    status: str
    created_at: datetime
    notification_status: str = "queued"
