"""Link-helper pieces and the token store."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from amex_mcp import link
from amex_mcp.errors import (
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    ProviderError,
    TokenStoreError,
)
from amex_mcp.link import (
    build_auth_url,
    extend_connection,
    extract_code,
    new_pkce_pair,
    reset_store,
    wait_for_redirect,
)
from amex_mcp.providers.truelayer import TrueLayerConfig, TrueLayerProvider
from amex_mcp.token_store import FileTokenStore, MemoryTokenStore, TokenStore, build_token_store

from .conftest import ME, run
from .test_server import _free_port

AUTH = "https://auth.truelayer.com"
API = "https://api.truelayer.com"


def test_auth_url_requests_read_only_card_scopes_and_never_contains_the_secret():
    cfg = TrueLayerConfig(client_id="my-client", client_secret="very-secret")
    url = build_auth_url(cfg, "http://localhost:3000/callback", "state-123")
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    assert parsed.netloc == "auth.truelayer.com"
    assert q["response_type"] == ["code"] and q["client_id"] == ["my-client"] and q["state"] == ["state-123"]
    assert q["redirect_uri"] == ["http://localhost:3000/callback"]
    assert set(q["scope"][0].split()) == {"info", "cards", "balance", "transactions", "offline_access"}
    assert "very-secret" not in url and "code_challenge" not in q
    sandbox = build_auth_url(
        TrueLayerConfig("sandbox-c", "s", environment="sandbox"), "http://localhost:3000/callback", "s"
    )
    assert urlparse(sandbox).netloc == "auth.truelayer-sandbox.com" and "uk-cs-mock" in sandbox


def test_pkce_challenge_is_s256_of_the_verifier_and_goes_in_the_auth_url():
    verifier, challenge = new_pkce_pair()
    assert 43 <= len(verifier) <= 128 and verifier != new_pkce_pair()[0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    cfg = TrueLayerConfig(client_id="my-client", client_secret="very-secret")
    q = parse_qs(urlparse(build_auth_url(cfg, "http://localhost:3000/callback", "st", None, challenge)).query)
    assert q["code_challenge"] == [challenge] and q["code_challenge_method"] == ["S256"]
    assert verifier not in build_auth_url(cfg, "http://localhost:3000/callback", "st", None, challenge)


def test_extract_code_from_url_or_bare_value():
    assert extract_code("abc123", "st") == "abc123"
    assert extract_code("  abc123\n", "st") == "abc123"
    assert extract_code("http://localhost:3000/callback?code=abc123&state=st", "st") == "abc123"
    assert extract_code("code=abc123&state=st", "st") == "abc123"
    with pytest.raises(ConfigError, match="state"):
        extract_code("http://localhost:3000/callback?code=abc123&state=other", "st")
    with pytest.raises(ConfigError, match="access_denied"):
        extract_code("http://localhost:3000/callback?error=access_denied&state=st", "st")
    with pytest.raises(ConfigError, match="No 'code'"):
        extract_code("http://localhost:3000/callback?state=st", "st")


@pytest.mark.parametrize(
    "pasted",
    [
        "http://localhost:3000/callback?code=attacker-code",  # no state at all
        "http://localhost:3000/callback?code=attacker-code&state=",
        "code=attacker-code",
        "?code=attacker-code",
        "http://localhost:3000/callback?error=access_denied",
    ],
)
def test_pasted_url_without_matching_state_is_refused(pasted):
    with pytest.raises(ConfigError, match="missing or does not match"):
        extract_code(pasted, "st")


def test_pasted_error_is_stripped_of_control_characters():
    with pytest.raises(ConfigError) as info:
        extract_code("http://localhost/cb?state=st&error=bad%1b%5B2Jthing%07" + "x" * 500, "st")
    assert "\x1b" not in str(info.value) and "\x07" not in str(info.value) and len(str(info.value)) < 200


def _get(url: str) -> int:
    for _ in range(50):
        try:
            return urllib.request.urlopen(url, timeout=2).status
        except urllib.error.HTTPError as exc:
            return exc.code
        except OSError:
            time.sleep(0.1)
    raise AssertionError("listener never came up")


def test_local_redirect_listener_returns_the_code():
    port = _free_port()
    statuses: list[int] = []
    threading.Thread(
        target=lambda: statuses.append(_get(f"http://127.0.0.1:{port}/callback?code=the-code&state=good")),
        daemon=True,
    ).start()
    assert wait_for_redirect(f"http://localhost:{port}/callback", "good", timeout=15) == "the-code"


def test_wrong_or_missing_state_gets_400_and_the_listener_keeps_waiting():
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    statuses: list[int] = []

    def traffic() -> None:
        statuses.append(_get(f"{base}/callback?code=evil&state=wrong"))
        statuses.append(_get(f"{base}/callback?code=evil"))
        statuses.append(_get(f"{base}/callback?error=access_denied"))  # an error without state must not abort
        statuses.append(_get(f"{base}/favicon.ico"))
        statuses.append(_get(f"{base}/callback?code=the-real-code&state=good"))

    threading.Thread(target=traffic, daemon=True).start()
    assert wait_for_redirect(f"http://localhost:{port}/callback", "good", timeout=20) == "the-real-code"
    assert statuses == [400, 400, 400, 404, 200]


def test_listener_times_out_if_only_wrong_state_requests_arrive():
    port = _free_port()
    threading.Thread(target=_get, args=(f"http://127.0.0.1:{port}/callback?code=x&state=evil",), daemon=True).start()
    with pytest.raises(ConfigError, match="Timed out"):
        wait_for_redirect(f"http://localhost:{port}/callback", "good", timeout=1.5)


def test_listener_reports_a_genuine_denial_with_sanitised_text():
    port = _free_port()
    threading.Thread(
        target=_get, args=(f"http://127.0.0.1:{port}/callback?error=access%1b_denied&state=good",), daemon=True
    ).start()
    with pytest.raises(ConfigError, match="access _denied") as info:
        wait_for_redirect(f"http://localhost:{port}/callback", "good", timeout=15)
    assert "\x1b" not in str(info.value)


# ------------------------------------------------------------------ --extend


def _provider(store=None) -> TrueLayerProvider:
    cfg = TrueLayerConfig(client_id="test-client", client_secret="s3cret-value")
    return TrueLayerProvider(cfg, store or MemoryTokenStore({"refresh_token": "refresh-1"}), backoff_seconds=0)


@respx.mock
def test_extend_posts_to_the_documented_path_and_stores_new_tokens():
    wrong = respx.post(f"{API}/data/v1/connections/extend").mock(return_value=httpx.Response(404))
    route = respx.post(f"{API}/connections/extend").mock(
        return_value=httpx.Response(
            200,
            json={
                "action_needed": "no_action_needed",
                "access_token": "a-new",
                "expires_in": 3600,
                "refresh_token": "r-new",
                "token_type": "Bearer",
            },
        )
    )
    store = MemoryTokenStore({"refresh_token": "refresh-1"})
    body = run(extend_connection(_provider(store), "http://localhost:3000/callback", "A N Other", "a@example.com"))
    assert route.called and not wrong.called
    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "user_has_reconfirmed_consent": True,
        "client_id": "test-client",
        "client_secret": "s3cret-value",
        "refresh_token": "refresh-1",
        "redirect_uri": "http://localhost:3000/callback",
        "user": {"name": "A N Other", "email": "a@example.com"},
    }
    assert body["action_needed"] == "no_action_needed"
    assert store.load()["refresh_token"] == "r-new" and "connected_at" in store.load()


@respx.mock
def test_extend_that_needs_user_action_stores_nothing():
    respx.post(f"{API}/connections/extend").mock(
        return_value=httpx.Response(
            200, json={"action_needed": "authentication_needed", "user_input_link": "https://auth.example/x"}
        )
    )
    store = MemoryTokenStore({"refresh_token": "refresh-1"})
    body = run(extend_connection(_provider(store), "http://localhost:3000/callback", "n", "e@example.com"))
    assert body["user_input_link"] == "https://auth.example/x" and store.load() == {"refresh_token": "refresh-1"}


@pytest.mark.parametrize(
    "status, problem, exc, expected",
    [
        (
            401,
            {"title": "Unauthorized", "detail": "Refresh token missing or invalid.", "status": 401, "trace_id": "t-1"},
            ConsentExpiredError,
            "Refresh token missing or invalid",
        ),
        (
            422,
            {"title": "No way to extend this connection", "detail": "This connection cannot be extended"},
            InvalidRequestError,
            "No way to extend this connection | This connection cannot be extended",
        ),
        (
            400,
            {
                "title": "One or more validation errors occurred.",
                "errors": {"User.Email": ["The Email field is not a valid e-mail address."]},
            },
            InvalidRequestError,
            "User.Email: The Email field is not a valid e-mail address.",
        ),
        (500, {"title": "Boom\x1b[2J"}, ProviderError, "HTTP 500: Boom"),
        (403, None, ProviderError, "HTTP 403: no details"),
    ],
)
@respx.mock
def test_extend_failures_report_the_problem_json_body(status, problem, exc, expected):
    response = (
        httpx.Response(status, text="<html>nope</html>")
        if problem is None
        else httpx.Response(status, json=problem, headers={"Content-Type": "application/problem+json"})
    )
    respx.post(f"{API}/connections/extend").mock(return_value=response)
    with pytest.raises(exc) as info:
        run(extend_connection(_provider(), "http://localhost:3000/callback", "n", "e@example.com"))
    text = str(info.value)
    assert expected in text and "\x1b" not in text
    assert "has to be enabled for your client" not in text
    assert "s3cret-value" not in text and "refresh-1" not in text


# -------------------------------------------------------- re-linking and --reset


@respx.mock
def test_relinking_overwrites_the_store_and_the_env_seed_stays_ignored(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save({"refresh_token": "old-refresh", "access_token": "old", "access_token_expires_at": 0})
    token_route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600, "refresh_token": "relinked"})
    )
    cfg = TrueLayerConfig(client_id="c", client_secret="s", seed_refresh_token="stale-env-seed")
    p = TrueLayerProvider(cfg, store)
    run(p.exchange_code("the-code", "http://localhost:3000/callback", code_verifier="the-verifier"))
    sent = json.loads(token_route.calls[0].request.content)
    assert sent["code_verifier"] == "the-verifier" and sent["grant_type"] == "authorization_code"
    saved = store.load()
    assert saved["refresh_token"] == "relinked" and "old" not in json.dumps(saved)
    assert TrueLayerProvider(cfg, store)._load_tokens()["refresh_token"] == "relinked"


def test_reset_clears_the_store_so_the_seed_or_a_fresh_link_applies(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save({"refresh_token": "old"})
    reset_store(store)
    assert store.load() is None and not (tmp_path / "tokens.json").exists()
    reset_store(store)  # idempotent

    class NoClear:
        def __init__(self):
            self.doc = {"refresh_token": "old"}

        def load(self):
            return self.doc

        def save(self, tokens):
            self.doc = tokens

    custom = NoClear()
    reset_store(custom)
    cfg = TrueLayerConfig(client_id="c", client_secret="s", seed_refresh_token="seed")
    assert TrueLayerProvider(cfg, custom)._load_tokens() == {"refresh_token": "seed"}


def test_reset_flag_on_the_command_line(tmp_path, monkeypatch, capsys):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save({"refresh_token": "old"})
    monkeypatch.setenv("AMEX_TOKEN_FILE", str(path))
    monkeypatch.setenv("AMEX_TOKEN_STORE", "file")
    monkeypatch.delenv("TRUELAYER_CLIENT_ID", raising=False)  # --reset must not need credentials
    with pytest.raises(SystemExit) as info:
        link.main(["--reset"])
    assert info.value.code == 0 and not path.exists()
    assert "deleted" in capsys.readouterr().out


@respx.mock
def test_full_link_flow_uses_pkce_and_checks_state(tmp_path, monkeypatch, capsys):
    """Drive the helper end to end in --paste mode against mocked TrueLayer endpoints."""
    path = tmp_path / "tokens.json"
    for var in ("TRUELAYER_ENV", "TRUELAYER_AUTH_BASE", "TRUELAYER_API_BASE", "TRUELAYER_PROVIDERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AMEX_TOKEN_FILE", str(path))
    monkeypatch.setenv("AMEX_TOKEN_STORE", "file")
    monkeypatch.setenv("TRUELAYER_CLIENT_ID", "test-client")
    monkeypatch.setenv("TRUELAYER_CLIENT_SECRET", "s3cret-value")
    monkeypatch.setenv("TRUELAYER_REDIRECT_URI", "https://example.test/callback")
    token_route = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600, "refresh_token": "r-linked"})
    )
    respx.get(f"{API}/data/v1/me").mock(return_value=httpx.Response(200, json={"results": [ME]}))

    def fake_input(prompt: str) -> str:
        printed = capsys.readouterr().out
        url = next(line for line in printed.splitlines() if line.startswith("https://auth.truelayer.com/"))
        fake_input.query = parse_qs(urlparse(url).query)
        return f"https://example.test/callback?code=the-code&state={fake_input.query['state'][0]}"

    monkeypatch.setattr("builtins.input", fake_input)
    with pytest.raises(SystemExit) as info:
        link.main([])
    assert info.value.code == 0
    sent = json.loads(token_route.calls[0].request.content)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert fake_input.query["code_challenge"] == [challenge] and fake_input.query["code_challenge_method"] == ["S256"]
    assert json.loads(path.read_text())["refresh_token"] == "r-linked"
    out = capsys.readouterr().out
    assert "r-linked" not in out and "s3cret-value" not in out  # nothing secret is printed by default

    # a pasted URL from another session (wrong state) is refused and nothing is exchanged
    monkeypatch.setattr("builtins.input", lambda prompt: "https://example.test/callback?code=evil&state=nope")
    with pytest.raises(SystemExit) as info:
        link.main(["--no-pkce"])
    assert info.value.code == 2 and token_route.call_count == 1
    assert "does not match" in capsys.readouterr().err


# ---------------------------------------------------------------- token store


def test_file_store_round_trip_is_atomic_and_leaves_no_temp_files(tmp_path):
    store = FileTokenStore(tmp_path / "deep" / "tokens.json")
    assert store.load() is None
    store.save({"refresh_token": "r1"})
    store.save({"refresh_token": "r2", "access_token": "a"})
    assert store.load() == {"refresh_token": "r2", "access_token": "a"}
    assert [p.name for p in (tmp_path / "deep").iterdir()] == ["tokens.json"]


def test_file_store_fsyncs_before_replacing(tmp_path, monkeypatch):
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    FileTokenStore(tmp_path / "tokens.json").save({"refresh_token": "r"})
    assert events == ["fsync", "replace"]


def test_file_store_corrupt_file_is_a_token_store_error(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json")
    with pytest.raises(TokenStoreError, match=r"\[token_store_error\].*could not be read"):
        FileTokenStore(path).load()


def test_file_store_write_failure_is_a_token_store_error_not_a_bare_oserror(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    with pytest.raises(TokenStoreError, match=r"\[token_store_error\] Could not write the token file"):
        FileTokenStore(blocker / "tokens.json").save({"refresh_token": "secret-r"})

    def boom(a, b):
        raise PermissionError("locked: secret-r")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(TokenStoreError) as info:
        FileTokenStore(tmp_path / "tokens.json").save({"refresh_token": "secret-r"})
    assert "secret-r" not in str(info.value)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tokens-")] == []  # temp file cleaned up


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_token_file_is_0600_on_posix(tmp_path):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save({"refresh_token": "r"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_token_file_acl_is_restricted_to_the_current_user_on_windows(tmp_path):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save({"refresh_token": "r"})
    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    entries = [line.replace(str(path), "").strip() for line in listing.splitlines() if ":(" in line]
    assert len(entries) == 1, listing  # exactly one ACE ...
    assert "(I)" not in entries[0] and entries[0].endswith(":(F)")  # ... explicit, not inherited, full control
    assert os.environ.get("USERNAME", "").lower() in entries[0].lower()


def test_acl_failure_is_logged_not_fatal(tmp_path, monkeypatch, caplog):
    import amex_mcp.token_store as ts

    def fail(*args, **kwargs):
        raise OSError("no icacls here")

    monkeypatch.setattr(ts.subprocess, "run", fail)
    monkeypatch.setattr(ts.os, "chmod", fail)
    with caplog.at_level("WARNING"):
        FileTokenStore(tmp_path / "tokens.json").save({"refresh_token": "r"})
    assert json.loads((tmp_path / "tokens.json").read_text()) == {"refresh_token": "r"}
    assert "could not restrict permissions" in caplog.text


class CustomStore:
    def load(self):
        return {"refresh_token": "from-custom"}

    def save(self, tokens):
        self.saved = tokens


def test_token_store_is_pluggable(monkeypatch, tmp_path):
    monkeypatch.setenv("AMEX_TOKEN_FILE", str(tmp_path / "t.json"))
    assert isinstance(build_token_store("file"), FileTokenStore)
    assert isinstance(build_token_store("memory"), MemoryTokenStore)
    custom = build_token_store("tests.test_link_and_store:CustomStore")
    assert isinstance(custom, TokenStore) and custom.load() == {"refresh_token": "from-custom"}
    with pytest.raises(ConfigError):
        build_token_store("nonsense")
    with pytest.raises(ConfigError, match="Could not load"):
        build_token_store("no.such.module:Thing")


def test_default_token_file_is_amex_specific(monkeypatch):
    from amex_mcp.token_store import default_token_path

    monkeypatch.delenv("AMEX_TOKEN_FILE", raising=False)
    assert default_token_path().parent.name == ".amex-mcp"
    assert FileTokenStore().path == default_token_path()


def test_saved_tokens_are_plain_json(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save({"refresh_token": "r", "access_token_expires_at": 123.0})
    assert json.loads((tmp_path / "tokens.json").read_text())["access_token_expires_at"] == 123.0
