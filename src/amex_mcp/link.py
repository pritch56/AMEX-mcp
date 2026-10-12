"""One-off link helper - run by YOU, on your own machine, never by the server.

    amex-mcp-link            # print the consent link, wait for the redirect, store tokens
    amex-mcp-link --status   # show when the consent expires
    amex-mcp-link --extend --name "..." --email "..."   # reconfirm consent for another 90 days
    amex-mcp-link --reset    # delete the stored tokens

What linking does:
  1. Prints a TrueLayer authorisation link. You open it, pick American Express and
     approve access on Amex's own site/app. Your Amex password never touches this code.
  2. Catches the redirect on http://localhost (or you paste the redirected URL).
  3. Exchanges the one-time code for tokens and writes them to the token store,
     REPLACING whatever was stored before.

Tokens are never printed unless you pass --print-refresh-token (which prints a secret).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .errors import (
    AmexMcpError,
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TokenStoreError,
)
from .models import clean_text
from .providers.truelayer import (
    SCOPES,
    TrueLayerConfig,
    TrueLayerProvider,
    _headers,
    _iso,
    _now,
    is_loopback_host,
)
from .token_store import build_token_store

DEFAULT_REDIRECT = "http://localhost:3000/callback"
# ASSUMPTION (unverified): these `providers` values make American Express selectable.
DEFAULT_PROVIDERS = {"live": "uk-ob-all uk-oauth-all", "sandbox": "uk-cs-mock uk-ob-all uk-oauth-all"}


def safe(value: Any, limit: int = 200) -> str:
    """Text from the network or the user, made safe to echo to a terminal."""
    return clean_text(value)[:limit]


def new_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for PKCE with the S256 method (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(
    config: TrueLayerConfig,
    redirect_uri: str,
    state: str,
    providers: str | None = None,
    code_challenge: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "providers": providers or DEFAULT_PROVIDERS[config.environment],
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{config.auth_base}/?{urlencode(params)}"


def extract_code(value: str, expected_state: str) -> str:
    """Accept a bare code, or the full redirected URL (or its query string).

    When a URL / query string is given, its `state` MUST be present and match this session.
    """
    value = value.strip()
    if not value:
        raise ConfigError("Nothing was pasted.")
    if "://" not in value and "=" not in value and "?" not in value:
        return value
    query = parse_qs(urlparse(value).query or value.lstrip("?"))
    state = query.get("state", [""])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        raise ConfigError(
            "The 'state' value in the redirect is missing or does not match this session. "
            "Start again and paste the URL from the link printed by THIS run."
        )
    if "error" in query:
        raise ConfigError(f"Authorisation was not completed: {safe(query['error'][0], 80)}.")
    if not query.get("code"):
        raise ConfigError("No 'code' parameter found in the redirect URL.")
    return query["code"][0]


def wait_for_redirect(redirect_uri: str, expected_state: str, timeout: float = 600.0) -> str:
    """Listen on the loopback redirect URI and return the authorisation code.

    A request with a missing or wrong `state` is answered 400 and ignored: the listener keeps
    waiting until the genuine redirect arrives or the timeout passes, so a stray or hostile
    local request cannot abort (or hijack) the flow.
    """
    target = urlparse(redirect_uri)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, text: str) -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != (target.path or "/"):
                self._reply(404, "Not found.")
                return
            query = parse_qs(parsed.query)
            state = query.get("state", [""])[0]
            if done.is_set() or not state or not secrets.compare_digest(state, expected_state):
                self._reply(400, "Ignored: this request does not belong to the current linking session.")
                return
            if "error" in query:
                result["error"] = safe(query["error"][0], 80)
                self._reply(400, f"Linking failed: {result['error']}. Return to the terminal.")
            elif query.get("code"):
                result["code"] = query["code"][0]
                self._reply(200, "Amex card linked. You can close this tab.")
            else:
                result["error"] = "no code in redirect"
                self._reply(400, "Linking failed: no code in redirect. Return to the terminal.")
            done.set()

        def log_message(self, *args: Any) -> None:  # the query string holds the code: never log it
            return

    try:
        server = HTTPServer(("127.0.0.1", target.port or 80), Handler)
    except OSError as exc:
        raise ConfigError(
            f"Could not listen on port {target.port or 80} ({type(exc).__name__}). Free the port, or use --paste."
        ) from None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise ConfigError("Timed out waiting for the redirect. Run the helper again.")
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in result:
        raise ConfigError(f"Authorisation was not completed: {result.get('error', 'unknown error')}.")
    return result["code"]


def _problem_text(resp: httpx.Response) -> str:
    """Summarise an RFC 7807 problem+json body (title, detail, per-field errors) for the terminal."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = [safe(body.get(k)) for k in ("title", "detail") if body.get(k)]
    errors = body.get("errors")
    if isinstance(errors, dict):
        for field, messages in list(errors.items())[:10]:
            text = "; ".join(str(m) for m in messages) if isinstance(messages, list) else str(messages)
            parts.append(f"{safe(field, 40)}: {safe(text)}")
    if not parts and body.get("error"):  # OAuth-style body
        parts = [safe(body.get(k)) for k in ("error", "error_description") if body.get(k)]
    if body.get("trace_id"):
        parts.append(f"trace_id={safe(body['trace_id'], 64)}")
    return " | ".join(parts)


async def extend_connection(provider: TrueLayerProvider, redirect_uri: str, name: str, email: str) -> dict[str, Any]:
    """POST {api}/connections/extend - reconfirm consent for another 90 days.

    Path per https://docs.truelayer.com/reference/extendconnection (no /data/v1 prefix).
    """
    tokens = provider._load_tokens()
    cfg = provider.config
    try:
        resp = await provider._client.post(
            f"{cfg.api_base}/connections/extend",
            headers=_headers(),
            json={
                "user_has_reconfirmed_consent": True,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "refresh_token": tokens["refresh_token"],
                "redirect_uri": redirect_uri,
                "user": {"name": name, "email": email},
            },
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not reach TrueLayer ({type(exc).__name__}).") from None
    if resp.status_code != 200:
        detail = _problem_text(resp) or "no details in the response"
        status = resp.status_code
        if status == 429:
            raise RateLimitError(f"TrueLayer is rate limiting the extend request. {detail}")
        if status == 401:
            raise ConsentExpiredError(
                f"TrueLayer rejected the refresh token when extending (HTTP 401: {detail}). "
                "Link the card again (run this helper with no flags)."
            )
        if status == 422:
            raise InvalidRequestError(
                f"TrueLayer cannot extend this connection (HTTP 422: {detail}). "
                "Link the card again instead (run this helper with no flags)."
            )
        if status == 400:
            raise InvalidRequestError(f"TrueLayer rejected the extend request (HTTP 400: {detail}).")
        raise ProviderError(f"TrueLayer did not extend the connection (HTTP {status}: {detail}).")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ProviderError("TrueLayer's extend response was not a JSON object.")
    if body.get("action_needed") == "no_action_needed" and body.get("access_token"):
        provider._persist({**tokens, "connected_at": _iso(_now())}, body)
        if provider._store_error:
            raise TokenStoreError(provider._store_error)
    return body


def reset_store(store: Any) -> None:
    """Delete the stored tokens (uses the store's optional clear(), else saves an empty document)."""
    if callable(getattr(store, "clear", None)):
        store.clear()
    else:
        store.save({})


async def _run(args: argparse.Namespace) -> int:
    store = build_token_store()
    if args.reset:
        reset_store(store)
        print("Stored tokens deleted. Run `amex-mcp-link` to link the card again.")
        return 0

    config = TrueLayerConfig.from_env()
    provider = TrueLayerProvider(config, store)
    redirect_uri = args.redirect_uri or os.environ.get("TRUELAYER_REDIRECT_URI") or DEFAULT_REDIRECT
    try:
        if args.status:
            status = await provider.connection_status()
            for key in (
                "connected",
                "consent_status",
                "consent_expires_at",
                "days_remaining",
                "needs_reauthorisation",
                "message",
            ):
                print(f"{key}: {safe(status.get(key), 400)}")
            return 0 if status.get("connected") else 1

        if args.extend:
            if not args.name or not args.email:
                raise ConfigError("--extend needs --name and --email (TrueLayer requires them for the consent record).")
            answer = input("Do you reconfirm that this app may keep reading your Amex card data for 90 days? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Not extended.")
                return 1
            body = await extend_connection(provider, redirect_uri, args.name, args.email)
            action = body.get("action_needed")
            if action == "no_action_needed":
                print("Connection extended. New tokens stored.")
            else:
                print(
                    f"TrueLayer needs a further step ({safe(action, 60)}). Open this link to finish:\n"
                    f"{safe(body.get('user_input_link'), 2000)}"
                )
                return 1
        else:
            state = secrets.token_urlsafe(24)
            verifier, challenge = (None, None) if args.no_pkce else new_pkce_pair()
            url = build_auth_url(
                config, redirect_uri, state, args.providers or os.environ.get("TRUELAYER_PROVIDERS"), challenge
            )
            print(
                f"\nEnvironment: {config.environment}\n\nOpen this link in your browser, choose American Express "
                f"and approve read access:\n\n{url}\n"
            )
            if args.open:
                webbrowser.open(url)
            target = urlparse(redirect_uri)
            if target.scheme == "http" and is_loopback_host(target.hostname) and not args.paste:
                print(f"Waiting for the redirect on {redirect_uri} (Ctrl+C to cancel) ...")
                code = await asyncio.to_thread(wait_for_redirect, redirect_uri, state)
            else:
                code = extract_code(input("Paste the full URL you were redirected to (or just the code): "), state)
            await provider.exchange_code(code, redirect_uri, code_verifier=verifier)
            print("Card linked. Tokens stored in the token store (replacing any previous ones).")

        status = await provider.connection_status()
        print(
            f"Consent expires: {safe(status.get('consent_expires_at'))} ({safe(status.get('days_remaining'))} days). "
            "Set yourself a reminder a week or two before."
        )
        if args.print_refresh_token:
            print("\nRefresh token (a SECRET - treat it like a password; anyone holding it can read the card data):")
            print((store.load() or {}).get("refresh_token"))
        return 0
    finally:
        await provider.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="amex-mcp-link", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--redirect-uri", help=f"must be allow-listed in the TrueLayer Console (default {DEFAULT_REDIRECT})"
    )
    parser.add_argument("--providers", help="TrueLayer 'providers' filter (default: all UK Open Banking providers)")
    parser.add_argument("--open", action="store_true", help="also open the link in your default browser")
    parser.add_argument("--paste", action="store_true", help="paste the redirect URL instead of listening on localhost")
    parser.add_argument("--no-pkce", action="store_true", help="do not send a PKCE code challenge with the auth link")
    parser.add_argument("--status", action="store_true", help="show consent status and exit")
    parser.add_argument("--extend", action="store_true", help="reconfirm consent via TrueLayer's /connections/extend")
    parser.add_argument("--name", help="your name (only for --extend)")
    parser.add_argument("--email", help="your email (only for --extend)")
    parser.add_argument("--reset", action="store_true", help="delete the stored tokens and exit")
    parser.add_argument(
        "--print-refresh-token",
        action="store_true",
        help="print the refresh token (a secret) so you can copy it into a secret store",
    )
    args = parser.parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except AmexMcpError as exc:
        print(f"\nERROR {safe(exc, 1000)}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
