"""Input validation (requirement: create and update check at least 4 data types).

One Pydantic model describes a valid holding. The web forms AND the public API
both use it, so the rules are written once.

  text     -> ticker (must be one of the Magnificent 7), notes
  integer  -> shares
  decimal  -> buy_price
  date     -> buy_date
  boolean  -> is_active
"""

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services import MAG7


class HoldingIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    ticker: str = Field(description="One of: " + ", ".join(MAG7), examples=["AAPL"])
    shares: int = Field(gt=0, le=10_000_000, description="Whole number of shares", examples=[10])
    buy_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2,
                               description="Price paid per share in USD", examples=["189.50"])
    buy_date: date = Field(description="YYYY-MM-DD, not in the future", examples=["2026-03-15"])
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
    def not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Buy date cannot be in the future")
        return v

    @field_validator("notes")
    @classmethod
    def empty_to_none(cls, v):
        return v or None  # an empty form field is saved as "no notes"


# Short messages for the web form, by Pydantic error type.
FRIENDLY = {
    "int_parsing": "Must be a whole number",
    "int_from_float": "Must be a whole number",
    "decimal_parsing": "Must be a number, e.g. 189.50",
    "decimal_max_places": "Use at most 2 decimal places",
    "greater_than": "Must be greater than 0",
    "date_from_datetime_parsing": "Must be a date (YYYY-MM-DD)",
    "string_too_long": "Use at most 500 characters",
}


def friendly_errors(exc: ValidationError) -> dict:
    """Turn Pydantic's error list into {field name: message} for the HTML form."""
    errors = {}
    for err in exc.errors():
        message = FRIENDLY.get(err["type"]) or err["msg"].removeprefix("Value error, ")
        errors[str(err["loc"][0])] = message
    return errors
