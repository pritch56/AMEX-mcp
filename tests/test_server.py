"""MCP-level tests: tool listing, tool calls, error surfacing, HTTP auth gate, and
end-to-end runs of the real server process over BOTH transports against a stub backend."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from importlib.metadata import version
from typing import Any

import httpx
import pytest
from mcp import Client, StdioServerParameters

from amex_mcp.errors import (
    AccountNotFoundError,
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    NotConnectedError,
    ProviderError,
    RateLimitError,
    ScaRequiredError,
    TokenExpiredError,
    TokenStoreError,
)
from amex_mcp.models import Account, Balance, Transaction, TransactionPage, london_today
from amex_mcp.providers.base import Provider
from amex_mcp.server import build_http_app, build_provider, create_server

from .conftest import CARD, FIXTURES, run

EXPECTED_TOOLS = {"list_accounts", "get_balance", "list_transactions", "get_connection_status", "parse_statement"}
TX_KEYS = [
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


class StubProvider(Provider):
    name = "stub"

    def __init__(self, fail: Exception | None = None, status_fail: Exception | None = None, warning: str | None = None):
        self.fail = fail
        self.status_fail = status_fail
        self.warning = warning
        self.calls: list[tuple] = []

    async def list_accounts(self) -> list[Account]:
        if self.fail:
            raise self.fail
        return [Account(account_id="card-1", name="Stub Amex", currency="GBP", last4="0000", provider="stub")]

    async def get_balance(self, account_id: str) -> Balance:
        return Balance(account_id=account_id, currency="GBP", balance_minor=-12345)

    async def list_transactions(self, account_id, since, before, include_pending=False):
        if self.fail:
            raise self.fail
        self.calls.append((account_id, since, before, include_pending))
        return TransactionPage(
            transactions=[
                Transaction(
                    id="amex_abc",
                    account_id=account_id,
                    date="2026-10-02",
                    amount_minor=-4567,
                    currency="GBP",
                    description="TESCO",
                    notes="",
                )
            ],
            pending_excluded=2,
            earliest_pending_date="2026-10-05",
        )

    async def connection_status(self) -> dict[str, Any]:
        if self.status_fail:
            raise self.status_fail
        return {
            "provider": "stub",
            "connected": True,
            "consent_expires_at": "2026-12-19T10:00:00Z",
            "days_remaining": 70,
            "needs_reauthorisation": False,
            "message": "ok",
        }

    def pending_warning(self) -> str | None:
        return self.warning


def call(provider: Provider | None, tool: str, args: dict | None = None, **server_kw):
    async def go():
        async with Client(create_server(provider, **server_kw)) as client:
            return await client.call_tool(tool, args or {})

    return run(go())


def payload(result) -> dict:
    assert not result.is_error, result.content
    return result.structured_content if result.structured_content else json.loads(result.content[0].text)


def error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


# ------------------------------------------------------------------- in-process


def test_tools_are_listed_and_all_read_only():
    async def go():
        async with Client(create_server(StubProvider())) as client:
            return (await client.list_tools()).tools

    tools = run(go())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert t.description
    names = " ".join(t.name for t in tools)
    assert not any(word in names for word in ("pay", "transfer", "create", "update", "delete", "freeze"))
    described = next(t for t in tools if t.name == "list_transactions").description
    # the overlapping-window requirement is part of the tool contract
    assert "overlapping window" in described and "earliest_pending_date" in described and "14 days" in described


def test_server_info_version_is_the_package_version():
    async def go():
        async with Client(create_server(StubProvider())) as client:
            return client.server_info

    info = run(go())
    assert info.name == "amex" and info.version == version("amex-mcp")


def test_list_transactions_tool_shape_and_defaults():
    stub = StubProvider()
    out = payload(
        call(stub, "list_transactions", {"account_id": "card-1", "since": "2026-10-01", "before": "2026-10-08"})
    )
    assert out["count"] == 1 and out["source"] == "amex"
    assert list(out["transactions"][0]) == TX_KEYS
    assert out["transactions"][0]["notes"] is None  # never ""
    assert out["since"] == "2026-10-01" and out["before"] == "2026-10-08"  # echoed as plain dates
    assert out["pending_excluded"] == 2 and out["earliest_pending_date"] == "2026-10-05"
    assert out["pending_supported"] is True and "token_store_warning" not in out
    assert stub.calls[0] == ("card-1", date(2026, 10, 1), date(2026, 10, 8), False)

    # defaults: before = tomorrow (London), since = before - 30 days
    out = payload(call(stub, "list_transactions", {"account_id": "card-1"}))
    _, since, before, _ = stub.calls[1]
    assert before in (london_today() + timedelta(days=1), london_today() + timedelta(days=2))  # midnight-safe
    assert (before - since).days == 30
    assert out["since"] == since.isoformat() and out["before"] == before.isoformat()

    # a datetime bound is converted to its London date (23:30 UTC in July is the next day)
    payload(
        call(stub, "list_transactions", {"account_id": "c", "since": "2026-07-01T23:30:00Z", "before": "2026-07-09"})
    )
    assert stub.calls[2][1] == date(2026, 7, 2)


def test_range_cap_rejects_before_the_provider_is_touched(monkeypatch):
    stub = StubProvider()
    text = error_text(call(stub, "list_transactions", {"account_id": "c", "since": "1970-01-01"}))
    assert "[invalid_request]" in text and "maximum is 400" in text and stub.calls == []
    ok_args = {"account_id": "c", "since": "2025-09-01", "before": "2026-10-06"}  # exactly 400 days
    assert payload(call(stub, "list_transactions", ok_args))["count"] == 1

    monkeypatch.setenv("AMEX_MAX_RANGE_DAYS", "7")
    text = error_text(
        call(stub, "list_transactions", {"account_id": "c", "since": "2026-10-01", "before": "2026-10-09"})
    )
    assert "[invalid_request]" in text and "maximum is 7" in text
    monkeypatch.setenv("AMEX_MAX_RANGE_DAYS", "lots")
    assert "[config_error] AMEX_MAX_RANGE_DAYS" in error_text(call(stub, "list_transactions", {"account_id": "c"}))


def test_other_tools():
    stub = StubProvider()
    account = payload(call(stub, "list_accounts"))["accounts"][0]
    assert account["id"] == account["account_id"] == "card-1" and account["name"] == "Stub Amex"
    balance = payload(call(stub, "get_balance", {"account_id": "card-1"}))
    assert balance["balance_minor"] == -12345 and balance["currency"] == "GBP"
    status = payload(call(stub, "get_connection_status"))
    assert status["consent_expires_at"] == "2026-12-19T10:00:00Z" and status["source"] == "amex"
    assert status["needs_reauthorisation"] is False and status["checked_at"].endswith("Z")
    # contract keys are always present, even when a provider omits one (the stub omits warning)
    assert status["connected"] is True and status["days_remaining"] == 70 and status["warning"] is None


def test_unsaved_token_warning_rides_along_with_served_data():
    stub = StubProvider(warning="[token_store_error] The refreshed tokens could not be saved: disk.")
    for tool, args in (
        ("list_accounts", {}),
        ("get_balance", {"account_id": "card-1"}),
        ("list_transactions", {"account_id": "card-1", "since": "2026-10-01", "before": "2026-10-08"}),
    ):
        out = payload(call(stub, tool, args))  # the data is still served
        assert out["token_store_warning"].startswith("[token_store_error]")


def test_parse_statement_tool_needs_no_provider():
    csv_text = (FIXTURES / "amex_uk_activity.csv").read_text(encoding="utf-8")
    out = payload(
        call(
            None,
            "parse_statement",
            {"content": csv_text, "filename": "activity.csv"},
            provider_factory=lambda: (_ for _ in ()).throw(AssertionError("must not be built")),
        )
    )
    assert out["count"] == 7 and out["format"] == "csv"
    assert list(out["transactions"][0]) == TX_KEYS
    assert sum(t["amount_minor"] for t in out["transactions"]) == -4567 - 425 - 425 - 4321 + 1999 + 25000 - 123450


def test_parse_statement_runs_off_the_event_loop_and_caps_content(monkeypatch):
    import amex_mcp.server as server

    seen: list[int] = []
    real = server.parse_statement_text

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "parse_statement_text", spy)

    async def go():
        async with Client(create_server(None)) as client:
            await client.call_tool("parse_statement", {"content": "Date,Description,Amount\n05/10/2026,X,1.00\n"})
            return threading.get_ident()

    loop_thread = run(go())
    assert len(seen) == 1 and seen[0] != loop_thread

    monkeypatch.setattr("amex_mcp.providers.statement_import.MAX_CONTENT_CHARS", 100)
    text = error_text(call(None, "parse_statement", {"content": "x" * 101}))
    assert "[malformed_import_file]" in text and "larger than" in text


@pytest.mark.parametrize(
    "content, filename",
    [
        ("Date,Description,Amount\n05/10/2026,SHOP,1e999999999\n", "a.csv"),
        ("<OFX><BANKTRANLIST><STMTTRN><DTPOSTED>20261002<TRNAMT>NaN</STMTTRN></BANKTRANLIST></OFX>", "a.ofx"),
        ("nonsense", "statement.csv"),
    ],
)
def test_malformed_statement_content_is_never_an_internal_error(content, filename):
    text = error_text(call(None, "parse_statement", {"content": content, "filename": filename}))
    assert "[malformed_import_file]" in text and "internal_error" not in text


@pytest.mark.parametrize(
    "exc, expected",
    [
        (ConsentExpiredError("Consent has expired. Re-run amex-mcp-link."), "[consent_expired]"),
        (TokenExpiredError("x"), "[token_expired]"),
        (NotConnectedError("x"), "[not_connected]"),
        (RateLimitError("Slow down.", 60), "[rate_limited]"),
        (ScaRequiredError("x"), "[sca_required]"),
        (InvalidRequestError("x"), "[invalid_request]"),
        (AccountNotFoundError("x"), "[account_not_found]"),
        (ProviderError("x"), "[provider_error]"),
        (ConfigError("x"), "[config_error]"),
        (TokenStoreError("x"), "[token_store_error]"),
        (RuntimeError("boom with secret-looking detail"), "[internal_error] Unexpected RuntimeError"),
        (KeyError("secret-looking"), "[internal_error] Unexpected KeyError"),
    ],
)
def test_errors_surface_as_clean_prefixed_tool_errors(exc, expected):
    for tool, args in (("list_accounts", {}), ("list_transactions", {"account_id": "c"})):
        text = error_text(call(StubProvider(fail=exc), tool, args))
        assert expected in text and "Traceback" not in text and "secret-looking" not in text


def test_bad_arguments_and_malformed_statement_are_tool_errors():
    assert "[invalid_request]" in error_text(
        call(StubProvider(), "list_transactions", {"account_id": "c", "since": "last tuesday"})
    )
    for since, before in (("2026-10-08", "2026-10-01"), ("2026-10-01", "2026-10-01")):
        text = error_text(
            call(StubProvider(), "list_transactions", {"account_id": "c", "since": since, "before": before})
        )
        assert "[invalid_request]" in text and "must be earlier" in text
    assert "[malformed_import_file]" in error_text(call(None, "parse_statement", {"content": "nonsense"}))


def test_unconfigured_server_still_starts_and_explains(monkeypatch):
    for var in ("AMEX_PROVIDER", "TRUELAYER_CLIENT_ID", "AMEX_IMPORT_DIR"):
        monkeypatch.delenv(var, raising=False)
    assert "[config_error] No data source is configured" in error_text(call(None, "list_accounts"))
    status = payload(call(None, "get_connection_status"))
    assert status["connected"] is False and status["error"] == "config_error"
    assert status["consent_expires_at"] is None and status["days_remaining"] is None
    assert status["warning"].startswith("[config_error]")


@pytest.mark.parametrize(
    "exc, code",
    [
        (RateLimitError("Slow down.", 60), "rate_limited"),
        (ProviderError("upstream is down"), "provider_error"),
        (ConsentExpiredError("gone"), "consent_expired"),
        (NotConnectedError("never linked"), "not_connected"),
        (ConfigError("bad"), "config_error"),
        (TokenStoreError("unreadable"), "token_store_error"),
        (RuntimeError("secret-looking"), "internal_error"),
    ],
)
def test_get_connection_status_never_raises(exc, code):
    status = payload(call(StubProvider(status_fail=exc), "get_connection_status"))
    assert status["connected"] is False and status["error"] == code
    assert status["warning"].startswith(f"[{code}]") and "secret-looking" not in status["warning"]
    assert status["consent_expires_at"] is None and status["days_remaining"] is None


def test_build_provider_selection(tmp_path):
    assert build_provider({"AMEX_IMPORT_DIR": str(tmp_path)}).name == "import"
    tl = build_provider({"TRUELAYER_CLIENT_ID": "x", "TRUELAYER_CLIENT_SECRET": "y", "AMEX_TOKEN_STORE": "memory"})
    assert tl.name == "truelayer"
    explicit = build_provider({"AMEX_PROVIDER": "import", "AMEX_IMPORT_DIR": str(tmp_path), "TRUELAYER_CLIENT_ID": "x"})
    assert explicit.name == "import"
    with pytest.raises(ConfigError, match="not supported"):
        build_provider({"AMEX_PROVIDER": "plaid"})
    with pytest.raises(ConfigError, match="TRUELAYER_API_BASE must be an https"):
        build_provider(
            {"TRUELAYER_CLIENT_ID": "x", "TRUELAYER_CLIENT_SECRET": "y", "TRUELAYER_API_BASE": "http://evil.example"}
        )


# ---------------------------------------------------------------- HTTP auth gate


def test_http_refuses_public_bind_without_auth():
    for host in ("0.0.0.0", "192.168.1.10", "::", "amex.example"):
        with pytest.raises(ConfigError, match="AMEX_MCP_AUTH_TOKEN"):
            build_http_app(create_server(StubProvider()), host, env={})
        # a whitespace-only token is no token
        with pytest.raises(ConfigError):
            build_http_app(create_server(StubProvider()), host, env={"AMEX_MCP_AUTH_TOKEN": "   "})
        # the old override name is not honoured
        with pytest.raises(ConfigError, match="AMEX_MCP_ALLOW_NO_AUTH"):
            build_http_app(create_server(StubProvider()), host, env={"AMEX_MCP_ALLOW_UNAUTHENTICATED": "1"})
    assert build_http_app(create_server(StubProvider()), "0.0.0.0", env={"AMEX_MCP_AUTH_TOKEN": "t" * 32})
    assert build_http_app(create_server(StubProvider()), "0.0.0.0", env={"AMEX_MCP_ALLOW_NO_AUTH": "1"})
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
        assert build_http_app(create_server(StubProvider()), host, env={})


def test_http_bearer_token_is_enforced():
    app = build_http_app(create_server(StubProvider()), "0.0.0.0", env={"AMEX_MCP_AUTH_TOKEN": "correct-token"})

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://amex.example") as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            none = await client.post("/mcp", json=body)
            wrong = await client.post("/mcp", json=body, headers={"Authorization": "Bearer nope"})
            return none, wrong

    none, wrong = run(go())
    assert none.status_code == 401 and wrong.status_code == 401
    assert none.headers["www-authenticate"] == "Bearer" and "correct-token" not in none.text


def test_bearer_scheme_is_case_insensitive_and_token_is_stripped():
    from amex_mcp.server import BearerAuthMiddleware

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerAuthMiddleware(inner, "  correct-token \n")  # surrounding whitespace in the env value is stripped
    cases = {
        "none": None,
        "wrong": "Bearer nope",
        "prefix": "Bearer correct-toke",
        "longer": "Bearer correct-token-and-more",
        "basic": "Basic correct-token",
        "bare": "correct-token",
        "empty": "Bearer ",
        "good": "Bearer correct-token",
        "lower": "bearer correct-token",
        "upper": "BEARER correct-token",
        "padded": "Bearer   correct-token  ",
    }

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://amex.example") as client:
            return {
                name: (
                    await client.get("/mcp", headers={} if header is None else {"Authorization": header})
                ).status_code
                for name, header in cases.items()
            }

    out = run(go())
    assert {k: v for k, v in out.items() if v == 204}.keys() == {"good", "lower", "upper", "padded"}
    assert all(v == 401 for k, v in out.items() if k not in ("good", "lower", "upper", "padded"))


# ------------------------------------------------ end-to-end: real server process


def _env(tmp_path, fake_url: str | None = None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AMEX_", "TRUELAYER_"))}
    env["AMEX_TOKEN_FILE"] = str(tmp_path / "tokens.json")
    if fake_url:
        env.update(
            {
                "TRUELAYER_CLIENT_ID": "e2e-client",
                "TRUELAYER_CLIENT_SECRET": "e2e-secret",
                "TRUELAYER_AUTH_BASE": fake_url,
                "TRUELAYER_API_BASE": fake_url,
                "TRUELAYER_REFRESH_TOKEN": "generic-must-be-ignored",
                "AMEX_TRUELAYER_REFRESH_TOKEN": "seed-refresh",
            }
        )
    env.update(extra)
    return env


async def _exercise(client: Client, account_id: str) -> dict[str, Any]:
    tools = {t.name for t in (await client.list_tools()).tools}
    accounts = payload(await client.call_tool("list_accounts", {}))
    txns = payload(
        await client.call_tool(
            "list_transactions", {"account_id": account_id, "since": "2026-10-01", "before": "2026-10-08"}
        )
    )
    balance = payload(await client.call_tool("get_balance", {"account_id": account_id}))
    status = payload(await client.call_tool("get_connection_status", {}))
    return {"tools": tools, "accounts": accounts, "txns": txns, "balance": balance, "status": status}


def test_e2e_stdio_against_stub_truelayer(tmp_path, fake_truelayer):
    # Launched as `python -m amex_mcp` from an unrelated directory: no dependency on the
    # checkout location or on `uv` being on PATH.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "amex_mcp", "--transport", "stdio"],
        env=_env(tmp_path, fake_truelayer.url),
        cwd=str(tmp_path),
    )

    async def go():
        async with Client(params) as client:
            return await _exercise(client, CARD["account_id"])

    out = run(go())

    assert out["tools"] == EXPECTED_TOOLS
    account = out["accounts"]["accounts"][0]
    assert account["last4"] == "0000" and account["id"] == account["account_id"] == CARD["account_id"]
    assert [t["amount_minor"] for t in out["txns"]["transactions"]] == [-4567, -4321, 1999, 25000]
    assert list(out["txns"]["transactions"][0]) == TX_KEYS
    # the one pending item was left out, and reported
    assert out["txns"]["pending_excluded"] == 1 and out["txns"]["earliest_pending_date"] == "2026-10-07"
    assert out["txns"]["pending_supported"] is True
    assert out["balance"]["balance_minor"] == -131245
    assert out["status"]["connected"] is True and out["status"]["consent_expires_at"] == "2099-12-19T10:00:00Z"
    # the seed refresh token was exchanged once and the ROTATED token was persisted to disk
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved["refresh_token"] == "rotated-refresh"
    assert [r for r in fake_truelayer.requests if r[0] == "POST"] == [("POST", "/connect/token")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early: {proc.stderr.read().decode(errors='replace')}")
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError("server did not start listening")


def _stop(proc: subprocess.Popen) -> None:
    """Stop the one process this test started (by handle, never by name)."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_e2e_streamable_http_against_import_backend(tmp_path):
    statements = tmp_path / "statements"
    statements.mkdir()
    shutil.copy(FIXTURES / "amex_uk_activity.csv", statements / "activity.csv")
    shutil.copy(FIXTURES / "amex_statement.qfx", statements / "statement.qfx")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "amex_mcp", "--transport", "http", "--port", str(port)],
        env=_env(tmp_path, AMEX_IMPORT_DIR=str(statements)),
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port, proc)

        async def go():
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                return await _exercise(client, "amex-import")

        out = run(go())
    finally:
        _stop(proc)

    assert out["tools"] == EXPECTED_TOOLS
    assert out["accounts"]["accounts"][0]["provider"] == "import"
    # CSV + QFX overlap on 3 transactions but carry different ids (Reference vs FITID): 4 CSV rows + 3 QFX rows
    week = out["txns"]["transactions"]
    assert all("2026-10-01" <= t["date"] < "2026-10-08" for t in week)
    assert {t["amount_minor"] for t in week} == {-4567, -425, -4321, 1999, 25000}
    assert out["txns"]["pending_supported"] is False
    assert out["balance"]["balance_minor"] == -131245
    assert out["status"]["needs_reauthorisation"] is False and out["status"]["data_through"] == "2026-10-06"


def test_e2e_streamable_http_against_stub_truelayer_with_bearer_auth(tmp_path, fake_truelayer):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "amex_mcp"],
        env=_env(
            tmp_path,
            fake_truelayer.url,
            AMEX_MCP_TRANSPORT="http",
            AMEX_MCP_PORT=str(port),
            AMEX_MCP_AUTH_TOKEN="e2e-bearer-token",
            PORT="1",  # the generic PORT variable is not read
        ),
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port, proc)
        url = f"http://127.0.0.1:{port}/mcp"
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw-test", "version": "0"},
            },
        }
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert httpx.post(url, json=init, headers=headers).status_code == 401

        auth = {**headers, "Authorization": "bearer e2e-bearer-token"}
        r = httpx.post(url, json=init, headers=auth)
        info = r.json()["result"]["serverInfo"]
        assert r.status_code == 200 and info["name"] == "amex" and info["version"] == version("amex-mcp")
        proto = {"MCP-Protocol-Version": r.json()["result"]["protocolVersion"]}
        r = httpx.post(url, headers={**auth, **proto}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {t["name"] for t in r.json()["result"]["tools"]} == EXPECTED_TOOLS
        r = httpx.post(
            url,
            headers={**auth, **proto},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_transactions",
                    "arguments": {
                        "account_id": CARD["account_id"],
                        "since": "2026-10-01",
                        "before": "2026-10-08",
                        "include_pending": True,
                    },
                },
            },
        )
        result = r.json()["result"]
        assert result.get("isError") in (False, None)
        data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        assert data["count"] == 5 and sum(1 for t in data["transactions"] if t["pending"]) == 1
        assert data["pending_excluded"] == 0 and data["earliest_pending_date"] == "2026-10-07"
    finally:
        _stop(proc)
    stderr = proc.stderr.read().decode(errors="replace")
    for secret in ("e2e-secret", "seed-refresh", "rotated-refresh", "access-e2e", "e2e-bearer-token"):
        assert secret not in stderr


def test_non_loopback_http_without_token_exits_before_listening(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "amex_mcp", "--transport", "http", "--host", "0.0.0.0", "--port", str(_free_port())],
        env=_env(tmp_path),
        cwd=str(tmp_path),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 2 and b"[config_error] Refusing to serve" in proc.stderr
