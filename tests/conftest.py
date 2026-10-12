from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = Path(__file__).parent.parent


def run(coro):
    """Run a coroutine in a fresh event loop (keeps the suite free of async plugins)."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """The fixtures are dated early October 2026: pin the provider's idea of "today" so the
    suite does not depend on the day it is run."""
    from datetime import date

    monkeypatch.setattr("amex_mcp.providers.truelayer._today", lambda: date(2026, 10, 11))


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


# Synthetic TrueLayer payloads (shapes follow docs.truelayer.com card-data-requests).
CARD = {
    "account_id": "f7b093437032c216d4350b7d442b9c5c",
    "card_network": "AMEX",
    "card_type": "CREDIT",
    "currency": "GBP",
    "display_name": "Example American Express Credit Card",
    "partial_card_number": "0000",
    "name_on_card": "A N OTHER",
    "update_timestamp": "2026-10-08T07:00:00Z",
    "provider": {"provider_id": "ob-amex"},
}
BALANCE = {
    "available": 3687.55,
    "currency": "GBP",
    "current": 1312.45,
    "credit_limit": 5000,
    "last_statement_balance": 842.1,
    "last_statement_date": "2026-09-20",
    "payment_due": 25.0,
    "payment_due_date": "2026-10-15",
    "update_timestamp": "2026-10-08T07:00:00Z",
}
TRANSACTIONS = [
    {
        "transaction_id": "volatile-1",
        "normalised_provider_transaction_id": "txn-aaa111",
        "provider_transaction_id": "AT262750012000010000001",
        "timestamp": "2026-10-02T00:00:00+00:00",
        "description": "TESCO STORES 6021          LONDON",
        "amount": 45.67,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "transaction_category": "PURCHASE",
        "transaction_classification": ["Shopping", "Groceries"],
        "merchant_name": "Tesco",
        "meta": {"provider_transaction_category": "General Purchases-Groceries"},
    },
    {
        "transaction_id": "volatile-2",
        "normalised_provider_transaction_id": "txn-bbb222",
        "timestamp": "2026-10-04T00:00:00+00:00",
        "description": "EXAMPLE DINER             NEW YORK",
        "amount": 43.21,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "transaction_category": "PURCHASE",
        "transaction_classification": [],
        "meta": {"foreign_amount": "54.00 USD", "exchange_rate": "1.2871", "supplementary_card_id": "x1"},
    },
    {
        "transaction_id": "volatile-3",
        "normalised_provider_transaction_id": "txn-ccc333",
        "timestamp": "2026-10-05T00:00:00+00:00",
        "description": "EXAMPLE OUTDOORS LTD      LEEDS",
        "amount": -19.99,
        "currency": "GBP",
        "transaction_type": "CREDIT",
        "transaction_category": "PURCHASE",
        "transaction_classification": [],
    },
    {
        "transaction_id": "volatile-4",
        "normalised_provider_transaction_id": "txn-ddd444",
        "timestamp": "2026-10-06T00:00:00+00:00",
        "description": "PAYMENT RECEIVED - THANK YOU",
        "amount": -250.0,
        "currency": "GBP",
        "transaction_type": "CREDIT",
        "transaction_category": "CREDIT",
        "transaction_classification": [],
    },
]
PENDING = [
    {
        "transaction_id": "pending-volatile-1",
        "timestamp": "2026-10-07T00:00:00+00:00",
        "description": "EXAMPLE COFFEE CO         LONDON",
        "amount": 3.4,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "transaction_category": "PURCHASE",
        "transaction_classification": [],
    }
]
ME = {
    "client_id": "test-client",
    "credentials_id": "cred-1",
    "consent_status": "Authorised",
    "consent_created_at": "2026-09-20T10:00:00Z",
    "consent_expires_at": "2099-12-19T10:00:00Z",
    "provider": {"provider_id": "ob-amex", "display_name": "American Express"},
    "scopes": ["info", "cards", "balance", "transactions", "offline_access"],
}


class FakeTrueLayer:
    """A real HTTP server that imitates the handful of TrueLayer endpoints we call.
    Used for end-to-end runs of the MCP server process (no network, no credentials)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(("POST", self.path))
                if self.path == "/connect/token" and payload.get("refresh_token") == "seed-refresh":
                    self._send(
                        200,
                        {
                            "access_token": "access-e2e",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                            "refresh_token": "rotated-refresh",
                        },
                    )
                else:
                    self._send(400, {"error": "invalid_grant"})

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                fake.requests.append(("GET", self.path))
                if self.headers.get("Authorization") != "Bearer access-e2e":
                    self._send(401, {"error": "invalid_token"})
                    return
                acct = CARD["account_id"]
                routes = {
                    "/data/v1/cards": [CARD],
                    f"/data/v1/cards/{acct}/balance": [BALANCE],
                    f"/data/v1/cards/{acct}/transactions": TRANSACTIONS,
                    f"/data/v1/cards/{acct}/transactions/pending": PENDING,
                    "/data/v1/me": [ME],
                }
                if parsed.path in routes:
                    assert parse_qs(parsed.query) is not None
                    self._send(200, {"results": routes[parsed.path], "status": "Succeeded"})
                else:
                    self._send(404, {"error": "account_not_found"})

            def log_message(self, *args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeTrueLayer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_truelayer():
    with FakeTrueLayer() as fake:
        yield fake
