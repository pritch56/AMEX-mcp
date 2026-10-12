"""TrueLayer Data API v1 provider (cards endpoints), read-only.

Docs used (read October 2026; never exercised against the live API):
  https://docs.truelayer.com/docs/card-data-requests
  https://docs.truelayer.com/reference/generateaccesstoken
  https://docs.truelayer.com/docs/data-api-errors
  https://docs.truelayer.com/docs/general-data-api-concepts
  https://auth.truelayer.com/api/providers   (lists provider_id "ob-amex")

TrueLayer card sign convention: POSITIVE amount = money out of the card (purchase),
NEGATIVE = money in (refund / payment). This module inverts that.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import USER_AGENT
from ..errors import (
    AccountNotFoundError,
    AmexMcpError,
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
from ..models import (
    Account,
    Balance,
    Transaction,
    TransactionPage,
    clean_text,
    date_in_window,
    london_date,
    london_today,
    merge_transactions,
    stable_id,
    to_minor,
    utc_z,
)
from ..token_store import TokenStore
from .base import Provider

log = logging.getLogger("amex_mcp.truelayer")

ENVIRONMENTS = {
    "live": ("https://auth.truelayer.com", "https://api.truelayer.com"),
    "sandbox": ("https://auth.truelayer-sandbox.com", "https://api.truelayer-sandbox.com"),
}
SCOPES = "info cards balance transactions offline_access"
RELINK_HINT = "Re-authorise the card by running the link helper on your own machine (`amex-mcp-link`)."
# TrueLayer's default window when from/to are omitted; also a safe single-request span.
MAX_WINDOW_DAYS = 88
REAUTH_WARNING_DAYS = 14
# Hard ceiling on upstream GET requests (including retries) made by one tool call.
MAX_REQUESTS_PER_CALL = 30
_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_LOOPBACK_NAMES = ("localhost",)


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_base_url(name: str, url: str) -> str:
    """Base-URL overrides are test hooks: they must be https unless they point at loopback."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    if parsed.scheme == "http" and is_loopback_host(parsed.hostname):
        return url.rstrip("/")
    raise ConfigError(f"{name} must be an https:// URL (plain http is allowed only for a loopback host).")


class TrueLayerConfig:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        environment: str = "live",
        auth_base: str | None = None,
        api_base: str | None = None,
        seed_refresh_token: str | None = None,
        token_encoding: str = "json",  # noqa: S107 - an encoding name, not a secret
        window_days: int = MAX_WINDOW_DAYS,
    ):
        if environment not in ENVIRONMENTS:
            raise ConfigError("TRUELAYER_ENV must be 'live' or 'sandbox'.")
        if not client_id or not client_secret:
            raise ConfigError(
                "TRUELAYER_CLIENT_ID and TRUELAYER_CLIENT_SECRET must be set "
                "(from the TrueLayer Console). See README.md."
            )
        default_auth, default_api = ENVIRONMENTS[environment]
        self.client_id = client_id
        self.client_secret = client_secret
        self.environment = environment
        self.auth_base = check_base_url("TRUELAYER_AUTH_BASE", auth_base) if auth_base else default_auth
        self.api_base = check_base_url("TRUELAYER_API_BASE", api_base) if api_base else default_api
        self.seed_refresh_token = seed_refresh_token or None
        self.token_encoding = token_encoding if token_encoding in ("json", "form") else "json"
        # At least 2: consecutive windows share one day.
        self.window_days = max(2, min(int(window_days), MAX_WINDOW_DAYS))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TrueLayerConfig:
        e = os.environ if env is None else env
        try:
            window_days = int(e.get("TRUELAYER_WINDOW_DAYS") or MAX_WINDOW_DAYS)
        except ValueError:
            raise ConfigError("TRUELAYER_WINDOW_DAYS must be an integer.") from None
        return cls(
            client_id=e.get("TRUELAYER_CLIENT_ID", ""),
            client_secret=e.get("TRUELAYER_CLIENT_SECRET", ""),
            environment=e.get("TRUELAYER_ENV", "live").strip().lower() or "live",
            auth_base=e.get("TRUELAYER_AUTH_BASE") or None,
            api_base=e.get("TRUELAYER_API_BASE") or None,
            # Only the Amex-specific seed is read: a generic TRUELAYER_REFRESH_TOKEN could belong
            # to a different connection made with the same TrueLayer application.
            seed_refresh_token=e.get("AMEX_TRUELAYER_REFRESH_TOKEN") or None,
            token_encoding=e.get("TRUELAYER_TOKEN_ENCODING", "json").strip().lower(),
            window_days=window_days,
        )

    def __repr__(self) -> str:  # never leak the secret through logs / tracebacks
        return f"TrueLayerConfig(client_id={self.client_id!r}, environment={self.environment!r})"


def _today() -> date:
    """A `to` in the future is rejected, so "today" is the earlier of London's and UTC's date."""
    return min(london_today(), datetime.now(UTC).date())


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


class _Budget:
    """Counts upstream requests made on behalf of one tool call."""

    def __init__(self, limit: int = MAX_REQUESTS_PER_CALL):
        self.limit = limit
        self.spent = 0

    def spend(self) -> None:
        if self.spent >= self.limit:
            raise ProviderError(
                f"Stopped after {self.limit} upstream requests for one call (safety limit). "
                "Ask for a shorter date range, or retry later if the provider is failing."
            )
        self.spent += 1


class TrueLayerProvider(Provider):
    name = "truelayer"

    def __init__(
        self,
        config: TrueLayerConfig,
        store: TokenStore,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
    ):
        self.config = config
        self.store = store
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._owns_client = client is None
        self._refresh_lock = asyncio.Lock()
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        # Newest token set obtained by this process, and whether it still has to be saved.
        self._tokens: dict[str, Any] | None = None
        self._unsaved = False
        self._store_error: str | None = None
        # provider-id-derived transaction id -> (account_id, content) that first used it.
        self._id_owners: dict[str, tuple[str, tuple]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def pending_warning(self) -> str | None:
        return f"[{TokenStoreError.code}] {self._store_error}" if self._store_error else None

    # ------------------------------------------------------------------ tokens

    def _save(self, tokens: dict[str, Any]) -> bool:
        """Persist the token set. Never raises: on failure the tokens stay in memory, the
        save is retried on the next call, and the problem is reported via pending_warning()."""
        try:
            self.store.save(tokens)
        except Exception as exc:
            detail = exc.message if isinstance(exc, AmexMcpError) else f"{type(exc).__name__} from the token store."
            self._unsaved = True
            self._store_error = (
                f"The refreshed tokens could not be saved: {detail} They are held in memory only; if this "
                "process stops before a save succeeds, the card may have to be linked again."
            )
            log.warning("token store save failed (%s); keeping the new tokens in memory", type(exc).__name__)
            return False
        self._unsaved = False
        self._store_error = None
        return True

    def _load_tokens(self) -> dict[str, Any]:
        if self._unsaved and self._tokens:
            # Memory is newer than the store. Retry the save; either way use the newer tokens,
            # so a refresh token that may have been rotated away is never sent again.
            self._save(self._tokens)
            return self._tokens
        try:
            tokens = self.store.load()
        except Exception as exc:
            if self._tokens and self._tokens.get("refresh_token"):
                log.warning("token store load failed (%s); using the tokens held in memory", type(exc).__name__)
                return self._tokens
            if isinstance(exc, AmexMcpError):
                raise
            raise TokenStoreError(f"The token store could not be read ({type(exc).__name__}).") from None
        if tokens and tokens.get("refresh_token"):
            # A token in the store always wins over the environment seed.
            self._tokens = tokens
            return tokens
        if self.config.seed_refresh_token:
            # Empty store: bootstrap from the env-provided token.
            return {"refresh_token": self.config.seed_refresh_token}
        raise NotConnectedError(
            "No Amex connection has been set up yet (no refresh token in the token store "
            "and AMEX_TRUELAYER_REFRESH_TOKEN is not set). " + RELINK_HINT
        )

    async def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        url = f"{self.config.auth_base}/connect/token"
        as_json = self.config.token_encoding == "json"  # noqa: S105 - an encoding name, not a secret
        kwargs: dict[str, Any] = {"json": payload} if as_json else {"data": payload}
        try:
            resp = await self._client.post(url, headers=_headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach TrueLayer's token endpoint ({type(exc).__name__}).") from None
        if resp.status_code == 200:
            return _json(resp)
        error = _error_code(resp)
        if error == "invalid_grant":
            raise ConsentExpiredError(
                "TrueLayer rejected the refresh token (invalid_grant): the Amex consent has expired, "
                "been revoked, or the token was superseded. " + RELINK_HINT
            )
        if error in ("invalid_client", "unauthorized_client"):
            raise ConfigError(
                f"TrueLayer rejected the client credentials ({error}). Check TRUELAYER_CLIENT_ID / "
                "TRUELAYER_CLIENT_SECRET and that TRUELAYER_ENV matches the credentials (live vs sandbox)."
            )
        if error == "invalid_authorization_code":
            raise InvalidRequestError(
                "The authorisation code is wrong, already used, or older than 5 minutes. Start the consent flow again."
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "TrueLayer's token endpoint is rate limiting requests. Try again later.", _retry_after(resp)
            )
        raise ProviderError(f"TrueLayer token endpoint returned HTTP {resp.status_code} ({error or 'no error code'}).")

    def _persist(self, previous: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        tokens = dict(previous)
        tokens["access_token"] = body.get("access_token")
        try:
            lifetime = float(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600.0
        tokens["access_token_expires_at"] = _now() + lifetime
        # Refresh tokens may rotate: always keep the newest one; keep the old one if none is returned.
        if body.get("refresh_token"):
            tokens["refresh_token"] = body["refresh_token"]
        tokens["updated_at"] = _iso(_now())
        self._tokens = tokens  # usable from now on, whether or not the save works
        self._save(tokens)
        return tokens

    async def _refresh(self, tokens: dict[str, Any]) -> dict[str, Any]:
        body = await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": tokens["refresh_token"],
            }
        )
        if not body.get("access_token"):
            raise ProviderError("TrueLayer's token response did not contain an access token.")
        return self._persist(tokens, body)

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> dict[str, Any]:
        """Used only by the local link helper. Overwrites whatever the store held."""
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier
        body = await self._token_request(payload)
        if not body.get("refresh_token"):
            raise ProviderError(
                "TrueLayer did not return a refresh token. The 'offline_access' scope must be enabled "
                "for the application and requested in the auth link."
            )
        tokens = self._persist({"connected_at": _iso(_now())}, body)
        if self._unsaved:
            raise TokenStoreError(self._store_error or "The tokens could not be saved.")
        return tokens

    async def _access_token(self, rejected: str | None = None) -> str:
        """Return a usable access token. `rejected` is an access token the API just answered
        401 to: refresh only if it is still the current one, otherwise reuse the newer one."""
        async with self._refresh_lock:
            tokens = self._load_tokens()
            current = tokens.get("access_token")
            try:
                expires_at = float(tokens.get("access_token_expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0.0
            usable = bool(current) and expires_at - 60 > _now()
            if usable and current != rejected:
                return str(current)
            return str((await self._refresh(tokens))["access_token"])

    # ---------------------------------------------------------------- requests

    async def _get(
        self, path: str, params: dict[str, str] | None = None, budget: _Budget | None = None
    ) -> list[dict[str, Any]]:
        url = f"{self.config.api_base}{path}"
        budget = budget or _Budget()
        refreshed = False
        attempt = 0
        token = await self._access_token()
        while True:
            budget.spend()
            try:
                resp = await self._client.get(url, params=params, headers=_headers(token))
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await asyncio.sleep(self._backoff * attempt)
                    continue
                raise ProviderError(f"Could not reach TrueLayer ({type(exc).__name__}).") from None

            status, error = resp.status_code, clean_text(_error_code(resp))[:60]
            if status == 200:
                results = _json(resp).get("results")
                if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
                    raise ProviderError("Unexpected response from TrueLayer (not JSON with a 'results' list).")
                return results
            if status == 401:
                if not refreshed:
                    refreshed = True
                    token = await self._access_token(rejected=token)
                    continue
                raise TokenExpiredError(
                    f"TrueLayer rejected the access token even after refreshing it ({error or 'unauthorized'}). "
                    + RELINK_HINT
                )
            if status == 403:
                if error == "sca_exceeded":
                    raise ScaRequiredError(
                        "Amex requires a fresh authentication for this request (sca_exceeded). This happens "
                        "when asking for history older than about 90 days long after linking. Request a "
                        "more recent range, or re-link the card and fetch the older history within 5 minutes."
                    )
                raise ConsentExpiredError(
                    f"Access to the Amex data was denied ({error or 'access_denied'}): the consent has "
                    "expired or been revoked. " + RELINK_HINT
                )
            if status == 404:
                raise AccountNotFoundError(
                    "TrueLayer does not know this card (account_not_found). Call list_accounts for current "
                    "ids - they can change after the card is re-linked."
                )
            if status == 429:  # never retried
                retry_after = _retry_after(resp)
                raise RateLimitError(
                    f"Rate limited ({error or 'too_many_requests'}). Banks allow only a few unattended "
                    "data fetches per day per endpoint. Do not retry immediately"
                    + (f"; retry after {retry_after}s." if retry_after else "; retry in 6 hours or more."),
                    retry_after,
                )
            if status == 400:
                detail = clean_text(_error_description(resp))[:200]
                if error == "invalid_date_range":
                    raise InvalidRequestError(f"TrueLayer rejected the date range (invalid_date_range). {detail}")
                raise InvalidRequestError(f"TrueLayer rejected the request ({error or 'bad_request'}). {detail}")
            if status == 501:
                raise NotSupportedError(
                    f"Amex does not support this endpoint via TrueLayer ({error or 'endpoint_not_supported'})."
                )
            if status >= 500:
                if attempt < self._max_retries:
                    attempt += 1
                    await asyncio.sleep(self._backoff * attempt)
                    continue
                raise ProviderError(
                    f"TrueLayer / Amex is temporarily unavailable (HTTP {status}, {error or 'no error code'}). "
                    "Safe to retry later."
                )
            raise ProviderError(f"Unexpected HTTP {status} from TrueLayer ({error or 'no error code'}).")

    # ------------------------------------------------------------------- tools

    async def list_accounts(self) -> list[Account]:
        cards = await self._get("/data/v1/cards")
        try:
            return [self._account(c) for c in cards]
        except (KeyError, TypeError, ValueError):
            raise ProviderError("TrueLayer returned a card in an unexpected shape.") from None

    def _account(self, c: dict[str, Any]) -> Account:
        return Account(
            account_id=str(c["account_id"]),
            name=clean_text(c.get("display_name")) or "American Express",
            currency=str(c.get("currency") or "GBP").upper(),
            type="charge_card" if str(c.get("card_type", "")).upper() == "CHARGE" else "credit_card",
            card_network=str(c.get("card_network") or "AMEX").upper(),
            last4=(str(c.get("partial_card_number"))[-4:] if c.get("partial_card_number") else None),
            provider=self.name,
        )

    async def get_balance(self, account_id: str) -> Balance:
        _check_account_id(account_id)
        results = await self._get(f"/data/v1/cards/{account_id}/balance")
        if not results:
            raise ProviderError("TrueLayer returned no balance for this card.")
        b = results[0]
        cur = str(b.get("currency") or "GBP").upper()

        def minor(key: str, negate: bool = False) -> int | None:
            if b.get(key) is None:
                return None
            v = to_minor(b[key], cur)
            return -v if negate else v

        def day(key: str) -> str | None:
            if not b.get(key):
                return None
            try:
                return london_date(b[key])
            except ValueError:
                return None

        try:
            return Balance(
                account_id=account_id,
                currency=cur,
                # ASSUMPTION: `current` = amount spent on the card (positive = owed). Ours: negative = owed.
                balance_minor=minor("current", negate=True),
                available_credit_minor=minor("available"),
                credit_limit_minor=minor("credit_limit"),
                last_statement_balance_minor=minor("last_statement_balance", negate=True),
                last_statement_date=day("last_statement_date"),
                payment_due_minor=minor("payment_due"),
                payment_due_date=day("payment_due_date"),
                as_of=utc_z(b.get("update_timestamp")),
                notes="balance_minor excludes pending transactions",
            )
        except (ValueError, TypeError):
            raise ProviderError("TrueLayer returned a balance in an unexpected shape.") from None

    async def list_transactions(
        self,
        account_id: str,
        since: date,
        before: date,
        include_pending: bool = False,
    ) -> TransactionPage:
        _check_account_id(account_id)
        today = _today()
        # One day of padding before `since`: a timestamp late on the previous UTC day can fall on
        # `since` once converted to London time. Rows outside the window are filtered out below.
        start = since - timedelta(days=1)
        end = min(before, today)
        windows = _windows(start, end, self.config.window_days) if start <= end else []
        budget = _Budget()
        if len(windows) + 1 > budget.limit:
            raise InvalidRequestError(
                f"This range needs {len(windows) + 1} upstream requests; the limit per call is "
                f"{budget.limit}. Ask for a shorter range."
            )

        merged: dict[str, Transaction] = {}
        # The Data API returns a whole date range per call, so long ranges are walked in windows.
        # Consecutive windows share one day (in case `to` is treated as start-of-day); the
        # repeated rows are collapsed by id.
        for win_from, win_to in windows:
            raw = await self._get(
                f"/data/v1/cards/{account_id}/transactions",
                {"from": win_from.isoformat(), "to": win_to.isoformat()},
                budget,
            )
            merge_transactions(self._normalise(raw, account_id, pending=False), log, merged)
        page = TransactionPage(transactions=[t for t in merged.values() if date_in_window(t.date, since, before)])

        # Pending items are always fetched, so that the ones left out can be reported.
        try:
            pending_raw = await self._get(f"/data/v1/cards/{account_id}/transactions/pending", None, budget)
        except NotSupportedError:
            page.pending_supported = False
            pending_raw = []
        pending = merge_transactions(self._normalise(pending_raw, account_id, pending=True), log)
        in_window = [t for t in pending.values() if date_in_window(t.date, since, before)]
        if include_pending:
            page.transactions.extend(in_window)
        else:
            page.pending_excluded = len(in_window)
        page.earliest_pending_date = min((t.date for t in in_window), default=None)
        page.transactions.sort(key=lambda t: (t.date, t.pending, t.id))
        return page

    def _normalise(self, raw: list[dict[str, Any]], account_id: str, pending: bool) -> list[Transaction]:
        try:
            return self._normalise_rows(raw, account_id, pending)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("TrueLayer returned a transaction in an unexpected shape.") from None

    def _normalise_rows(self, raw: list[dict[str, Any]], account_id: str, pending: bool) -> list[Transaction]:
        seen: dict[tuple, int] = {}
        out: list[Transaction] = []
        for r in raw:
            currency = str(r.get("currency") or "GBP").upper()
            signed = to_minor(r.get("amount", 0), currency)
            kind = str(r.get("transaction_type") or "").upper()
            if kind == "DEBIT":
                amount_minor = -abs(signed)
            elif kind == "CREDIT":
                amount_minor = abs(signed)
            else:  # fall back to TrueLayer's card sign rule: positive = money out
                amount_minor = -signed
            day = london_date(r.get("timestamp"))  # ValueError -> provider_error
            description = clean_text(r.get("description"))
            meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
            content = (day, amount_minor, currency, description)

            provider_id = r.get("normalised_provider_transaction_id") or r.get("provider_transaction_id")
            if provider_id and not pending:
                # Deliberately independent of TrueLayer's account_id, which may change on re-link.
                txn_id = self._claim_id(stable_id("tl", provider_id), account_id, content)
            else:
                # `transaction_id` is documented as unstable between requests, and pending items
                # usually have no durable provider id, so derive the id from the content instead.
                key = ("tl-hash", account_id, "pending" if pending else "booked", *content)
                n = seen.get(key, 0)
                seen[key] = n + 1
                txn_id = stable_id(*key, n)

            classification = [clean_text(x) for x in (r.get("transaction_classification") or []) if x]
            category = " > ".join(classification) or clean_text(meta.get("provider_transaction_category")) or None

            notes: list[str] = []
            tl_category = clean_text(r.get("transaction_category")).upper()
            if tl_category and tl_category not in ("PURCHASE", "UNKNOWN"):
                notes.append(f"type={tl_category}")
            for k, v in meta.items():
                if v not in (None, "") and any(s in str(k).lower() for s in ("foreign", "exchange", "original", "fx")):
                    notes.append(f"{clean_text(k)}={clean_text(v)}")
            if pending:
                notes.append("pending: amount and id may change when the charge is booked")

            out.append(
                Transaction(
                    id=txn_id,
                    account_id=account_id,
                    date=day,
                    amount_minor=amount_minor,
                    currency=currency,
                    description=description,
                    merchant=clean_text(r.get("merchant_name")) or None,
                    category=category,
                    pending=pending,
                    notes="; ".join(notes) or None,
                )
            )
        return out

    def _claim_id(self, txn_id: str, account_id: str, content: tuple) -> str:
        """Guard against two cards on one consent reusing a provider transaction id: if another
        account already used this id for a different transaction, derive a content-based id."""
        owner = self._id_owners.get(txn_id)
        if owner is not None and owner[0] != account_id and owner[1] != content:
            log.warning("a provider transaction id is used by two accounts; giving the later one a content-derived id")
            return stable_id("dup", txn_id, *content, 0)
        if len(self._id_owners) >= 200_000:
            self._id_owners.clear()
        self._id_owners[txn_id] = (account_id, content)
        return txn_id

    async def connection_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "provider": self.name,
            "environment": self.config.environment,
            "connected": False,
            "consent_expires_at": None,
            "days_remaining": None,
            "needs_reauthorisation": False,
            "warning": None,
            "message": "",
        }
        try:
            results = await self._get("/data/v1/me")
        except AmexMcpError as exc:
            status["message"] = status["warning"] = str(exc)
            status["error"] = exc.code
            status["needs_reauthorisation"] = isinstance(
                exc, (NotConnectedError, ConsentExpiredError, TokenExpiredError)
            )
            return status

        me = results[0] if results else {}
        expires = utc_z(me.get("consent_expires_at")) if me.get("consent_expires_at") else None
        estimated = False
        if not expires:
            connected_at = utc_z((self._tokens or {}).get("connected_at"))
            if connected_at:
                connected = datetime.fromisoformat(connected_at)
                expires = utc_z((connected + timedelta(days=90)).isoformat())
                estimated = True
        provider_info = me.get("provider") if isinstance(me.get("provider"), dict) else {}
        status.update(
            {
                "connected": True,
                "consent_status": me.get("consent_status"),
                "consent_created_at": utc_z(me.get("consent_created_at")),
                "consent_expires_at": expires,
                "consent_expiry_is_estimate": estimated,
                "aggregator_provider_id": provider_info.get("provider_id"),
                "scopes": me.get("scopes"),
            }
        )
        if not expires:
            status["message"] = (
                "Connected, but TrueLayer did not report a consent expiry. UK consents must be "
                "reconfirmed at least every 90 days."
            )
        else:
            remaining = datetime.fromisoformat(expires) - datetime.now(UTC)
            days = remaining.days
            status["days_remaining"] = days
            if remaining.total_seconds() <= 0:
                status["connected"] = False
                status["needs_reauthorisation"] = True
                status["error"] = ConsentExpiredError.code
                status["message"] = status["warning"] = (
                    f"[{ConsentExpiredError.code}] Consent has expired. " + RELINK_HINT
                )
            elif days < REAUTH_WARNING_DAYS:
                status["message"] = status["warning"] = (
                    f"Consent expires in {days} day(s) on {expires[:10]}. Re-authorise before then "
                    "to avoid a gap: " + RELINK_HINT
                )
            else:
                status["message"] = f"Connected. Consent is valid until {expires[:10]} ({days} days)."
        store_warning = self.pending_warning()
        if store_warning:
            status["warning"] = " ".join(x for x in (store_warning, status["warning"]) if x)
        return status


def _windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Inclusive date windows of at most `days` days covering start..end. Each window starts
    on the last day of the previous one (a one-day overlap)."""
    days = max(2, days)
    out, cur = [], start
    while True:
        win_end = min(cur + timedelta(days=days - 1), end)
        out.append((cur, win_end))
        if win_end >= end:
            return out
        cur = win_end


def _check_account_id(account_id: str) -> None:
    if not isinstance(account_id, str) or not _ID_RE.fullmatch(account_id) or len(account_id) > 128:
        raise InvalidRequestError("account_id is missing or malformed. Use an id returned by list_accounts.")


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(resp: httpx.Response) -> str:
    return str(_json(resp).get("error") or "")


def _error_description(resp: httpx.Response) -> str:
    return str(_json(resp).get("error_description") or "")


def _retry_after(resp: httpx.Response) -> int | None:
    value = resp.headers.get("Retry-After")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
