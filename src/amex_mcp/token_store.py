"""Pluggable token persistence.

TrueLayer issues a short-lived access token (about 1 hour) and a long-lived refresh
token. A refresh (or a /connections/extend call) MAY return a new refresh token; if it
does, the new one must be persisted or the next process start will fail. The provider
therefore calls `save()` after every successful token response.

To plug in a secret store (AWS Secrets Manager, GCP Secret Manager, Azure Key Vault,
HashiCorp Vault, ...), implement the two-method `TokenStore` protocol below and point
AMEX_TOKEN_STORE at it as "package.module:ClassName". The class is constructed with no
arguments and should read its own configuration from the environment. Note that this
imports and runs the named code: only set it to a module you trust.

A store may also offer an optional `clear()` method (used by `amex-mcp-link --reset`);
without one, the helper saves an empty document instead.

Stored document (JSON-serialisable dict):
    {
      "access_token": str | None,
      "access_token_expires_at": float | None,   # unix seconds
      "refresh_token": str | None,
      "connected_at": str | None,                # ISO 8601, set when the card is linked
      "updated_at": str                          # ISO 8601
    }

The file store writes PLAINTEXT JSON. It is protected only by file permissions:
0600 on POSIX, and an ACL restricted to the current user on Windows.
"""

from __future__ import annotations

import contextlib
import getpass
import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .errors import ConfigError, TokenStoreError

log = logging.getLogger("amex_mcp.token_store")


@runtime_checkable
class TokenStore(Protocol):
    def load(self) -> dict[str, Any] | None:
        """Return the stored token document, or None if nothing is stored."""

    def save(self, tokens: dict[str, Any]) -> None:
        """Persist the token document, replacing any previous one (atomically if possible)."""


class MemoryTokenStore:
    """Non-persistent store. Useful for tests, and for read-only file systems when combined
    with AMEX_TRUELAYER_REFRESH_TOKEN (but a rotated refresh token is then lost on restart)."""

    def __init__(self, initial: dict[str, Any] | None = None):
        self._tokens = dict(initial) if initial else None

    def load(self) -> dict[str, Any] | None:
        return dict(self._tokens) if self._tokens else None

    def save(self, tokens: dict[str, Any]) -> None:
        self._tokens = dict(tokens)

    def clear(self) -> None:
        self._tokens = None


def restrict_to_current_user(path: str | os.PathLike[str]) -> bool:
    """Make `path` readable and writable by the current user only.

    POSIX: chmod 0600. Windows: drop inherited ACL entries and grant the current user full
    control (`icacls /inheritance:r /grant:r "<user>:(F)"`). Failure is logged, not fatal.
    """
    try:
        if sys.platform == "win32":
            user = getpass.getuser()
            domain = os.environ.get("USERDOMAIN")
            principal = f"{domain}\\{user}" if domain else user
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # Absolute path: do not let a planted icacls.exe earlier on PATH run instead.
            icacls = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "icacls.exe")
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [icacls, str(path), "/inheritance:r", "/grant:r", f"{principal}:(F)"],
                capture_output=True,
                timeout=15,
                creationflags=flags,
                check=False,
            )
            if result.returncode != 0:
                raise OSError(f"icacls exited with {result.returncode}")
        else:
            os.chmod(path, 0o600)
        return True
    except (OSError, subprocess.SubprocessError, KeyError) as exc:
        log.warning("could not restrict permissions on the token file (%s)", type(exc).__name__)
        return False


class FileTokenStore:
    """Default store: a JSON file written atomically and readable only by the current user."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path or os.environ.get("AMEX_TOKEN_FILE") or default_token_path())
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise TokenStoreError(
                f"Token file {self.path} exists but could not be read ({type(exc).__name__}). "
                "Fix its permissions, or clear it with `amex-mcp-link --reset` and link the card again."
            ) from None
        return data if isinstance(data, dict) else None

    def save(self, tokens: dict[str, Any]) -> None:
        with self._lock:
            tmp: str | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens-", suffix=".tmp")
                os.close(fd)  # mkstemp already created it 0600 on POSIX
                restrict_to_current_user(tmp)  # before any secret is written to it
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(tokens, fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                tmp = None
            except (OSError, TypeError, ValueError) as exc:
                raise TokenStoreError(
                    f"Could not write the token file {self.path} ({type(exc).__name__}). "
                    "Check that the folder exists and is writable."
                ) from None
            finally:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                raise TokenStoreError(f"Could not delete the token file {self.path} ({type(exc).__name__}).") from None


def default_token_path() -> Path:
    return Path.home() / ".amex-mcp" / "tokens.json"


def build_token_store(spec: str | None = None) -> TokenStore:
    """AMEX_TOKEN_STORE = "file" (default) | "memory" | "package.module:ClassName"."""
    spec = (spec if spec is not None else os.environ.get("AMEX_TOKEN_STORE", "file")).strip()
    if spec in ("", "file"):
        return FileTokenStore()
    if spec == "memory":
        return MemoryTokenStore()
    if ":" not in spec:
        raise ConfigError(
            f"AMEX_TOKEN_STORE={spec!r} is not valid. Use 'file', 'memory' or 'package.module:ClassName'."
        )
    module_name, _, class_name = spec.partition(":")
    try:
        cls = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"Could not load token store {spec!r}: {exc}") from None
    store = cls()
    if not isinstance(store, TokenStore):
        raise ConfigError(f"{spec!r} does not implement load() and save().")
    return store
