# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-12

First public version. The automated tests use mocks, a local fake TrueLayer server and synthetic
statement files; the author also runs it against their own live card through TrueLayer.

### Added

- Read-only MCP tools: `get_connection_status`, `list_accounts`, `get_balance`,
  `list_transactions`, `parse_statement`.
- TrueLayer Data API provider (cards endpoints) with automatic token refresh, a pluggable token
  store, and the `amex-mcp-link` helper (`--status`, `--extend`, `--reset`, `--paste`, `--no-pkce`,
  `--print-refresh-token`).
- Statement-file provider for Amex CSV, OFX and QFX exports.
- stdio and streamable HTTP transports, with a bearer-token gate for HTTP.
- `list_transactions` reports `pending_excluded`, `earliest_pending_date` and `pending_supported`,
  and enforces a maximum range (`AMEX_MAX_RANGE_DAYS`, default 400 days).
- Dates are Europe/London calendar dates.
