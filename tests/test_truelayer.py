"""TrueLayer provider against mocked HTTP (respx). No network, no real credentials."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from amex_mcp.errors import (
    AccountNotFoundError,
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    NotConnectedError,
    NotSupportedError,
    ProviderError,
    RateLimitError,
    ScaRequiredError,
    TokenExpiredError,
    TokenStoreError,
)
from amex_mcp.models import utc_z
from amex_mcp.providers.truelayer import MAX_REQUESTS_PER_CALL, TrueLayerConfig, TrueLayerProvider, _windows
from amex_mcp.token_store import FileTokenStore, MemoryTokenStore

from .conftest import BALANCE, CARD, ME, PENDING, TRANSACTIONS, run

AUTH = "https://auth.truelayer.com"
API = "https://api.truelayer.com"
ACCT = CARD["account_id"]
SINCE = date(2026, 10, 1)
BEFORE = date(2026, 10, 8)


def config(**kw) -> TrueLayerConfig:
    return TrueLayerConfig(client_id="test-client", client_secret="s3cret-value", **kw)


def fresh_store(**extra) -> MemoryTokenStore:
    return MemoryTokenStore(
        {
            "access_token": "access-1",
            "access_token_expires_at": time.time() + 3000,
            "refresh_token": "refresh-1",
            **extra,
        }
    )


def provider(store=None, **kw) -> TrueLayerProvider:
    return TrueLayerProvider(config(**kw), store if store is not None else fresh_store(), backoff_seconds=0)


def ok(results) -> httpx.Response:
    return httpx.Response(200, json={"results": results, "status": "Succeeded"})


# ---------------------------------------------------------------- auth / tokens


@respx.mock
def test_valid_access_token_is_reused_without_refresh():
    token_route = respx.post(f"{AUTH}/connect/token")
    cards = respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    accounts = run(provider().list_accounts())
    assert not token_route.called
    assert cards.calls[0].request.headers["Authorization"] == "Bearer access-1"
    assert accounts[0].to_dict() == {
        "id": ACCT,
        "account_id": ACCT,
        "name": "Example American Express Credit Card",
        "currency": "GBP",
        "type": "credit_card",
        "card_network": "AMEX",
        "last4": "0000",
        "provider": "truelayer",
        "source": "amex",
    }


@respx.mock
def test_expired_access_token_is_refreshed_and_rotated_refresh_token_persisted(tmp_path):
    store = FileTokenStore(tmp_path / "nested" / "tokens.json")
    store.save({"access_token": "old", "access_token_expires_at": time.time() - 10, "refresh_token": "refresh-1"})
    token_route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "access-2", "expires_in": 3600, "token_type": "Bearer", "refresh_token": "refresh-2"},
        )
    )
    cards = respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))

    run(provider(store).list_accounts())

    sent = json.loads(token_route.calls[0].request.content)
    assert sent == {
        "grant_type": "refresh_token",
        "client_id": "test-client",
        "client_secret": "s3cret-value",
        "refresh_token": "refresh-1",
    }
    assert cards.calls[0].request.headers["Authorization"] == "Bearer access-2"
    saved = json.loads((tmp_path / "nested" / "tokens.json").read_text())
    assert saved["refresh_token"] == "refresh-2" and saved["access_token"] == "access-2"
    assert saved["access_token_expires_at"] > time.time() + 3000


@respx.mock
def test_refresh_without_new_refresh_token_keeps_the_old_one():
    store = MemoryTokenStore({"refresh_token": "refresh-1"})
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})
    )
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    run(provider(store).list_accounts())
    assert store.load()["refresh_token"] == "refresh-1"


@respx.mock
def test_seed_refresh_token_from_env_bootstraps_empty_store():
    store = MemoryTokenStore()
    token_route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "access-2", "expires_in": 3600, "refresh_token": "refresh-2"}
        )
    )
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    run(provider(store, seed_refresh_token="seed-token").list_accounts())
    assert json.loads(token_route.calls[0].request.content)["refresh_token"] == "seed-token"
    assert store.load()["refresh_token"] == "refresh-2"


@respx.mock
def test_form_encoding_option():
    store = MemoryTokenStore({"refresh_token": "refresh-1"})
    token_route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    run(provider(store, token_encoding="form").list_accounts())
    req = token_route.calls[0].request
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert b"grant_type=refresh_token" in req.content


def test_no_tokens_at_all_is_not_connected():
    with pytest.raises(NotConnectedError, match="amex-mcp-link"):
        run(provider(MemoryTokenStore()).list_accounts())


@respx.mock
def test_401_triggers_one_refresh_then_succeeds():
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})
    )
    cards = respx.get(f"{API}/data/v1/cards").mock(
        side_effect=[httpx.Response(401, json={"error": "invalid_token"}), ok([CARD])]
    )
    assert len(run(provider().list_accounts())) == 1
    assert cards.calls[1].request.headers["Authorization"] == "Bearer access-2"


@respx.mock
def test_expired_token_that_cannot_be_recovered():
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})
    )
    respx.get(f"{API}/data/v1/cards").mock(return_value=httpx.Response(401, json={"error": "invalid_token"}))
    with pytest.raises(TokenExpiredError, match=r"\[token_expired\].*even after refreshing"):
        run(provider().list_accounts())


@respx.mock
def test_expired_consent_on_refresh_needs_reauthorisation():
    store = MemoryTokenStore({"refresh_token": "refresh-1"})
    respx.post(f"{AUTH}/connect/token").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(ConsentExpiredError, match=r"\[consent_expired\].*Re-authorise"):
        run(provider(store).list_accounts())
    assert store.load()["refresh_token"] == "refresh-1"  # nothing clobbered


@respx.mock
def test_expired_consent_on_data_call():
    respx.get(f"{API}/data/v1/cards").mock(return_value=httpx.Response(403, json={"error": "access_denied"}))
    with pytest.raises(ConsentExpiredError, match="expired or been revoked"):
        run(provider().list_accounts())


@respx.mock
def test_bad_client_credentials_is_a_config_error():
    respx.post(f"{AUTH}/connect/token").mock(return_value=httpx.Response(400, json={"error": "invalid_client"}))
    with pytest.raises(ConfigError, match="TRUELAYER_CLIENT_ID"):
        run(provider(MemoryTokenStore({"refresh_token": "r"})).list_accounts())


def test_amex_specific_seed_token_takes_precedence_over_shared_one():
    base = {"TRUELAYER_CLIENT_ID": "shared-app", "TRUELAYER_CLIENT_SECRET": "x"}
    both = TrueLayerConfig.from_env(
        {**base, "TRUELAYER_REFRESH_TOKEN": "generic", "AMEX_TRUELAYER_REFRESH_TOKEN": "amex-only"}
    )
    assert both.seed_refresh_token == "amex-only"
    assert TrueLayerConfig.from_env({**base, "TRUELAYER_REFRESH_TOKEN": "generic"}).seed_refresh_token is None
    assert TrueLayerConfig.from_env(base).seed_refresh_token is None


def test_missing_client_credentials():
    with pytest.raises(ConfigError, match="TRUELAYER_CLIENT_ID and TRUELAYER_CLIENT_SECRET"):
        TrueLayerConfig.from_env({})


def test_sandbox_environment_urls_and_secret_free_repr():
    cfg = TrueLayerConfig.from_env(
        {"TRUELAYER_CLIENT_ID": "sandbox-x", "TRUELAYER_CLIENT_SECRET": "topsecret", "TRUELAYER_ENV": "sandbox"}
    )
    assert cfg.auth_base == "https://auth.truelayer-sandbox.com"
    assert cfg.api_base == "https://api.truelayer-sandbox.com"
    assert "topsecret" not in repr(cfg)


@respx.mock
def test_secrets_never_reach_logs_or_error_messages(caplog):
    caplog.set_level(logging.DEBUG)
    store = MemoryTokenStore({"refresh_token": "refresh-SECRET-1"})
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "token refresh-SECRET-1 revoked"}
        )
    )
    with pytest.raises(ConsentExpiredError) as info:
        run(provider(store).list_accounts())
    blob = caplog.text + str(info.value)
    assert "refresh-SECRET-1" not in blob and "s3cret-value" not in blob


# ------------------------------------------------------------------- data calls


def mock_pending(results=None, response: httpx.Response | None = None):
    return respx.get(f"{API}/data/v1/cards/{ACCT}/transactions/pending").mock(
        return_value=response or ok(results or [])
    )


def listed(p: TrueLayerProvider, since=SINCE, before=BEFORE, **kw):
    return run(p.list_transactions(ACCT, since, before, **kw)).transactions


@respx.mock
def test_balance_sign_convention():
    respx.get(f"{API}/data/v1/cards/{ACCT}/balance").mock(return_value=ok([BALANCE]))
    b = run(provider().get_balance(ACCT)).to_dict()
    assert b["balance_minor"] == -131245  # owed on the card -> negative
    assert b["available_credit_minor"] == 368755
    assert b["credit_limit_minor"] == 500000
    assert b["last_statement_balance_minor"] == -84210
    assert b["payment_due_minor"] == 2500 and b["payment_due_date"] == "2026-10-15"
    assert b["currency"] == "GBP" and b["source"] == "amex"
    assert b["as_of"] == "2026-10-08T07:00:00Z"


@respx.mock
def test_transactions_normalisation_signs_ids_foreign_currency():
    route = respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok(TRANSACTIONS))
    mock_pending()
    txns = [t.to_dict() for t in listed(provider())]

    # one day of padding before `since` (timestamps are converted to London dates afterwards)
    assert dict(route.calls[0].request.url.params) == {"from": "2026-09-30", "to": "2026-10-08"}
    assert route.calls[0].request.headers["User-Agent"].startswith("amex-mcp/")
    assert [t["amount_minor"] for t in txns] == [-4567, -4321, 1999, 25000]  # charge, charge, refund, payment
    assert [t["date"] for t in txns] == ["2026-10-02", "2026-10-04", "2026-10-05", "2026-10-06"]
    tesco, diner, _refund, payment = txns
    assert tesco == {
        "id": tesco["id"],
        "source": "amex",
        "account_id": ACCT,
        "date": "2026-10-02",
        "amount_minor": -4567,
        "currency": "GBP",
        "description": "TESCO STORES 6021 LONDON",
        "merchant": "Tesco",
        "category": "Shopping > Groceries",
        "pending": False,
        "notes": None,
    }
    # foreign-currency charge: billed in GBP, original currency details surfaced in notes
    assert diner["currency"] == "GBP" and diner["merchant"] is None and diner["category"] is None
    assert diner["notes"] == "foreign_amount=54.00 USD; exchange_rate=1.2871"
    assert payment["notes"] == "type=CREDIT"
    assert all(t["pending"] is False for t in txns)
    assert len({t["id"] for t in txns}) == 4 and all(t["id"].startswith("amex_") for t in txns)


@respx.mock
def test_ids_are_stable_across_runs_even_when_truelayer_transaction_id_changes():
    second = [dict(t, transaction_id=f"changed-{i}") for i, t in enumerate(TRANSACTIONS)]
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(side_effect=[ok(TRANSACTIONS), ok(second)])
    mock_pending()
    p = provider()
    first_ids = [t.id for t in listed(p)]
    assert first_ids == [t.id for t in listed(p)]


@respx.mock
def test_booked_ids_do_not_depend_on_the_account_id():
    """TrueLayer's account_id may change when the card is re-linked; ids must survive that."""
    other = "0123456789abcdef0123456789abcdef"
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok(TRANSACTIONS))
    respx.get(f"{API}/data/v1/cards/{other}/transactions").mock(return_value=ok(TRANSACTIONS))
    mock_pending()
    respx.get(f"{API}/data/v1/cards/{other}/transactions/pending").mock(return_value=ok([]))
    before_relink = [t.id for t in listed(provider())]
    after_relink = [t.id for t in run(provider().list_transactions(other, SINCE, BEFORE)).transactions]
    assert before_relink == after_relink


@respx.mock
def test_hash_ids_when_provider_gives_no_id_and_identical_rows_stay_distinct():
    bare = {
        "transaction_id": "v",
        "timestamp": "2026-10-03T00:00:00Z",
        "description": "PRET A MANGER   LONDON",
        "amount": 4.25,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "transaction_category": "PURCHASE",
        "transaction_classification": [],
    }
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(
        side_effect=[ok([bare, dict(bare)]), ok([dict(bare, transaction_id="w"), dict(bare, transaction_id="x")])]
    )
    mock_pending()
    p = provider()
    a = listed(p)
    b = listed(p)
    assert len(a) == 2 and a[0].id != a[1].id
    assert sorted(t.id for t in a) == sorted(t.id for t in b)


@respx.mock
def test_sign_falls_back_to_amount_when_type_missing():
    rows = [
        {"transaction_id": "1", "timestamp": "2026-10-03T00:00:00Z", "description": "A", "amount": 10.0},
        {"transaction_id": "2", "timestamp": "2026-10-03T00:00:00Z", "description": "B", "amount": -3.0},
    ]
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok(rows))
    mock_pending()
    amounts = {t.description: t.amount_minor for t in listed(provider())}
    assert amounts == {"A": -1000, "B": 300}


@respx.mock
def test_pending_items_only_when_requested_and_always_reported():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok(TRANSACTIONS))
    mock_pending(PENDING)
    p = provider()
    page = run(p.list_transactions(ACCT, SINCE, BEFORE))
    assert len(page.transactions) == 4 and not any(t.pending for t in page.transactions)
    # the pending item was left out, and the caller is told so
    assert page.pending_supported is True
    assert page.pending_excluded == 1 and page.earliest_pending_date == "2026-10-07"

    with_pending = run(p.list_transactions(ACCT, SINCE, BEFORE, include_pending=True))
    pend = [t for t in with_pending.transactions if t.pending]
    assert len(with_pending.transactions) == 5 and len(pend) == 1
    assert with_pending.pending_excluded == 0 and with_pending.earliest_pending_date == "2026-10-07"
    assert pend[0].amount_minor == -340 and pend[0].date == "2026-10-07"
    assert "pending" in pend[0].notes
    assert pend[0].id not in {t.id for t in with_pending.transactions if not t.pending}


@respx.mock
def test_pending_fields_describe_only_the_requested_window():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok([]))
    mock_pending([dict(PENDING[0], timestamp="2026-09-20T00:00:00Z"), PENDING[0]])
    page = run(provider().list_transactions(ACCT, SINCE, BEFORE, include_pending=True))
    assert [t.date for t in page.transactions] == ["2026-10-07"]
    assert page.pending_excluded == 0 and page.earliest_pending_date == "2026-10-07"
    page = run(provider().list_transactions(ACCT, SINCE, BEFORE))
    assert page.transactions == [] and page.pending_excluded == 1 and page.earliest_pending_date == "2026-10-07"


@respx.mock
def test_pending_endpoint_unsupported_is_reported_not_swallowed():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok(TRANSACTIONS))
    mock_pending(response=httpx.Response(501, json={"error": "endpoint_not_supported"}))
    for include in (False, True):
        page = run(provider().list_transactions(ACCT, SINCE, BEFORE, include_pending=include))
        assert len(page.transactions) == 4
        assert page.pending_supported is False
        assert page.pending_excluded == 0 and page.earliest_pending_date is None


@respx.mock
def test_window_filter_since_inclusive_before_exclusive():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok(TRANSACTIONS))
    mock_pending()
    txns = listed(provider(), date(2026, 10, 4), date(2026, 10, 6))
    assert [t.date for t in txns] == ["2026-10-04", "2026-10-05"]


# -------------------------------------------------------- dates (Europe/London)


def _row(n, timestamp, **extra):
    return {
        "transaction_id": f"v{n}",
        "normalised_provider_transaction_id": f"txn-{n}",
        "timestamp": timestamp,
        "description": f"SHOP {n}",
        "amount": 1.0 + n,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "transaction_category": "PURCHASE",
        "transaction_classification": [],
        **extra,
    }


@respx.mock
def test_timestamps_are_converted_to_london_dates_across_the_bst_boundary():
    rows = [
        _row(1, "2026-07-01T23:30:00Z"),  # BST (UTC+1): 00:30 on 2 July in London
        _row(2, "2026-07-01T22:59:59+00:00"),  # 23:59:59 on 1 July in London
        _row(3, "2026-07-02T00:30:00+02:00"),  # 23:30 on 1 July in London
        _row(4, "2026-12-01T23:30:00Z"),  # GMT: still 1 December
        _row(5, "2026-07-01T00:00:00"),  # no offset: taken at face value
        _row(6, "2026-07-03"),  # bare date
        _row(7, "2026-10-25T00:30:00Z"),  # clocks go back at 01:00 UTC that day: 01:30 BST, same date
        _row(8, "2026-03-28T23:59:00.1234567Z"),  # day before clocks go forward (GMT), 7-digit fraction
    ]
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok(rows))
    mock_pending()
    got = {t.description: t.date for t in listed(provider(), date(2026, 1, 1), date(2026, 12, 31))}
    assert got == {
        "SHOP 1": "2026-07-02",
        "SHOP 2": "2026-07-01",
        "SHOP 3": "2026-07-01",
        "SHOP 4": "2026-12-01",
        "SHOP 5": "2026-07-01",
        "SHOP 6": "2026-07-03",
        "SHOP 7": "2026-10-25",
        "SHOP 8": "2026-03-28",
    }


@respx.mock
def test_row_late_on_the_previous_utc_day_is_found_by_a_window_starting_on_its_london_date():
    route = respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(
        return_value=ok([_row(1, "2026-07-01T23:30:00Z"), _row(2, "2026-07-01T12:00:00Z")])
    )
    mock_pending()
    txns = listed(provider(), date(2026, 7, 2), date(2026, 7, 3))
    assert route.calls[0].request.url.params["from"] == "2026-07-01"
    assert [(t.description, t.date) for t in txns] == [("SHOP 1", "2026-07-02")]


@respx.mock
def test_timestamp_that_is_not_a_date_is_a_provider_error():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok([_row(1, "yesterday")]))
    with pytest.raises(ProviderError, match=r"\[provider_error\].*unexpected shape"):
        listed(provider())


# ------------------------------------------------------------- de-duplication


@respx.mock
def test_two_rows_sharing_a_provider_id_but_differing_are_both_kept(caplog):
    a = _row(1, "2026-10-02T00:00:00Z")
    b = dict(_row(1, "2026-10-03T00:00:00Z"), description="SHOP 1 SECOND LEG", amount=9.0)  # same provider id
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok([a, b, dict(a)]))
    mock_pending()
    p = provider()
    with caplog.at_level(logging.WARNING):
        first = listed(p)
    assert [(t.description, t.amount_minor) for t in first] == [("SHOP 1", -200), ("SHOP 1 SECOND LEG", -900)]
    assert first[0].id != first[1].id
    assert "share one provider id" in caplog.text
    # deterministic: the same ids come back on the next call
    assert [t.id for t in listed(p)] == [t.id for t in first]


@respx.mock
def test_two_cards_reusing_a_provider_id_do_not_collide():
    other = "0123456789abcdef0123456789abcdef"
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok([_row(1, "2026-10-02T00:00:00Z")]))
    respx.get(f"{API}/data/v1/cards/{other}/transactions").mock(
        return_value=ok([dict(_row(1, "2026-10-02T00:00:00Z"), description="OTHER CARD", amount=50)])
    )
    mock_pending()
    respx.get(f"{API}/data/v1/cards/{other}/transactions/pending").mock(return_value=ok([]))
    p = provider()
    first = listed(p)
    second = run(p.list_transactions(other, SINCE, BEFORE)).transactions
    assert len(first) == len(second) == 1 and first[0].id != second[0].id
    # and each card keeps its id on later calls
    assert listed(p)[0].id == first[0].id
    assert run(p.list_transactions(other, SINCE, BEFORE)).transactions[0].id == second[0].id


# ------------------------------------------------------------------ pagination


def test_windows_cover_range_with_a_one_day_overlap():
    wins = _windows(date(2026, 1, 1), date(2026, 3, 31), 31)
    assert wins == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 1, 31), date(2026, 3, 2)),
        (date(2026, 3, 2), date(2026, 3, 31)),
    ]
    assert _windows(date(2026, 1, 1), date(2026, 1, 1), 31) == [(date(2026, 1, 1), date(2026, 1, 1))]
    for (_, prev_end), (next_start, _) in itertools.pairwise(wins):
        assert next_start == prev_end
    assert len(_windows(date(2026, 1, 1), date(2026, 1, 5), 1)) == 4  # a 1-day window is widened to 2


@respx.mock
def test_long_range_is_paged_in_overlapping_windows_and_merged_without_duplicates():
    pages = {
        "2026-06-30": [_row(1, "2026-07-01T00:00:00Z"), _row(2, "2026-07-30T00:00:00Z")],
        # the shared day is returned by both windows
        "2026-07-30": [_row(2, "2026-07-30T00:00:00Z"), _row(3, "2026-08-15T00:00:00Z")],
        "2026-08-29": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(pages[request.url.params["from"]])

    route = respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(side_effect=handler)
    mock_pending()
    txns = listed(provider(window_days=31), date(2026, 7, 1), date(2026, 9, 10))

    assert [(c.request.url.params["from"], c.request.url.params["to"]) for c in route.calls] == [
        ("2026-06-30", "2026-07-30"),
        ("2026-07-30", "2026-08-29"),
        ("2026-08-29", "2026-09-10"),
    ]
    assert [t.description for t in txns] == ["SHOP 1", "SHOP 2", "SHOP 3"]


@respx.mock
def test_nothing_is_lost_if_the_provider_treats_to_as_start_of_day():
    """Simulate a provider whose `to` is exclusive of that day: the overlap day covers it."""
    rows = [_row(n, f"2026-07-{d:02d}T10:00:00Z") for n, d in enumerate(range(1, 21))]

    def handler(request: httpx.Request) -> httpx.Response:
        lo, hi = request.url.params["from"], request.url.params["to"]
        return ok([r for r in rows if lo <= r["timestamp"][:10] < hi])

    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(side_effect=handler)
    mock_pending()
    txns = listed(provider(window_days=7), date(2026, 7, 1), date(2026, 7, 20))
    assert [t.date for t in txns] == [f"2026-07-{d:02d}" for d in range(1, 20)]


@respx.mock
def test_future_before_is_clamped_to_today():
    route = respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok([]))
    mock_pending()
    listed(provider(), date(2026, 10, 9), date(2026, 11, 10))  # "today" is pinned to 2026-10-11
    assert dict(route.calls[0].request.url.params) == {"from": "2026-10-08", "to": "2026-10-11"}


@respx.mock
def test_request_fan_out_is_bounded():
    route = respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(return_value=ok([]))
    mock_pending()
    # the pathological call from the review: half a century of history, smallest windows
    with pytest.raises(InvalidRequestError, match=r"\[invalid_request\].*limit per call"):
        listed(provider(window_days=2), date(1970, 1, 1), date(2026, 10, 1))
    assert route.call_count == 0
    # the widest range the server allows by default (400 days) costs a handful of requests
    listed(provider(), date(2025, 9, 1), date(2026, 10, 6))
    assert route.call_count <= 6


@respx.mock
def test_request_budget_also_caps_retries():
    route = respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(
        return_value=httpx.Response(503, json={"error": "provider_error"})
    )
    p = TrueLayerProvider(config(), fresh_store(), backoff_seconds=0, max_retries=1000)
    with pytest.raises(ProviderError, match=rf"Stopped after {MAX_REQUESTS_PER_CALL} upstream requests"):
        listed(p)
    assert route.call_count == MAX_REQUESTS_PER_CALL


@pytest.mark.parametrize("bad", ["../me", "abc/def", "", "x" * 200, "١٢٣", "café", "a b", "a%2Fb"])
def test_malformed_account_id_is_rejected_before_any_request(bad):
    # no respx mock: any HTTP request would fail the test with a different error
    p = provider()
    with pytest.raises(InvalidRequestError, match=r"\[invalid_request\]"):
        run(p.get_balance(bad))
    with pytest.raises(InvalidRequestError):
        run(p.list_transactions(bad, SINCE, BEFORE))


@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"results": "nope"}),
        httpx.Response(200, json={"results": ["not-a-dict"]}),
        httpx.Response(200, json={"results": [{"no_account_id": True}]}),
    ],
)
@respx.mock
def test_malformed_or_non_json_responses_are_provider_errors(body):
    respx.get(f"{API}/data/v1/cards").mock(return_value=body)
    with pytest.raises(ProviderError, match=r"\[provider_error\]"):
        run(provider().list_accounts())


@respx.mock
def test_absurd_amount_from_the_provider_is_a_provider_error():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(
        return_value=ok([dict(_row(1, "2026-10-02T00:00:00Z"), amount="1e999999999")])
    )
    with pytest.raises(ProviderError, match="unexpected shape"):
        listed(provider())


# ---------------------------------------------------------- token lifecycle (A9)


class FlakyStore:
    """A store whose save() can be made to fail."""

    def __init__(self, initial, fail=True):
        self.doc = dict(initial)
        self.fail = fail
        self.save_attempts = 0

    def load(self):
        return dict(self.doc)

    def save(self, tokens):
        self.save_attempts += 1
        if self.fail:
            raise OSError("disk full")
        self.doc = dict(tokens)


def _token_handler(issued: list[str]):
    """Token endpoint that rotates: each refresh token works once."""
    valid = {"refresh-1"}

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["refresh_token"]
        issued.append(sent)
        if sent not in valid:
            return httpx.Response(400, json={"error": "invalid_grant"})
        valid.discard(sent)
        n = len(issued) + 1
        valid.add(f"refresh-{n}")
        return httpx.Response(
            200, json={"access_token": f"access-{n}", "expires_in": 1, "refresh_token": f"refresh-{n}"}
        )

    return handler


@respx.mock
def test_failed_save_after_refresh_keeps_new_tokens_in_memory_and_retries_the_save():
    store = FlakyStore({"refresh_token": "refresh-1"})
    sent: list[str] = []
    respx.post(f"{AUTH}/connect/token").mock(side_effect=_token_handler(sent))
    cards = respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    p = provider(store)

    # 1. the data call is served although the rotated token could not be saved (no bare OSError)
    assert len(run(p.list_accounts())) == 1
    assert cards.calls[0].request.headers["Authorization"] == "Bearer access-2"
    warning = p.pending_warning()
    assert warning.startswith("[token_store_error]") and "could not be saved" in warning
    assert "refresh-2" not in warning and "disk full" not in warning

    # 2. next call (access token already expired): the NEW refresh token is used, never the old one
    assert len(run(p.list_accounts())) == 1
    assert sent == ["refresh-1", "refresh-2"]
    assert store.doc == {"refresh_token": "refresh-1"}  # still nothing persisted
    assert p.pending_warning()

    # 3. the store recovers: the save is retried on the next call and the warning clears
    store.fail = False
    assert len(run(p.list_accounts())) == 1
    assert store.doc["refresh_token"] == "refresh-4" and sent == ["refresh-1", "refresh-2", "refresh-3"]
    assert p.pending_warning() is None


@respx.mock
def test_failed_save_is_retried_even_without_another_refresh():
    store = FlakyStore({"refresh_token": "refresh-1"})
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a2", "expires_in": 3600, "refresh_token": "r2"})
    )
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    p = provider(store)
    run(p.list_accounts())
    assert store.save_attempts == 1 and store.doc["refresh_token"] == "refresh-1"
    store.fail = False
    run(p.list_accounts())
    assert store.save_attempts == 2 and store.doc["refresh_token"] == "r2" and store.doc["access_token"] == "a2"


@respx.mock
def test_failed_save_shows_in_connection_status_warning():
    store = FlakyStore({"refresh_token": "refresh-1"})
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a2", "expires_in": 3600, "refresh_token": "r2"})
    )
    respx.get(f"{API}/data/v1/me").mock(return_value=ok([ME]))
    s = run(provider(store).connection_status())
    assert s["connected"] is True and s["warning"].startswith("[token_store_error]")


@respx.mock
def test_exchange_code_reports_a_store_failure_as_token_store_error():
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600, "refresh_token": "r-new"})
    )
    with pytest.raises(TokenStoreError, match=r"\[token_store_error\]"):
        run(provider(FlakyStore({})).exchange_code("c", "http://localhost:3000/callback"))


@respx.mock
def test_concurrent_401s_cause_exactly_one_refresh():
    token_route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600, "refresh_token": "r2"})
    )

    def cards(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer access-2":
            return ok([CARD])
        return httpx.Response(401, json={"error": "invalid_token"})

    respx.get(f"{API}/data/v1/cards").mock(side_effect=cards)
    p = provider()  # holds "access-1", which the API no longer accepts

    async def go():
        return await asyncio.gather(*(p.list_accounts() for _ in range(5)))

    results = run(go())
    assert all(len(r) == 1 for r in results)
    assert token_route.call_count == 1


@respx.mock
def test_store_token_wins_over_env_seed_and_seed_is_used_only_when_store_is_empty():
    token_route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a2", "expires_in": 3600})
    )
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    run(provider(MemoryTokenStore({"refresh_token": "from-store"}), seed_refresh_token="from-env").list_accounts())
    assert json.loads(token_route.calls[0].request.content)["refresh_token"] == "from-store"
    run(provider(MemoryTokenStore(), seed_refresh_token="from-env").list_accounts())
    assert json.loads(token_route.calls[1].request.content)["refresh_token"] == "from-env"


def test_store_that_raises_on_load_is_a_token_store_error():
    class Broken:
        def load(self):
            raise RuntimeError("vault sealed: token=hunter2")

        def save(self, tokens):
            pass

    with pytest.raises(TokenStoreError, match=r"\[token_store_error\]") as info:
        run(provider(Broken()).list_accounts())
    assert "hunter2" not in str(info.value)


# ------------------------------------------------------------ base-URL overrides


@pytest.mark.parametrize(
    "url, ok_",
    [
        ("https://api.example.test", True),
        ("http://127.0.0.1:8080", True),
        ("http://localhost:8080/", True),
        ("http://[::1]:9000", True),
        ("http://api.example.test", False),
        ("http://127.0.0.1.evil.example", False),
        ("ftp://127.0.0.1", False),
        ("api.truelayer.com", False),
    ],
)
def test_base_url_overrides_must_be_https_unless_loopback(url, ok_):
    env = {"TRUELAYER_CLIENT_ID": "x", "TRUELAYER_CLIENT_SECRET": "y"}
    for var in ("TRUELAYER_API_BASE", "TRUELAYER_AUTH_BASE"):
        if ok_:
            cfg = TrueLayerConfig.from_env({**env, var: url})
            assert url.rstrip("/") in (cfg.api_base, cfg.auth_base)
        else:
            with pytest.raises(ConfigError, match=rf"\[config_error\] {var} must be an https"):
                TrueLayerConfig.from_env({**env, var: url})


# ----------------------------------------------------------------- error cases


@respx.mock
def test_rate_limit_is_reported_with_retry_after_and_not_retried():
    route = respx.get(f"{API}/data/v1/cards").mock(
        return_value=httpx.Response(
            429, json={"error": "provider_request_limit_exceeded"}, headers={"Retry-After": "21600"}
        )
    )
    with pytest.raises(RateLimitError, match=r"\[rate_limited\].*few unattended.*retry after 21600s") as info:
        run(provider().list_accounts())
    assert info.value.retry_after_seconds == 21600
    assert route.call_count == 1


@respx.mock
def test_rate_limit_without_retry_after():
    respx.get(f"{API}/data/v1/cards").mock(
        return_value=httpx.Response(429, json={"error": "provider_too_many_requests"})
    )
    with pytest.raises(RateLimitError, match="retry in 6 hours"):
        run(provider().list_accounts())


@respx.mock
def test_sca_exceeded_is_explained():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(
        return_value=httpx.Response(403, json={"error": "sca_exceeded"})
    )
    with pytest.raises(ScaRequiredError, match="90 days"):
        run(provider().list_transactions(ACCT, SINCE, BEFORE))


@respx.mock
def test_unknown_account():
    respx.get(f"{API}/data/v1/cards/{ACCT}/balance").mock(
        return_value=httpx.Response(404, json={"error": "account_not_found"})
    )
    with pytest.raises(AccountNotFoundError, match="list_accounts"):
        run(provider().get_balance(ACCT))


@respx.mock
def test_invalid_date_range():
    respx.get(f"{API}/data/v1/cards/{ACCT}/transactions").mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_date_range", "error_description": "from is too far in the past"}
        )
    )
    with pytest.raises(InvalidRequestError, match=r"invalid_date_range.*too far"):
        run(provider().list_transactions(ACCT, SINCE, BEFORE))


@respx.mock
def test_upstream_5xx_is_retried_then_succeeds():
    route = respx.get(f"{API}/data/v1/cards").mock(
        side_effect=[httpx.Response(503, json={"error": "provider_error"}), ok([CARD])]
    )
    assert len(run(provider().list_accounts())) == 1
    assert route.call_count == 2


@respx.mock
def test_upstream_5xx_gives_up_with_provider_error():
    route = respx.get(f"{API}/data/v1/cards").mock(return_value=httpx.Response(503, json={"error": "provider_error"}))
    with pytest.raises(ProviderError, match="temporarily unavailable"):
        run(provider().list_accounts())
    assert route.call_count == 3


@respx.mock
def test_network_failure_is_a_provider_error():
    respx.get(f"{API}/data/v1/cards").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="Could not reach TrueLayer"):
        run(provider().list_accounts())


@respx.mock
def test_unsupported_endpoint():
    respx.get(f"{API}/data/v1/cards/{ACCT}/balance").mock(
        return_value=httpx.Response(501, json={"error": "endpoint_not_supported"})
    )
    with pytest.raises(NotSupportedError, match=r"\[provider_error\]"):
        run(provider().get_balance(ACCT))


# ------------------------------------------------------------ connection status


@respx.mock
def test_connection_status_reports_expiry():
    soon = (datetime.now(UTC) + timedelta(days=40, hours=1)).isoformat()
    respx.get(f"{API}/data/v1/me").mock(return_value=ok([dict(ME, consent_expires_at=soon)]))
    s = run(provider().connection_status())
    assert s["connected"] is True and s["needs_reauthorisation"] is False
    assert s["days_remaining"] == 40 and s["consent_expires_at"] == utc_z(soon) and s["warning"] is None
    assert s["consent_expires_at"].endswith("Z")
    assert s["aggregator_provider_id"] == "ob-amex"


@respx.mock
def test_connection_status_warns_when_expiry_is_near():
    soon = (datetime.now(UTC) + timedelta(days=5, hours=1)).isoformat()
    respx.get(f"{API}/data/v1/me").mock(return_value=ok([dict(ME, consent_expires_at=soon)]))
    s = run(provider().connection_status())
    assert s["needs_reauthorisation"] is False and "expires in 5 day" in s["message"]
    assert s["warning"] == s["message"]


@respx.mock
def test_connection_status_warning_threshold_is_under_14_days():
    def status_at(days):
        when = (datetime.now(UTC) + timedelta(days=days, hours=1)).isoformat()
        respx.get(f"{API}/data/v1/me").mock(return_value=ok([dict(ME, consent_expires_at=when)]))
        return run(provider().connection_status())

    at_14 = status_at(14)
    assert at_14["warning"] is None and at_14["days_remaining"] == 14
    assert "13 day" in status_at(13)["warning"]


@respx.mock
def test_connection_status_expired_consent_does_not_raise():
    respx.post(f"{AUTH}/connect/token").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    s = run(provider(MemoryTokenStore({"refresh_token": "r"})).connection_status())
    assert s["connected"] is False and s["needs_reauthorisation"] is True and s["error"] == "consent_expired"
    assert s["warning"].startswith("[consent_expired]") and s["days_remaining"] is None


@respx.mock
def test_connection_status_past_expiry_date():
    respx.get(f"{API}/data/v1/me").mock(return_value=ok([dict(ME, consent_expires_at="2020-01-01T00:00:00Z")]))
    s = run(provider().connection_status())
    assert s["needs_reauthorisation"] is True and s["days_remaining"] < 0
    assert s["connected"] is False and s["warning"].startswith("[consent_expired]")


@respx.mock
def test_connection_status_estimates_expiry_when_not_reported():
    me = {k: v for k, v in ME.items() if k != "consent_expires_at"}
    respx.get(f"{API}/data/v1/me").mock(return_value=ok([me]))
    connected = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    s = run(provider(fresh_store(connected_at=connected)).connection_status())
    assert s["consent_expiry_is_estimate"] is True and s["days_remaining"] in (79, 80)


def test_connection_status_never_linked():
    s = run(provider(MemoryTokenStore()).connection_status())
    assert s["connected"] is False and s["error"] == "not_connected"
    assert s["warning"].startswith("[not_connected]") and s["needs_reauthorisation"] is True


@pytest.mark.parametrize(
    "response, code",
    [
        (httpx.Response(429, json={"error": "provider_too_many_requests"}), "rate_limited"),
        (httpx.Response(503, json={"error": "provider_error"}), "provider_error"),
        (httpx.Response(200, text="not json"), "provider_error"),
        (httpx.Response(403, json={"error": "access_denied"}), "consent_expired"),
        (httpx.Response(418, text="teapot"), "provider_error"),
    ],
)
@respx.mock
def test_connection_status_never_raises_for_provider_problems(response, code):
    respx.get(f"{API}/data/v1/me").mock(return_value=response)
    s = run(provider().connection_status())
    assert s["connected"] is False and s["error"] == code
    assert s["warning"].startswith(f"[{code}]") and s["days_remaining"] is None


@respx.mock
def test_connection_status_network_failure_does_not_raise():
    respx.get(f"{API}/data/v1/me").mock(side_effect=httpx.ConnectError("boom"))
    s = run(provider().connection_status())
    assert s["connected"] is False and s["warning"].startswith("[provider_error]")


# ----------------------------------------------------------------- consent flow


@respx.mock
def test_exchange_code_stores_tokens():
    store = MemoryTokenStore()
    route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600, "refresh_token": "r-new"})
    )
    run(provider(store).exchange_code("the-code", "http://localhost:3000/callback"))
    sent = json.loads(route.calls[0].request.content)
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "the-code"
    assert sent["redirect_uri"] == "http://localhost:3000/callback"
    assert store.load()["refresh_token"] == "r-new" and "connected_at" in store.load()


@respx.mock
def test_exchange_code_without_offline_access_is_an_error():
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    with pytest.raises(ProviderError, match="offline_access"):
        run(provider(MemoryTokenStore()).exchange_code("c", "http://localhost:3000/callback"))


@respx.mock
def test_stale_authorisation_code():
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_authorization_code"})
    )
    with pytest.raises(InvalidRequestError, match="5 minutes"):
        run(provider(MemoryTokenStore()).exchange_code("c", "http://localhost:3000/callback"))
