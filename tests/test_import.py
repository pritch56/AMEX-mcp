"""Statement-file import provider, using synthetic Amex UK exports in tests/fixtures."""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import time
from datetime import date

import pytest

from amex_mcp.errors import AccountNotFoundError, ConfigError, InvalidRequestError, MalformedImportError
from amex_mcp.providers import statement_import
from amex_mcp.providers.statement_import import (
    MAX_CONTENT_CHARS,
    ImportOptions,
    StatementImportProvider,
    decode_statement_bytes,
    parse_csv,
    parse_ofx,
    parse_statement_text,
)

from .conftest import FIXTURES, run

CSV = (FIXTURES / "amex_uk_activity.csv").read_text(encoding="utf-8")
QFX = (FIXTURES / "amex_statement.qfx").read_text(encoding="utf-8")
OPTS = ImportOptions()
ALL = (date(2026, 1, 1), date(2027, 1, 1))


def by_desc(txns):
    return {t.description: t for t in txns}


# --------------------------------------------------------------------------- CSV


def test_csv_sign_convention_is_flipped():
    txns = parse_csv(CSV, "card", OPTS).transactions
    d = by_desc(txns)
    assert len(txns) == 7
    assert d["TESCO STORES 6021 LONDON"].amount_minor == -4567  # charge: positive in file -> negative
    assert d["EXAMPLE OUTDOORS LTD LEEDS"].amount_minor == 1999  # refund: negative in file -> positive
    assert d["PAYMENT RECEIVED - THANK YOU"].amount_minor == 25000  # payment to card -> positive
    assert d["EXAMPLE RAIL TICKETS YORK"].amount_minor == -123450  # thousands separator handled


def test_csv_uk_dates_and_fields():
    d = by_desc(parse_csv(CSV, "card", OPTS).transactions)
    tesco = d["TESCO STORES 6021 LONDON"].to_dict()
    assert tesco["date"] == "2026-10-02"  # 02/10/2026 is 2 October, not 10 February
    assert d["EXAMPLE RAIL TICKETS YORK"].date == "2026-09-13"
    assert tesco["source"] == "amex" and tesco["account_id"] == "card" and tesco["currency"] == "GBP"
    assert tesco["category"] == "General Purchases-Groceries"
    assert tesco["merchant"] is None and tesco["pending"] is False and tesco["notes"] is None
    assert d["PAYMENT RECEIVED - THANK YOU"].category is None


def test_csv_foreign_currency_charge_keeps_gbp_amount_and_notes_original():
    diner = by_desc(parse_csv(CSV, "card", OPTS).transactions)["EXAMPLE DINER NEW YORK"]
    assert diner.amount_minor == -4321 and diner.currency == "GBP"
    assert diner.notes == "foreign_amount=54.00 US DOLLARS; fx_rate=1.2871; fx_commission=1.25"


def test_csv_ids_use_reference_and_are_stable():
    a = parse_csv(CSV, "card", OPTS).transactions
    b = parse_csv(CSV, "a-different-account-label", OPTS).transactions
    assert [t.id for t in a] == [t.id for t in b]  # reference-based: independent of local labels
    assert len({t.id for t in a}) == 7  # the two identical Pret rows have distinct references


def test_csv_without_reference_derives_distinct_deterministic_ids():
    text = (FIXTURES / "amex_uk_no_reference.csv").read_text(encoding="utf-8")
    a = parse_csv(text, "card", OPTS).transactions
    b = parse_csv(text, "card", OPTS).transactions
    assert [t.id for t in a] == [t.id for t in b]
    assert len({t.id for t in a}) == 3 and a[0].description == a[1].description


def test_csv_with_bom_preamble_and_reordered_columns():
    text = "﻿American Express activity export\n\nAmount,Description,Date,Reference\n12.00,COFFEE,05/10/2026,'R1'\n"
    txns = parse_csv(text, "card", OPTS).transactions
    assert [(t.date, t.amount_minor, t.description) for t in txns] == [("2026-10-05", -1200, "COFFEE")]


def test_csv_us_date_order_option():
    text = "Date,Description,Amount\n10/13/2026,COFFEE,3.00\n"
    assert parse_csv(text, "c", ImportOptions(date_order="mdy")).transactions[0].date == "2026-10-13"
    with pytest.raises(MalformedImportError, match="AMEX_CSV_DATE_ORDER=mdy"):
        parse_csv(text, "c", OPTS)


def test_csv_inverted_sign_file_is_detected():
    text = "Date,Description,Amount\n06/10/2026,PAYMENT RECEIVED - THANK YOU,250.00\n05/10/2026,SHOP,-10.00\n"
    with pytest.raises(MalformedImportError, match="AMEX_CSV_CHARGES_POSITIVE=false"):
        parse_csv(text, "c", OPTS)
    flipped = parse_csv(text, "c", ImportOptions(csv_charges_positive=False)).transactions
    assert {t.description: t.amount_minor for t in flipped} == {"PAYMENT RECEIVED - THANK YOU": 25000, "SHOP": -1000}


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "file is empty"),
        ("   \n\n", "file is empty"),
        ("this is not,a statement\n1,2\n", "could not find a header row"),
        ("Date,Description,Amount\n32/13/2026,SHOP,1.00\n", "line 2: invalid date"),
        ("Date,Description,Amount\nyesterday,SHOP,1.00\n", "line 2: unrecognised date"),
        ("Date,Description,Amount\n05/10/2026,SHOP,twelve\n", "line 2: unrecognised amount"),
        (
            "Date,Description,Amount\n05/10/2026,SHOP,1.00\n06/10/2026,TRUNCATED\n",
            "line 3: expected at least 3 columns",
        ),
    ],
)
def test_malformed_csv_gives_clear_error(text, message):
    with pytest.raises(MalformedImportError, match=message) as info:
        parse_csv(text, "c", OPTS, name="bad.csv")
    assert str(info.value).startswith("[malformed_import_file] bad.csv")


def test_csv_header_only_is_valid_and_empty():
    assert parse_csv("Date,Description,Amount\n", "c", OPTS).transactions == []


# --------------------------------------------------------------------------- OFX


def test_qfx_signs_are_kept_and_balance_read():
    parsed = parse_ofx(QFX, "card", OPTS)
    d = by_desc(parsed.transactions)
    assert d["TESCO STORES 6021 LONDON"].amount_minor == -4567
    assert d["PAYMENT RECEIVED - THANK YOU"].amount_minor == 25000
    assert d["TESCO STORES 6021 LONDON"].date == "2026-10-02"
    assert d["TESCO STORES 6021 LONDON"].notes == "General Purchases"
    assert d["EXAMPLE DINER NEW YORK"].notes == "foreign_currency=USD; fx_rate=1.2871"
    assert parsed.currency == "GBP"
    assert parsed.balance_minor == -131245 and parsed.balance_as_of == "2026-10-08"
    assert len({t.id for t in parsed.transactions}) == 3
    assert [t.id for t in parsed.transactions] == [t.id for t in parse_ofx(QFX, "x", OPTS).transactions]


def test_ofx_xml_flavour_with_closing_tags():
    xml = (
        "<?xml version='1.0'?><OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>GBP</CURDEF>"
        "<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261002</DTPOSTED>"
        "<TRNAMT>-9.99</TRNAMT><FITID>F1</FITID><NAME>BOOKSHOP</NAME></STMTTRN></BANKTRANLIST>"
        "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"
    )
    t = parse_ofx(xml, "c", OPTS).transactions
    assert [(x.date, x.amount_minor, x.description) for x in t] == [("2026-10-02", -999, "BOOKSHOP")]


@pytest.mark.parametrize(
    "text, message",
    [
        ("just some text", "not an OFX/QFX file"),
        ("<OFX><SIGNONMSGSRSV1></SIGNONMSGSRSV1></OFX>", "no transaction list"),
        ("<OFX><BANKTRANLIST><STMTTRN><TRNAMT>-1.00<FITID>1</STMTTRN></BANKTRANLIST></OFX>", "missing <DTPOSTED>"),
        (
            "<OFX><BANKTRANLIST><STMTTRN><DTPOSTED>2026-10<TRNAMT>-1.00</STMTTRN></BANKTRANLIST></OFX>",
            "unrecognised OFX date",
        ),
        (
            "<OFX><BANKTRANLIST><STMTTRN><DTPOSTED>20261002<TRNAMT>abc</STMTTRN></BANKTRANLIST></OFX>",
            "unrecognised amount",
        ),
    ],
)
def test_malformed_ofx_gives_clear_error(text, message):
    with pytest.raises(MalformedImportError, match=message):
        parse_ofx(text, "c", OPTS, name="bad.qfx")


def test_format_is_sniffed_when_extension_is_unknown():
    assert parse_statement_text(QFX, "download").fmt == "ofx"
    assert parse_statement_text(CSV, "download.txt").fmt == "csv"
    with pytest.raises(MalformedImportError, match="empty"):
        parse_statement_text("", "x.csv")


# ---------------------------------------------------------------------- provider


@pytest.fixture
def import_dir(tmp_path):
    shutil.copy(FIXTURES / "amex_uk_activity.csv", tmp_path / "activity-oct.csv")
    return tmp_path


def test_provider_lists_filters_and_sorts(import_dir):
    p = StatementImportProvider(import_dir)
    accounts = run(p.list_accounts())
    assert [a.account_id for a in accounts] == ["amex-import"] and accounts[0].provider == "import"
    page = run(p.list_transactions("amex-import", date(2026, 10, 3), date(2026, 10, 6)))
    # statement files hold booked items only: the provider says it cannot report pending
    assert page.pending_supported is False and page.pending_excluded == 0 and page.earliest_pending_date is None
    week = page.transactions
    assert [t.date for t in week] == ["2026-10-03", "2026-10-03", "2026-10-04", "2026-10-05"]
    assert len(run(p.list_transactions("amex-import", *ALL)).transactions) == 7
    assert run(p.list_accounts())[0].to_dict()["id"] == "amex-import"


def test_overlapping_exports_are_deduplicated(import_dir):
    shutil.copy(FIXTURES / "amex_uk_activity.csv", import_dir / "activity-oct-again.csv")
    (import_dir / "newer.csv").write_text(
        "Date,Description,Amount,Reference\n07/10/2026,NEW SHOP,5.00,'AT-NEW-1'\n"
        "02/10/2026,TESCO STORES 6021          LONDON,45.67,'AT262750012000010000001'\n",
        encoding="utf-8",
    )
    txns = run(StatementImportProvider(import_dir).list_transactions("amex-import", *ALL)).transactions
    assert len(txns) == 8 and txns[-1].description == "NEW SHOP"


def test_subfolders_are_separate_accounts_and_qfx_gives_balance(import_dir):
    (import_dir / "gold").mkdir()
    shutil.copy(FIXTURES / "amex_statement.qfx", import_dir / "gold" / "statement.qfx")
    p = StatementImportProvider(import_dir)
    assert [a.account_id for a in run(p.list_accounts())] == ["amex-import", "gold"]
    gold = run(p.get_balance("gold"))
    assert gold.balance_minor == -131245 and gold.as_of == "2026-10-08"
    assert all(t.account_id == "gold" for t in run(p.list_transactions("gold", *ALL)).transactions)
    csv_balance = run(p.get_balance("amex-import"))
    assert csv_balance.balance_minor is None and "no balance" in csv_balance.notes


def test_provider_errors(import_dir, tmp_path):
    p = StatementImportProvider(import_dir)
    with pytest.raises(AccountNotFoundError, match="list_accounts"):
        run(p.list_transactions("nope", *ALL))
    with pytest.raises(InvalidRequestError):
        run(p.list_transactions("../etc", *ALL))
    with pytest.raises(ConfigError, match="does not exist"):
        run(StatementImportProvider(tmp_path / "missing").list_accounts())
    with pytest.raises(ConfigError, match="AMEX_IMPORT_DIR"):
        StatementImportProvider("")


def test_malformed_file_in_folder_names_the_file(import_dir):
    (import_dir / "broken.csv").write_text("Date,Description,Amount\n05/10/2026,SHOP,oops\n", encoding="utf-8")
    with pytest.raises(MalformedImportError, match=r"broken\.csv line 2"):
        run(StatementImportProvider(import_dir).list_transactions("amex-import", *ALL))


def test_binary_file_is_rejected(import_dir):
    (import_dir / "x.csv").write_bytes(b"\x81\x8d\x8f\x90\x9d" * 10)
    with pytest.raises(MalformedImportError, match=r"not a text file|header row"):
        run(StatementImportProvider(import_dir).list_transactions("amex-import", *ALL))


def test_connection_status_reports_freshness(import_dir, tmp_path):
    s = run(StatementImportProvider(import_dir, stale_after_days=100000).connection_status())
    assert s["connected"] is True and s["needs_reauthorisation"] is False and s["consent_expires_at"] is None
    assert s["data_through"] == "2026-10-06" and s["file_count"] == 1
    stale = run(StatementImportProvider(import_dir, stale_after_days=0).connection_status())
    assert "stale" in stale["warning"] and s["warning"] is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(StatementImportProvider(empty).connection_status())["connected"] is False
    assert run(StatementImportProvider(tmp_path / "missing").connection_status())["error"] == "config_error"


def test_options_from_env():
    o = ImportOptions.from_env(
        {"AMEX_CSV_CHARGES_POSITIVE": "false", "AMEX_IMPORT_CURRENCY": "eur", "AMEX_CSV_DATE_ORDER": "MDY"}
    )
    assert (o.csv_charges_positive, o.currency, o.date_order) == (False, "EUR", "mdy")
    with pytest.raises(ConfigError):
        ImportOptions.from_env({"AMEX_CSV_DATE_ORDER": "ymd"})


# ------------------------------------------------------- hardening / regressions


def test_cp1252_and_latin1_exports_are_decoded(import_dir):
    text = "Date,Description,Amount,Reference\n05/10/2026,CAF\u00c9 \u20ac ROUGE,3.00,'R-1'\n"
    (import_dir / "activity-oct.csv").write_bytes(text.encode("cp1252"))
    txns = run(StatementImportProvider(import_dir).list_transactions("amex-import", *ALL)).transactions
    assert txns[0].description == "CAF\u00c9 \u20ac ROUGE"
    # 0x81 is undefined in cp1252: fall back to latin-1 instead of failing
    assert "CAF\u00c9" in decode_statement_bytes(b"CAF\xc9 \x81", "x.csv")
    assert decode_statement_bytes("\ufeffDate".encode("utf-8")) == "Date"
    assert decode_statement_bytes("Date,Amount".encode("utf-16")) == "Date,Amount"


def test_file_with_nul_bytes_is_rejected_as_binary(import_dir):
    (import_dir / "x.csv").write_bytes(b"PK\x03\x04\x00\x00binary")
    with pytest.raises(MalformedImportError, match="not a text file"):
        run(StatementImportProvider(import_dir).list_transactions("amex-import", *ALL))


@pytest.mark.parametrize("amount", ["1e999999999", "-1E+999999999", "NaN", "Infinity", "9" * 400, "1e30"])
def test_absurd_amounts_are_malformed_file_errors_not_internal_errors(amount):
    with pytest.raises(MalformedImportError, match=r"\[malformed_import_file\] bad.csv line 2: unrecognised amount"):
        parse_csv(f"Date,Description,Amount\n05/10/2026,SHOP,{amount}\n", "c", OPTS, name="bad.csv")
    ofx = f"<OFX><BANKTRANLIST><STMTTRN><DTPOSTED>20261002<TRNAMT>{amount}</STMTTRN></BANKTRANLIST></OFX>"
    with pytest.raises(MalformedImportError, match="unrecognised amount"):
        parse_ofx(ofx, "c", OPTS, name="bad.qfx")
    ledger = f"<OFX><BANKTRANLIST></BANKTRANLIST><LEDGERBAL><BALAMT>{amount}</LEDGERBAL></OFX>"
    with pytest.raises(MalformedImportError, match="unrecognised amount"):
        parse_ofx(ledger, "c", OPTS, name="bad.qfx")


def test_error_messages_do_not_echo_long_or_control_input():
    with pytest.raises(MalformedImportError) as info:
        parse_csv("Date,Description,Amount\n05/10/2026,SHOP,\x1b[31m" + "9" * 5000 + "x\n", "c", OPTS)
    assert len(str(info.value)) < 200 and "\x1b" not in str(info.value)


def _hostile_seconds(n: int) -> float:
    """Best-of-three time to parse the hostile OFX shapes at size factor n."""
    shapes = [
        "<OFX><BANKTRANLIST></BANKTRANLIST>" + "<LEDGERBAL>" * n,  # the reported quadratic case
        "<OFX><BANKTRANLIST></BANKTRANLIST>" + "<LEDGERBAL><BALAMT>" * n,
        "<OFX><BANKTRANLIST>" + "<STMTTRN>" * n,
        "<OFX>" + "<STMTTRN><DTPOSTED>20261002<TRNAMT>1" * n,
        "<OFX>" + "<" * (n * 10),
    ]
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        for text in shapes:
            with contextlib.suppress(MalformedImportError):
                parse_ofx(text, "c", OPTS)
        best = min(best, time.perf_counter() - start)
    return best


def test_ofx_parser_is_linear_on_hostile_input():
    # 16k repetitions of <LEDGERBAL> is ~172 KiB: the input that used to block for ~22 s.
    small, large = _hostile_seconds(16_000), _hostile_seconds(64_000)
    assert large < 5.0, f"hostile 64k input took {large:.2f}s"
    # 4x the input: linear growth is ~4x, quadratic would be ~16x. Allow generous noise.
    assert large < max(small, 0.02) * 10, f"super-linear growth: {small:.3f}s -> {large:.3f}s"


def test_ledger_balance_without_closing_tag_is_still_read():
    text = "<OFX><CURDEF>GBP<BANKTRANLIST></BANKTRANLIST><LEDGERBAL><BALAMT>-12.34<DTASOF>20261008"
    parsed = parse_ofx(text, "c", OPTS)
    assert parsed.balance_minor == -1234 and parsed.balance_as_of == "2026-10-08"


def test_oversized_content_is_rejected_before_parsing():
    with pytest.raises(MalformedImportError, match="larger than 10 MiB"):
        parse_statement_text("x" * (MAX_CONTENT_CHARS + 1), "big.csv")


def test_oversized_file_is_rejected(import_dir, monkeypatch):
    monkeypatch.setattr(statement_import, "MAX_FILE_BYTES", 50)
    with pytest.raises(MalformedImportError, match="file is larger than"):
        run(StatementImportProvider(import_dir).list_transactions("amex-import", *ALL))


def test_rows_sharing_a_reference_but_differing_are_both_kept(import_dir):
    (import_dir / "activity-oct.csv").write_text(
        "Date,Description,Amount,Reference\n"
        "05/10/2026,HOTEL DEPOSIT,50.00,'DUP-1'\n"
        "06/10/2026,HOTEL BALANCE,150.00,'DUP-1'\n"
        "05/10/2026,HOTEL DEPOSIT,50.00,'DUP-1'\n",
        encoding="utf-8",
    )
    txns = run(StatementImportProvider(import_dir).list_transactions("amex-import", *ALL)).transactions
    assert [(t.description, t.amount_minor) for t in txns] == [("HOTEL DEPOSIT", -5000), ("HOTEL BALANCE", -15000)]
    assert txns[0].id != txns[1].id


def test_parsed_files_are_cached_until_they_change(import_dir):
    p = StatementImportProvider(import_dir)
    path = import_dir / "activity-oct.csv"
    for _ in range(3):
        run(p.list_transactions("amex-import", *ALL))
        run(p.get_balance("amex-import"))
        run(p.connection_status())
    assert p.parse_count == 1

    path.write_text("Date,Description,Amount\n05/10/2026,ONLY ROW,1.00\n", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))  # make the change unmistakable
    txns = run(p.list_transactions("amex-import", *ALL)).transactions
    assert [t.description for t in txns] == ["ONLY ROW"] and p.parse_count == 2

    path.unlink()
    with pytest.raises(AccountNotFoundError):
        run(p.list_transactions("amex-import", *ALL))
    assert p._cache == {}


def test_parsing_runs_off_the_event_loop_thread(import_dir, monkeypatch):
    seen: list[int] = []
    real = statement_import.parse_statement_text

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(statement_import, "parse_statement_text", spy)

    async def go():
        loop_thread = threading.get_ident()
        await StatementImportProvider(import_dir).list_transactions("amex-import", *ALL)
        return loop_thread

    loop_thread = run(go())
    assert seen and all(ident != loop_thread for ident in seen)


def test_connection_status_does_not_raise_for_a_malformed_file(import_dir):
    (import_dir / "broken.csv").write_text("Date,Description,Amount\n05/10/2026,SHOP,oops\n", encoding="utf-8")
    s = run(StatementImportProvider(import_dir).connection_status())
    assert s["connected"] is False and s["warning"].startswith("[malformed_import_file] broken.csv")


@pytest.mark.parametrize("bad", ["../etc", "a/b", "a\\b", "C:", "", "x" * 300, "nul\x00"])
def test_import_account_ids_are_validated(import_dir, bad):
    with pytest.raises(InvalidRequestError, match=r"\[invalid_request\]"):
        run(StatementImportProvider(import_dir).get_balance(bad))


def test_ofx_sign_option_from_env():
    o = ImportOptions.from_env({"AMEX_OFX_CHARGES_NEGATIVE": "false"})
    assert o.ofx_charges_negative is False
    parsed = parse_ofx(QFX, "card", o)
    assert by_desc(parsed.transactions)["TESCO STORES 6021 LONDON"].amount_minor == 4567
