"""Input validation.

One Pydantic model describes what a valid holding looks like. Both the web
forms and the public API use it, so the rules are written once.

Data types checked (the task asks for at least 4):
  text     -> ticker (must be one of the Magnificent 7), notes
  integer  -> shares
  decimal  -> buy_price
  date     -> buy_date
  boolean  -> is_active
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from market import MAG7


class HoldingIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    ticker: str = Field(description="One of: " + ", ".join(MAG7), examples=["AAPL"])
    shares: int = Field(gt=0, le=10_000_000, description="Whole number of shares", examples=[10])
    buy_price: Decimal = Field(
        gt=0, le=1_000_000, max_digits=12, decimal_places=2,
        description="Price paid per share in USD, max 2 decimal places", examples=["189.50"],
    )
    buy_date: date = Field(description="Date bought, YYYY-MM-DD, not in the future", examples=["2026-03-15"])
    is_active: bool = Field(default=True, description="Still holding this position?")
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("ticker")
    @classmethod
    def must_be_mag7(cls, v: str) -> str:
        v = v.upper()
        if v not in MAG7:
            raise ValueError("Choose one of the Magnificent 7: " + ", ".join(MAG7))
        return v

    @field_validator("buy_date")
    @classmethod
    def date_not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Buy date cannot be in the future")
        if v.year < 1980:
            raise ValueError("Buy date must be after 1980")
        return v

    @field_validator("notes", mode="before")
    @classmethod
    def empty_notes_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class HoldingOut(BaseModel):
    id: int
    ticker: str
    company_name: str
    shares: int
    buy_price: Decimal
    buy_date: date
    is_active: bool
    notes: Optional[str] = None
    attachment_url: Optional[str] = None
    attachment_name: Optional[str] = None
    cost: Decimal = Field(description="shares × buy_price")
    current_price: Optional[Decimal] = Field(None, description="Latest saved market price")
    price_updated_at: Optional[datetime] = None
    market_value: Optional[Decimal] = Field(None, description="shares × current_price")
    pnl: Optional[Decimal] = Field(None, description="market_value − cost")
    pnl_pct: Optional[Decimal] = Field(None, description="pnl ÷ cost × 100")


_FRIENDLY = {
    "int_parsing": "Must be a whole number",
    "int_from_float": "Must be a whole number (no decimals)",
    "decimal_parsing": "Must be a number, e.g. 189.50",
    "decimal_max_places": "Use at most 2 decimal places",
    "date_from_datetime_parsing": "Must be a date in the format YYYY-MM-DD",
    "date_parsing": "Must be a date in the format YYYY-MM-DD",
    "bool_parsing": "Must be true or false",
    "missing": "This field is required",
}


def friendly_errors(exc: ValidationError) -> dict:
    """Turn Pydantic's error list into {field: message} for the HTML form."""
    out = {}
    for err in exc.errors():
        field = str(err["loc"][-1]) if err["loc"] else "form"
        msg = _FRIENDLY.get(err["type"])
        if msg is None:
            msg = err["msg"].removeprefix("Value error, ")
        out.setdefault(field, msg)
    return out
