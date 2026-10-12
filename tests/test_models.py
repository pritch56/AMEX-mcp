import logging
from datetime import UTC, date, datetime

import pytest

from amex_mcp.errors import InvalidRequestError
from amex_mcp.models import (
    ISO4217_NON_DEFAULT_EXPONENTS,
    Account,
    Transaction,
    clean_text,
    currency_exponent,
    date_in_window,
    merge_transactions,
    parse_bound,
    resolve_window,
    stable_id,
    to_minor,
)


def test_to_minor_avoids_float_error():
    assert to_minor(24.25) == 2425
    assert to_minor(0.07) == 7
    assert to_minor(1312.45) == 131245
    assert to_minor("1,234.50") == 123450
    assert to_minor(-19.99) == -1999
    assert to_minor(5000) == 500000


def test_to_minor_currency_exponents():
    assert to_minor("1200", "JPY") == 1200
    assert to_minor("1.234", "KWD") == 1234
    assert to_minor("10.005", "GBP") == 1001  # half-up


def test_to_minor_rejects_garbage():
    with pytest.raises(ValueError):
        to_minor("abc")


def test_stable_id_is_deterministic_and_prefixed():
    a = stable_id("ref", "AT123")
    assert a == stable_id("ref", "AT123")
    assert a != stable_id("ref", "AT124")
    assert a.startswith("amex_") and len(a) == 5 + 24


def test_clean_text_collapses_amex_padding():
    assert clean_text("TESCO STORES 6021          LONDON\n") == "TESCO STORES 6021 LONDON"


def test_transaction_dict_has_exact_contract_keys():
    t = Transaction(
        id="amex_x", account_id="a", date="2026-10-02", amount_minor=-4567, currency="GBP", description="TESCO"
    )
    assert list(t.to_dict()) == [
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
    assert t.to_dict()["source"] == "amex"
    assert t.to_dict()["merchant"] is None and t.to_dict()["pending"] is False


def test_parse_bound_gives_london_dates():
    assert parse_bound("2026-10-03", "since") == date(2026, 10, 3)
    assert parse_bound("2026-10-03T12:30:00Z", "since") == date(2026, 10, 3)
    assert parse_bound("2026-07-03T23:30:00Z", "since") == date(2026, 7, 4)  # BST
    assert parse_bound("2026-10-03T00:30:00+02:00", "since") == date(2026, 10, 2)
    assert parse_bound(None, "since") is None and parse_bound("  ", "since") is None
    for bad in ("03/10/2026", "last tuesday", "2026-13-01", "2026-10-03\x1b[31m"):
        with pytest.raises(InvalidRequestError, match="ISO 8601") as info:
            parse_bound(bad, "since")
        assert "\x1b" not in str(info.value)


def test_window_since_inclusive_before_exclusive():
    since, before = date(2026, 10, 3), date(2026, 10, 10)
    assert not date_in_window("2026-10-02", since, before)
    assert date_in_window("2026-10-03", since, before)
    assert date_in_window("2026-10-09", since, before)
    assert not date_in_window("2026-10-10", since, before)


def test_resolve_window_defaults_and_limits(monkeypatch):
    monkeypatch.setattr("amex_mcp.models.london_today", lambda: date(2026, 10, 11))
    assert resolve_window(None, None, 400) == (date(2026, 9, 12), date(2026, 10, 12))  # tomorrow - 30 days
    assert resolve_window("2026-10-01", None, 400) == (date(2026, 10, 1), date(2026, 10, 12))
    assert resolve_window(None, "2026-10-01", 400) == (date(2026, 9, 1), date(2026, 10, 1))
    assert resolve_window("2025-09-01", "2026-10-06", 400) == (date(2025, 9, 1), date(2026, 10, 6))  # exactly 400
    with pytest.raises(InvalidRequestError, match="maximum is 400"):
        resolve_window("2025-08-31", "2026-10-06", 400)
    with pytest.raises(InvalidRequestError, match="maximum is 7"):
        resolve_window("2026-10-01", "2026-10-09", 7)
    for since, before in (("2026-10-08", "2026-10-01"), ("2026-10-01", "2026-10-01")):
        with pytest.raises(InvalidRequestError, match="earlier than"):
            resolve_window(since, before, 400)


def test_london_today_uses_london_not_utc(monkeypatch):
    import amex_mcp.models as models

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 1, 23, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(models, "datetime", Clock)
    assert models.london_today() == date(2026, 7, 2)  # 00:30 BST


def test_iso4217_exponents_table():
    # two decimals unless listed; HUF has two (it was wrongly treated as zero-decimal)
    assert currency_exponent("GBP") == currency_exponent("huf") == currency_exponent("") == 2
    assert to_minor("1234.56", "HUF") == 123456
    for code in ("JPY", "KRW", "VND", "CLP", "ISK", "XAF", "XOF", "XPF", "UGX", "PYG"):
        assert ISO4217_NON_DEFAULT_EXPONENTS[code] == 0
    for code in ("BHD", "KWD", "OMR", "JOD", "TND"):
        assert ISO4217_NON_DEFAULT_EXPONENTS[code] == 3
    assert "HUF" not in ISO4217_NON_DEFAULT_EXPONENTS
    assert to_minor("1200", "ISK") == 1200 and to_minor("1.2345", "TND") == 1235


@pytest.mark.parametrize("bad", ["1e999999999", "-1e999999999", "NaN", "Infinity", "-inf", "1e30", "", "12.3.4", None])
def test_to_minor_overflow_and_non_finite_are_value_errors(bad):
    with pytest.raises(ValueError):
        to_minor(bad)


def test_clean_text_strips_control_characters():
    assert clean_text("EVIL\x1b[2J\x00SHOP\x07\r\n  LTD\x9b") == "EVIL [2J SHOP LTD"


def test_account_has_id_and_name_and_notes_are_never_empty_strings():
    a = Account(account_id="card-1", name="Gold", currency="GBP").to_dict()
    assert a["id"] == a["account_id"] == "card-1" and a["name"] == "Gold"
    t = Transaction(
        id="x", account_id="a", date="2026-10-02", amount_minor=1, currency="GBP", description="d", notes=""
    )
    assert t.to_dict()["notes"] is None


def _tx(id_, day="2026-10-02", amount=-100, description="SHOP"):
    return Transaction(id=id_, account_id="a", date=day, amount_minor=amount, currency="GBP", description=description)


def test_merge_keeps_rows_that_share_an_id_but_differ(caplog):
    rows = [
        _tx("amex_1"),
        _tx("amex_1"),
        _tx("amex_1", amount=-250),
        _tx("amex_1", day="2026-10-03"),
        _tx("amex_1", description="OTHER"),
        _tx("amex_1", amount=-250),
        _tx("amex_2"),
    ]
    with caplog.at_level("WARNING"):
        merged = list(merge_transactions(rows, logging.getLogger("t")).values())
    assert len(merged) == 5  # 4 distinct contents under amex_1 + amex_2; exact repeats collapsed
    assert len({t.id for t in merged}) == 5 and merged[0].id == "amex_1"
    assert "share one provider id" in caplog.text
    # deterministic ids, independent of how often the duplicates repeat
    again = list(merge_transactions(rows + rows).values())
    assert [t.id for t in again] == [t.id for t in merged]
