"""Normalised data shapes shared by every provider.

Transaction shape:

    {id, source, account_id, date, amount_minor, currency, description,
     merchant, category, pending, notes}

Sign convention: amount_minor is a signed integer in minor units (pence).
NEGATIVE = money out (a card charge). POSITIVE = money in to the card
(a refund, or a payment made to the card).

Dates are calendar dates in Europe/London local time.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import Any
from zoneinfo import ZoneInfo

from . import SOURCE
from .errors import InvalidRequestError

LONDON = ZoneInfo("Europe/London")

# ISO 4217 minor-unit exponents for every currency whose exponent is NOT 2.
# Anything not listed here has two decimal places (GBP, EUR, USD, HUF, ...).
ISO4217_NON_DEFAULT_EXPONENTS: dict[str, int] = {
    # zero decimals
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "UYI": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    # three decimals
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
    # four decimals
    "CLF": 4,
    "UYW": 4,
}

# Largest amount accepted, in major units. Anything bigger is a parsing accident or an attack.
MAX_ABS_AMOUNT = Decimal(10) ** 12

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def currency_exponent(currency: str) -> int:
    return ISO4217_NON_DEFAULT_EXPONENTS.get((currency or "").upper(), 2)


def to_minor(amount: Any, currency: str = "GBP") -> int:
    """Convert a decimal amount (str / int / float / Decimal) to integer minor units.

    Floats are routed through str() so 24.25 does not become 2424.999...
    Raises ValueError for anything that is not a finite, sensibly sized number.
    """
    try:
        d = amount if isinstance(amount, Decimal) else Decimal(str(amount).replace(",", "").strip())
        if not d.is_finite() or abs(d) > MAX_ABS_AMOUNT:
            raise ValueError
        scaled = d * (Decimal(10) ** currency_exponent(currency))
        return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError, TypeError):
        raise ValueError(f"not a usable number: {str(amount)[:40]!r}") from None


def clean_text(value: Any) -> str:
    """Drop control characters and collapse runs of whitespace (Amex pads descriptions)."""
    return re.sub(r"\s+", " ", _CONTROL_RE.sub(" ", str(value or ""))).strip()


def stable_id(*parts: Any) -> str:
    """Deterministic id from the given parts: 'amex_' + 24 hex chars of SHA-256."""
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return f"{SOURCE}_{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:24]}"


def london_today() -> date:
    return datetime.now(LONDON).date()


def london_date(value: Any) -> str:
    """Calendar date (YYYY-MM-DD) in Europe/London for a provider timestamp.

    A timestamp carrying a UTC offset is converted; a bare date or a timestamp without an
    offset is taken at face value. Raises ValueError when the value is not a date/time.
    """
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    # Python 3.11's fromisoformat handles "Z" and fractional seconds of any length.
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(LONDON)
    return dt.date().isoformat()


def utc_z(value: Any) -> str | None:
    """Normalise a provider timestamp to ISO 8601 UTC ending in 'Z' (None if unparseable)."""
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Transaction:
    id: str
    account_id: str
    date: str  # YYYY-MM-DD, Europe/London
    amount_minor: int
    currency: str
    description: str
    merchant: str | None = None
    category: str | None = None
    pending: bool = False
    notes: str | None = None
    source: str = SOURCE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"] = d["notes"] or None  # never ""
        order = [
            "id",
            "source",
            "account_id",
            "date",
            "amount_minor",
            "currency",
            "description",
            "merchant",
            "category",
            "pending",
            "notes",
        ]
        return {k: d[k] for k in order}

    def content_key(self) -> tuple[str, int, str, str]:
        return (self.date, self.amount_minor, self.currency, self.description)


@dataclass
class TransactionPage:
    """Result of Provider.list_transactions.

    pending_excluded: pending items inside the window that were left out (0 when pending
    items were included). earliest_pending_date: earliest date of any pending item inside
    the window, included or not.
    """

    transactions: list[Transaction] = field(default_factory=list)
    pending_excluded: int = 0
    earliest_pending_date: str | None = None
    pending_supported: bool = True


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    currency: str
    type: str = "credit_card"
    card_network: str | None = "AMEX"
    last4: str | None = None
    provider: str = ""
    source: str = SOURCE

    def to_dict(self) -> dict[str, Any]:
        # `id` is the contract field; `account_id` is kept as a duplicate for compatibility.
        return {"id": self.account_id, **asdict(self)}


@dataclass(frozen=True)
class Balance:
    """Card balance. `balance_minor` follows the transaction sign convention:
    NEGATIVE means the cardholder owes money on the card."""

    account_id: str
    currency: str
    balance_minor: int | None
    available_credit_minor: int | None = None
    credit_limit_minor: int | None = None
    last_statement_balance_minor: int | None = None
    last_statement_date: str | None = None
    payment_due_minor: int | None = None
    payment_due_date: str | None = None
    as_of: str | None = None
    notes: str | None = None
    source: str = SOURCE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"] = d["notes"] or None
        return d


def parse_bound(value: str | None, name: str) -> date | None:
    """Parse a `since` / `before` tool argument into a Europe/London calendar date.

    Accepts YYYY-MM-DD, or an ISO 8601 datetime (converted to London when it has an offset).
    """
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(london_date(text))
    except ValueError:
        raise InvalidRequestError(
            f"'{name}' must be an ISO 8601 date or datetime (e.g. 2026-10-03 or "
            f"2026-10-03T00:00:00Z); got {clean_text(text)[:40]!r}."
        ) from None


def resolve_window(since: str | None, before: str | None, max_range_days: int) -> tuple[date, date]:
    """Apply the defaults and limits of list_transactions: since <= date < before.

    before defaults to tomorrow (London), since to before - 30 days.
    """
    before_d = parse_bound(before, "before") or (london_today() + timedelta(days=1))
    since_d = parse_bound(since, "since") or (before_d - timedelta(days=30))
    if since_d >= before_d:
        raise InvalidRequestError("'since' must be earlier than 'before'.")
    span = (before_d - since_d).days
    if span > max_range_days:
        raise InvalidRequestError(
            f"The requested range is {span} days; the maximum is {max_range_days} "
            "(AMEX_MAX_RANGE_DAYS). Ask for a shorter range."
        )
    return since_d, before_d


def date_in_window(iso_date: str, since: date | None, before: date | None) -> bool:
    """since is inclusive, before is exclusive."""
    d = date.fromisoformat(iso_date[:10])
    if since is not None and d < since:
        return False
    return not (before is not None and d >= before)


def merge_transactions(
    rows: list[Transaction], log: Any = None, into: dict[str, Transaction] | None = None
) -> dict[str, Transaction]:
    """Merge rows by id without losing data.

    Two rows with the same id and the same (date, amount, currency, description) are one
    transaction seen twice (overlapping windows / overlapping exports) and are collapsed.
    Two rows with the same id but different content are both kept: the later one gets a
    content-plus-sequence id, and a warning is logged.
    """
    out: dict[str, Transaction] = {} if into is None else into
    for t in rows:
        existing = out.get(t.id)
        if existing is None:
            out[t.id] = t
            continue
        if existing.content_key() == t.content_key() and existing.pending == t.pending:
            continue
        seq = 0
        while True:
            new_id = stable_id("dup", t.id, *t.content_key(), seq)
            clash = out.get(new_id)
            if clash is None or clash.content_key() == t.content_key():
                break
            seq += 1
        if clash is None:
            if log is not None:
                log.warning(
                    "two different transactions share one provider id; keeping both "
                    "(the later one was given a content-derived id)"
                )
            out[new_id] = replace(t, id=new_id)
    return out
