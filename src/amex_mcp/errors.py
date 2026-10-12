"""Error types. Every message is written to be actionable without a human reading logs.

Messages never contain secrets (tokens, client secrets, card numbers).
"""

from __future__ import annotations


class AmexMcpError(Exception):
    """Base class. `code` is a stable machine-readable string."""

    code = "error"

    def __init__(self, message: str):
        super().__init__(f"[{self.code}] {message}")
        self.message = message


class ConfigError(AmexMcpError):
    code = "config_error"


class NotConnectedError(AmexMcpError):
    """No token has ever been stored - the consent flow has not been run."""

    code = "not_connected"


class ConsentExpiredError(AmexMcpError):
    """The cardholder's Open Banking consent has lapsed or been revoked.

    Only a human can fix this: re-run the consent helper (amex-mcp-link).
    """

    code = "consent_expired"


class TokenExpiredError(AmexMcpError):
    """The access token was rejected and could not be refreshed automatically."""

    code = "token_expired"


class RateLimitError(AmexMcpError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderError(AmexMcpError):
    """Upstream (aggregator or Amex) failure that is not the caller's fault."""

    code = "provider_error"


class NotSupportedError(ProviderError):
    """The bank does not offer this endpoint through the aggregator (HTTP 501)."""

    code = "provider_error"


class TokenStoreError(AmexMcpError):
    """The token store could not be read or written."""

    code = "token_store_error"


class AccountNotFoundError(AmexMcpError):
    code = "account_not_found"


class InvalidRequestError(AmexMcpError):
    code = "invalid_request"


class ScaRequiredError(AmexMcpError):
    """The bank requires a fresh strong-customer-authentication for this request
    (typically: history older than ~90 days requested long after the card was linked)."""

    code = "sca_required"


class MalformedImportError(AmexMcpError):
    """A statement file could not be parsed."""

    code = "malformed_import_file"
