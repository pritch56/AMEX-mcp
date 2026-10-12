"""Unofficial read-only MCP server for American Express UK card data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("uk-bank-mcp-amex")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"

SOURCE = "amex"
USER_AGENT = f"amex-mcp/{__version__}"
