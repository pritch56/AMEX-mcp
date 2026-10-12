"""Statement-file import provider: reads Amex UK exports from a directory.

Supported: CSV ("Download your statement data" / activity export) and OFX / QFX.
(Amex's Excel export is the same table as the CSV - save it as CSV.)

Layout of AMEX_IMPORT_DIR:
    <dir>/*.csv|*.ofx|*.qfx          -> account AMEX_IMPORT_ACCOUNT_ID (default "amex-import")
    <dir>/<name>/*.csv|*.ofx|*.qfx   -> account "<name>" (one sub-folder per card)

Overlapping exports are fine: rows are de-duplicated by their stable id (two rows that
share an id but differ in date, amount or description are both kept).

Sign conventions handled here:
  * Amex CSV: charges are POSITIVE, payments/refunds NEGATIVE  -> flipped.
  * OFX/QFX:  charges are NEGATIVE (standard OFX)              -> kept.
Both can be overridden (AMEX_CSV_CHARGES_POSITIVE / AMEX_OFX_CHARGES_NEGATIVE).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import anyio

from ..errors import AccountNotFoundError, AmexMcpError, ConfigError, InvalidRequestError, MalformedImportError
from ..models import (
    Account,
    Balance,
    Transaction,
    TransactionPage,
    clean_text,
    date_in_window,
    london_today,
    merge_transactions,
    stable_id,
    to_minor,
)
from .base import Provider

log = logging.getLogger("amex_mcp.import")

EXTENSIONS = {".csv": "csv", ".ofx": "ofx", ".qfx": "ofx"}
DEFAULT_ACCOUNT_ID = "amex-import"
# Largest statement file, and largest `content` argument, that will be parsed.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CONTENT_CHARS = MAX_FILE_BYTES
# Free-text cells longer than this are truncated before any pattern is applied to them.
MAX_CELL_CHARS = 2000

# Normalised header name -> canonical field. Amex has changed header wording over time
# and between the UK and US sites, so several spellings are accepted.
_HEADER_ALIASES = {
    "date": "date",
    "transaction date": "date",
    "date processed": "date_processed",
    "process date": "date_processed",
    "description": "description",
    "amount": "amount",
    "extended details": "extended",
    "appears on your statement as": "statement_as",
    "address": "address",
    "town/city": "city",
    "city/state": "city",
    "postcode": "postcode",
    "zip code": "postcode",
    "country": "country",
    "reference": "reference",
    "category": "category",
    "card member": "card_member",
    "cardmember": "card_member",
    "account #": "card_account",
    "card number": "card_account",
}
_PAYMENT_RE = re.compile(r"PAYMENT RECEIVED|PAYMENT\s*-?\s*THANK YOU|DIRECT DEBIT PAYMENT", re.I)
_FOREIGN_RE = re.compile(
    r"Foreign Spend Amount:\s*([\d.,]+)\s*([A-Za-z ]+?)(?=\s+Commission Amount:|\s+Currency Exchange Rate:|$)",
    re.I,
)
_COMMISSION_RE = re.compile(r"Commission Amount:\s*([\d.,]+)", re.I)
_RATE_RE = re.compile(r"Currency Exchange Rate:\s*([\d.,]+)", re.I)


@dataclass(frozen=True)
class ImportOptions:
    currency: str = "GBP"
    csv_charges_positive: bool = True
    ofx_charges_negative: bool = True
    date_order: str = "dmy"  # "dmy" (UK) or "mdy" (US)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ImportOptions:
        e = os.environ if env is None else env
        order = e.get("AMEX_CSV_DATE_ORDER", "dmy").strip().lower()
        if order not in ("dmy", "mdy"):
            raise ConfigError("AMEX_CSV_DATE_ORDER must be 'dmy' or 'mdy'.")
        return cls(
            currency=(e.get("AMEX_IMPORT_CURRENCY") or "GBP").strip().upper(),
            csv_charges_positive=_truthy(e.get("AMEX_CSV_CHARGES_POSITIVE", "true")),
            ofx_charges_negative=_truthy(e.get("AMEX_OFX_CHARGES_NEGATIVE", "true")),
            date_order=order,
        )


@dataclass
class ParsedStatement:
    transactions: list[Transaction]
    balance_minor: int | None = None
    balance_as_of: str | None = None
    currency: str = "GBP"
    fmt: str = "csv"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- CSV


def _parse_date(text: str, order: str, where: str) -> str:
    t = text.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", t)
        if m:
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            if y < 100:
                y += 2000
            d, mo = (a, b) if order == "dmy" else (b, a)
        else:
            for fmt in ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d %b %y"):
                try:
                    return datetime.strptime(t, fmt).date().isoformat()
                except ValueError:
                    continue
            raise MalformedImportError(f"{where}: unrecognised date {_show(t)}.")
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        hint = " If this is a US-format export set AMEX_CSV_DATE_ORDER=mdy." if order == "dmy" else ""
        raise MalformedImportError(f"{where}: invalid date {_show(t)} for day/month order '{order}'.{hint}") from None


def _parse_amount(text: str, currency: str, where: str) -> int:
    t = text.strip().replace("£", "").replace("$", "").replace("€", "").replace(",", "").replace(" ", "")
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1]
    if t.upper().endswith("CR"):
        negative, t = True, t[:-2]
    try:
        value = to_minor(t, currency)
    except ValueError:
        raise MalformedImportError(f"{where}: unrecognised amount {_show(text)}.") from None
    return -value if negative else value


def _show(value: str) -> str:
    """A short, control-character-free rendering of untrusted input for error messages."""
    return repr(clean_text(value)[:40])


def _foreign_note(extended: str) -> str | None:
    if not extended:
        return None
    flat = clean_text(extended[:MAX_CELL_CHARS])
    m = _FOREIGN_RE.search(flat)
    if not m:
        return None
    parts = [f"foreign_amount={m[1]} {clean_text(m[2])}"]
    rate = _RATE_RE.search(flat)
    if rate:
        parts.append(f"fx_rate={rate[1]}")
    commission = _COMMISSION_RE.search(flat)
    if commission:
        parts.append(f"fx_commission={commission[1]}")
    return "; ".join(parts)


def parse_csv(text: str, account_id: str, options: ImportOptions, name: str = "statement.csv") -> ParsedStatement:
    text = text.lstrip("﻿")
    if not text.strip():
        raise MalformedImportError(f"{name}: file is empty.")
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise MalformedImportError(f"{name}: not valid CSV ({exc}).") from None

    header_idx, columns = None, {}
    for i, row in enumerate(rows[:30]):  # Amex sometimes puts a preamble above the table
        mapped = {}
        for j, cell in enumerate(row):
            canonical = _HEADER_ALIASES.get(clean_text(cell).lower())
            if canonical and canonical not in mapped:
                mapped[canonical] = j
        if {"date", "description", "amount"} <= mapped.keys():
            header_idx, columns = i, mapped
            break
    if header_idx is None:
        raise MalformedImportError(
            f"{name}: could not find a header row with Date, Description and Amount columns. "
            "Export from Amex as CSV (tick 'include all additional transaction details') and do not edit the file."
        )

    def cell(row: list[str], field: str) -> str:
        j = columns.get(field)
        return row[j].strip() if j is not None and j < len(row) else ""

    width = max(columns["date"], columns["description"], columns["amount"]) + 1
    parsed: list[dict[str, Any]] = []
    for offset, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        if not any(c.strip() for c in row):
            continue
        where = f"{name} line {offset}"
        if len(row) < width:
            raise MalformedImportError(f"{where}: expected at least {width} columns, found {len(row)}.")
        if not cell(row, "date") and not cell(row, "amount"):
            continue
        raw_amount = _parse_amount(cell(row, "amount"), options.currency, where)
        parsed.append(
            {
                "date": _parse_date(cell(row, "date"), options.date_order, where),
                "amount_minor": -raw_amount if options.csv_charges_positive else raw_amount,
                "description": clean_text(cell(row, "description")),
                "reference": clean_text(cell(row, "reference")).strip("'\" "),
                "category": clean_text(cell(row, "category")) or None,
                "extended": cell(row, "extended"),
                "card_member": clean_text(cell(row, "card_member")),
            }
        )

    # Safety net against a silently inverted file: payments to the card must come out positive.
    payments = [p for p in parsed if _PAYMENT_RE.search(p["description"])]
    if payments and all(p["amount_minor"] < 0 for p in payments):
        raise MalformedImportError(
            f"{name}: payments to the card appear as money out, so this file's sign convention is the "
            "opposite of what is configured. Set AMEX_CSV_CHARGES_POSITIVE="
            f"{'false' if options.csv_charges_positive else 'true'} and retry."
        )

    seen: dict[tuple, int] = {}
    txns: list[Transaction] = []
    for p in parsed:
        if p["reference"]:
            txn_id = stable_id("ref", p["reference"])
        else:
            key = ("row", account_id, p["date"], p["amount_minor"], options.currency, p["description"])
            n = seen.get(key, 0)
            seen[key] = n + 1
            txn_id = stable_id(*key, n)
        notes = [n for n in (_foreign_note(p["extended"]),) if n]
        if p["card_member"]:
            notes.append(f"card_member={p['card_member']}")
        txns.append(
            Transaction(
                id=txn_id,
                account_id=account_id,
                date=p["date"],
                amount_minor=p["amount_minor"],
                currency=options.currency,
                description=p["description"],
                merchant=None,
                category=p["category"],
                pending=False,
                notes="; ".join(notes) or None,
            )
        )
    return ParsedStatement(transactions=txns, currency=options.currency, fmt="csv")


# --------------------------------------------------------------------------- OFX


def _ofx_tag(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.I)
    return m[1].strip() if m and m[1].strip() else None


_STMTTRN_OPEN = re.compile(r"<STMTTRN>", re.I)
_STMTTRN_END = re.compile(r"</STMTTRN>|<STMTTRN>|</BANKTRANLIST>", re.I)
_LEDGER_OPEN = re.compile(r"<LEDGERBAL>", re.I)
_LEDGER_END = re.compile(r"</LEDGERBAL>|<AVAILBAL>|</CCSTMTRS>|</STMTRS>", re.I)


def _ofx_blocks(text: str) -> list[str]:
    """Bodies of every <STMTTRN> element. Each block ends at its closing tag, the next
    <STMTTRN>, the end of the list, or the end of the file. One forward pass: linear time."""
    blocks: list[str] = []
    pos = 0
    while True:
        start = _STMTTRN_OPEN.search(text, pos)
        if start is None:
            return blocks
        end = _STMTTRN_END.search(text, start.end())
        if end is None:
            blocks.append(text[start.end() :])
            return blocks
        blocks.append(text[start.end() : end.start()])
        pos = end.start() if end.group().upper() == "<STMTTRN>" else end.end()


def _ofx_ledger(text: str) -> str | None:
    """Body of the first <LEDGERBAL> element: up to its end tag, or the end of the file.
    A single forward scan (the previous lazy-lookahead pattern was quadratic on hostile input)."""
    start = _LEDGER_OPEN.search(text)
    if start is None:
        return None
    end = _LEDGER_END.search(text, start.end())
    return text[start.end() : end.start() if end else len(text)]


def _ofx_date(value: str, where: str) -> str:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", value)
    if not m:
        raise MalformedImportError(f"{where}: unrecognised OFX date {_show(value)}.")
    try:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    except ValueError:
        raise MalformedImportError(f"{where}: invalid OFX date {_show(value)}.") from None


def parse_ofx(text: str, account_id: str, options: ImportOptions, name: str = "statement.ofx") -> ParsedStatement:
    text = text.lstrip("﻿")
    if "<OFX>" not in text.upper():
        raise MalformedImportError(f"{name}: not an OFX/QFX file (no <OFX> element).")
    currency = clean_text(_ofx_tag(text, "CURDEF") or options.currency).upper()[:8]
    blocks = _ofx_blocks(text)
    if not blocks and not re.search(r"<BANKTRANLIST>", text, re.I):
        raise MalformedImportError(f"{name}: OFX file contains no transaction list (<BANKTRANLIST>).")

    seen: dict[tuple, int] = {}
    txns: list[Transaction] = []
    for i, block in enumerate(blocks, start=1):
        where = f"{name} transaction {i}"
        posted, amount = _ofx_tag(block, "DTPOSTED"), _ofx_tag(block, "TRNAMT")
        if not posted or not amount:
            raise MalformedImportError(f"{where}: missing <DTPOSTED> or <TRNAMT>.")
        day = _ofx_date(posted, where)
        raw_amount = _parse_amount(amount, currency, where)
        amount_minor = raw_amount if options.ofx_charges_negative else -raw_amount
        payee, memo = clean_text(_ofx_tag(block, "NAME")), clean_text(_ofx_tag(block, "MEMO"))
        description = payee or memo
        fitid = clean_text(_ofx_tag(block, "FITID"))
        if fitid:
            txn_id = stable_id("fitid", fitid)
        else:
            key = ("row", account_id, day, amount_minor, currency, description)
            n = seen.get(key, 0)
            seen[key] = n + 1
            txn_id = stable_id(*key, n)
        notes: list[str] = []
        if memo and memo != description:
            notes.append(memo)
        orig_cur, rate = _ofx_tag(block, "CURSYM"), _ofx_tag(block, "CURRATE")
        if orig_cur and orig_cur.upper() != currency:
            notes.append(
                f"foreign_currency={clean_text(orig_cur).upper()[:8]}"
                + (f"; fx_rate={clean_text(rate)[:20]}" if rate else "")
            )
        txns.append(
            Transaction(
                id=txn_id,
                account_id=account_id,
                date=day,
                amount_minor=amount_minor,
                currency=currency,
                description=description,
                merchant=None,
                category=None,
                pending=False,
                notes="; ".join(notes) or None,
            )
        )

    balance_minor = balance_as_of = None
    ledger = _ofx_ledger(text)
    if ledger and _ofx_tag(ledger, "BALAMT"):
        raw_balance = _parse_amount(_ofx_tag(ledger, "BALAMT") or "0", currency, f"{name} <LEDGERBAL>")
        balance_minor = raw_balance if options.ofx_charges_negative else -raw_balance
        as_of = _ofx_tag(ledger, "DTASOF")
        balance_as_of = _ofx_date(as_of, f"{name} <LEDGERBAL>") if as_of else None
    return ParsedStatement(
        transactions=txns, balance_minor=balance_minor, balance_as_of=balance_as_of, currency=currency, fmt="ofx"
    )


def parse_statement_text(
    text: str, filename: str, account_id: str = DEFAULT_ACCOUNT_ID, options: ImportOptions | None = None
) -> ParsedStatement:
    """Parse statement content by file extension, falling back to sniffing the content."""
    options = options or ImportOptions()
    filename = clean_text(Path(str(filename or "")).name)[:100]
    if not isinstance(text, str) or not text.strip():
        raise MalformedImportError(f"{filename or 'statement'}: file is empty.")
    if len(text) > MAX_CONTENT_CHARS:
        raise MalformedImportError(
            f"{filename or 'statement'}: larger than {MAX_CONTENT_CHARS // (1024 * 1024)} MiB; split the export."
        )
    fmt = EXTENSIONS.get(Path(filename or "").suffix.lower())
    if fmt is None:
        head = text[:2000].upper()
        fmt = "ofx" if ("<OFX>" in head or "OFXHEADER" in head) else "csv"
    if fmt == "ofx":
        return parse_ofx(text, account_id, options, filename or "statement.ofx")
    return parse_csv(text, account_id, options, filename or "statement.csv")


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise MalformedImportError(f"{path.name}: file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB.")
        data = path.read_bytes()
    except OSError as exc:
        raise MalformedImportError(f"{path.name}: could not be read ({type(exc).__name__}).") from None
    return decode_statement_bytes(data, path.name)


def decode_statement_bytes(data: bytes, name: str = "statement") -> str:
    """Decode an export: UTF-8 (with or without BOM), UTF-16 with BOM, then Windows-1252,
    then Latin-1 (which accepts any byte). Binary content is rejected."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise MalformedImportError(f"{name}: not a text file (broken UTF-16).") from None
    if b"\x00" in data:
        raise MalformedImportError(f"{name}: not a text file (contains NUL bytes).")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MalformedImportError(f"{name}: not a text file (unknown encoding).")  # pragma: no cover


# ---------------------------------------------------------------------- provider


class StatementImportProvider(Provider):
    name = "import"

    def __init__(
        self,
        directory: str | os.PathLike[str],
        options: ImportOptions | None = None,
        default_account_id: str = DEFAULT_ACCOUNT_ID,
        stale_after_days: int = 10,
    ):
        if not directory:
            raise ConfigError("AMEX_IMPORT_DIR must point at the folder holding your Amex statement exports.")
        self.directory = Path(directory)
        self.options = options or ImportOptions()
        self.default_account_id = default_account_id
        self.stale_after_days = stale_after_days
        # path -> ((mtime_ns, size), account_id, parsed). A file is re-parsed only when it changes.
        self._cache: dict[Path, tuple[tuple[int, int], str, ParsedStatement]] = {}
        self._lock = threading.Lock()
        self.parse_count = 0  # number of files actually parsed (cache misses)

    def _files(self) -> dict[str, list[Path]]:
        try:
            if not self.directory.is_dir():
                raise ConfigError(
                    f"Statement folder {self.directory} does not exist. Create it and drop Amex "
                    "CSV/OFX/QFX exports in it."
                )
            accounts: dict[str, list[Path]] = {}
            for path in sorted(self.directory.iterdir()):
                if path.is_file() and path.suffix.lower() in EXTENSIONS:
                    accounts.setdefault(self.default_account_id, []).append(path)
                elif path.is_dir() and not path.name.startswith("."):
                    files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS)
                    if files:
                        accounts.setdefault(path.name, []).extend(files)
            return accounts
        except OSError as exc:
            raise ConfigError(f"Statement folder {self.directory} could not be read ({type(exc).__name__}).") from None

    def _parse_file(self, path: Path, account_id: str) -> ParsedStatement:
        try:
            st = path.stat()
        except OSError as exc:
            raise MalformedImportError(f"{path.name}: could not be read ({type(exc).__name__}).") from None
        key = (st.st_mtime_ns, st.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == key and cached[1] == account_id:
            return cached[2]
        parsed = parse_statement_text(_read_text(path), path.name, account_id, self.options)
        self.parse_count += 1
        self._cache[path] = (key, account_id, parsed)
        return parsed

    def _load(self, account_id: str) -> tuple[list[Transaction], ParsedStatement | None, list[Path]]:
        """Blocking (file I/O and parsing): call it through anyio.to_thread."""
        with self._lock:
            accounts = self._files()
            present = {p for paths in accounts.values() for p in paths}
            for stale in [p for p in self._cache if p not in present]:
                del self._cache[stale]
            files = accounts.get(account_id)
            if not files:
                raise AccountNotFoundError(
                    f"No statement files found for account {_show(account_id)}. "
                    "Call list_accounts to see what is available."
                )
            merged: dict[str, Transaction] = {}
            latest_balance: ParsedStatement | None = None
            for path in files:
                parsed = self._parse_file(path, account_id)
                merge_transactions(parsed.transactions, log, merged)
                if parsed.balance_minor is not None and (
                    latest_balance is None or (parsed.balance_as_of or "") >= (latest_balance.balance_as_of or "")
                ):
                    latest_balance = parsed
            return sorted(merged.values(), key=lambda t: (t.date, t.id)), latest_balance, files

    async def list_accounts(self) -> list[Account]:
        accounts = await anyio.to_thread.run_sync(self._files)
        return [
            Account(
                account_id=aid,
                name=f"American Express ({clean_text(aid)})",
                currency=self.options.currency,
                provider=self.name,
            )
            for aid in sorted(accounts)
        ]

    async def get_balance(self, account_id: str) -> Balance:
        _check_account_id(account_id)
        _, bal, _ = await anyio.to_thread.run_sync(self._load, account_id)
        if bal is None:
            return Balance(
                account_id=account_id,
                currency=self.options.currency,
                balance_minor=None,
                notes="Amex CSV exports carry no balance. Import an OFX/QFX export, or use the TrueLayer "
                "provider, to get a balance.",
            )
        return Balance(
            account_id=account_id,
            currency=bal.currency,
            balance_minor=bal.balance_minor,
            as_of=bal.balance_as_of,
            notes="ledger balance from the most recent OFX/QFX export",
        )

    async def list_transactions(
        self, account_id: str, since: date, before: date, include_pending: bool = False
    ) -> TransactionPage:
        _check_account_id(account_id)
        txns, _, _ = await anyio.to_thread.run_sync(self._load, account_id)
        # Statement exports contain booked transactions only: pending items cannot be reported.
        return TransactionPage(
            transactions=[t for t in txns if date_in_window(t.date, since, before)],
            pending_supported=False,
        )

    async def connection_status(self) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(self._status)

    def _status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "provider": self.name,
            "connected": False,
            "consent_expires_at": None,
            "days_remaining": None,
            "needs_reauthorisation": False,
            "warning": None,
            "message": "",
        }
        try:
            accounts = self._files()
            if not accounts:
                status["message"] = status["warning"] = (
                    f"No CSV/OFX/QFX files in {self.directory}. Export a statement from Amex and add it."
                )
                return status
            newest_txn, newest_file, count = None, 0.0, 0
            for aid in accounts:
                txns, _, files = self._load(aid)
                count += len(files)
                newest_file = max([newest_file] + [f.stat().st_mtime for f in files])
                if txns:
                    newest_txn = max(newest_txn or txns[-1].date, txns[-1].date)
        except AmexMcpError as exc:
            status["message"] = status["warning"] = str(exc)
            status["error"] = exc.code
            return status
        except OSError as exc:
            status["message"] = status["warning"] = (
                f"[config_error] Statement folder unreadable ({type(exc).__name__})."
            )
            status["error"] = "config_error"
            return status
        status.update(
            {
                "connected": True,
                "accounts": sorted(accounts),
                "file_count": count,
                "data_through": newest_txn,
                "newest_file_modified_at": datetime.fromtimestamp(newest_file, tz=UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
        )
        age = (london_today() - date.fromisoformat(newest_txn)).days if newest_txn else None
        status["data_age_days"] = age
        if age is not None and age > self.stale_after_days:
            status["message"] = status["warning"] = (
                f"File import has no consent to expire, but the data is stale: newest transaction is "
                f"{newest_txn} ({age} days ago). Export a fresh statement from Amex."
            )
        else:
            status["message"] = f"File import: {count} file(s), data through {newest_txn}. No consent to expire."
        return status


def _check_account_id(account_id: str) -> None:
    # The id is only ever compared with folder names found on disk; it is never joined into a path.
    if (
        not isinstance(account_id, str)
        or not account_id
        or len(account_id) > 128
        or any(c in account_id for c in "/\\:\x00")
        or ".." in account_id
    ):
        raise InvalidRequestError("account_id is missing or malformed. Use an id returned by list_accounts.")
