# amex-mcp

An **unofficial**, **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server for
American Express UK card data: cards, balance, transactions and consent status.

- Two data sources behind one interface:
  1. **TrueLayer Data API** (UK Open Banking through a regulated aggregator) - automatic.
  2. **Statement-file import** (Amex CSV / OFX / QFX exports) - you export a file, no third party.
- Two transports: **stdio** and **streamable HTTP**.
- No tool can move money or change the account. There is no screen scraping, and the server never
  sees your Amex username or password.

> **Status: alpha, never run against the live API.** Everything here has been tested only against
> mocked HTTP, a local fake TrueLayer server and synthetic statement files. It has not been run
> against TrueLayer (sandbox or live) or against real American Express data. Specific things that are
> assumed rather than verified:
>
> - whether TrueLayer rotates refresh tokens on refresh (the server copes with both);
> - the sign of the card balance field `current` (assumed positive = money owed) and the meaning of
>   `available` (assumed available credit);
> - whether TrueLayer's `account_id` for a card changes when the card is re-linked;
> - whether pending transactions have stable ids (assumed not);
> - whether a transaction's `timestamp` is the purchase date or the posting date;
> - the effect of not sending the `X-PSU-IP` header (the server never sends it, so every request
>   counts as unattended and the bank's unattended rate limits apply);
> - whether an individual can get Live access to the TrueLayer Data API, and at what price;
> - the `providers` value used in the consent link (`uk-ob-all uk-oauth-all`);
> - the `code_challenge` / `code_challenge_method` parameter names on the consent link (PKCE);
> - whether `from` / `to` on the transactions endpoint are inclusive calendar days;
> - the exact column headers of today's Amex UK CSV export.
>
> See [Unverified assumptions](#unverified-assumptions) for what each one affects.

## Disclaimer

This project is **not affiliated with, endorsed by, or supported by American Express or TrueLayer**.
"American Express", "Amex" and "TrueLayer" are trademarks of their respective owners and are used
here only to describe what the software talks to.

- The software is provided **as is, without warranty of any kind**. Check its output before you rely
  on it.
- You must **register your own TrueLayer API client** and comply with TrueLayer's terms and with
  your card agreement. Whether TrueLayer offers Live access to individuals is **unconfirmed**: read
  the TrueLayer Console terms yourself before relying on this route.
- It is for **your own accounts only**. Do not host it for other people or connect accounts that are
  not yours.
- Nothing it returns is financial advice.

## Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Route 1: TrueLayer](#route-1-truelayer)
- [Route 2: statement files](#route-2-statement-files)
- [Running the server](#running-the-server)
- [MCP client configuration](#mcp-client-configuration)
- [Tools](#tools)
- [Pending transactions: always fetch overlapping windows](#pending-transactions-always-fetch-overlapping-windows)
- [Errors](#errors)
- [Configuration](#configuration)
- [Tokens and the token store](#tokens-and-the-token-store)
- [Limitations](#limitations)
- [Security](#security)
- [Unverified assumptions](#unverified-assumptions)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## How it works

American Express has no API for individual cardholders. Card data is reachable either through a
regulated Open Banking aggregator, or by exporting statement files from the Amex website. This server
supports one of each; `docs/research.md` has dated notes on the alternatives that were looked at.

| | TrueLayer route | Statement-file route |
|---|---|---|
| Setup | TrueLayer Console account, an application, a one-off consent | none |
| Ongoing effort | re-consent about every 90 days | export a file whenever you want fresh data |
| Balance | yes | only from OFX/QFX exports |
| Pending transactions | if the bank offers them | no |
| Third party involved | TrueLayer | none |

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

PowerShell:

```powershell
git clone https://github.com/pritch56/uk-bank-mcp.git
cd uk-bank-mcp/amex
uv sync
uv run pytest
```

bash:

```bash
git clone https://github.com/pritch56/uk-bank-mcp.git
cd uk-bank-mcp/amex
uv sync
uv run pytest
```

The tests need no network access and no credentials.

The quickest way to see it work is the statement-file route with the bundled synthetic fixture.

PowerShell:

```powershell
$env:AMEX_IMPORT_DIR = "$PWD\tests\fixtures"
uv run amex-mcp --transport http --port 8000
```

bash:

```bash
export AMEX_IMPORT_DIR="$PWD/tests/fixtures"
uv run amex-mcp --transport http --port 8000
```

The server is then at `http://127.0.0.1:8000/mcp`. For real use, pick a route below.

Without uv, a plain virtual environment works too.

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m amex_mcp --help
```

bash:

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m amex_mcp --help
```

## Route 1: TrueLayer

1. Create an account at <https://console.truelayer.com>, read and accept their terms yourself, and
   create an application.
2. Sandbox applications only return mock-bank data. Real card data needs a **Live** application;
   whether TrueLayer grants that to an individual is unconfirmed (see the status banner).
3. In the application's settings enable the Data API with the scopes `info`, `cards`, `balance`,
   `transactions` and `offline_access`, and add the redirect URI `http://localhost:3000/callback`.
4. Put the client id and secret in environment variables (never in a file you commit).

   PowerShell:

   ```powershell
   $env:TRUELAYER_CLIENT_ID = "<client id>"
   $env:TRUELAYER_CLIENT_SECRET = "<client secret>"
   ```

   bash:

   ```bash
   export TRUELAYER_CLIENT_ID="<client id>"
   export TRUELAYER_CLIENT_SECRET="<client secret>"
   ```

5. Link the card once, on your own machine.

   PowerShell:

   ```powershell
   uv run amex-mcp-link --open
   ```

   bash:

   ```bash
   uv run amex-mcp-link --open
   ```

   The helper prints a TrueLayer link. Open it, choose American Express, sign in on Amex's own page
   or app, and approve. The helper catches the redirect on `localhost`, exchanges the code and
   writes the tokens to the token store (`~/.amex-mcp/tokens.json` by default), replacing anything
   stored before. It then prints when the consent expires.

   - The link uses a random `state` (checked on return) and PKCE (`S256`). If TrueLayer rejects the
     link because of the PKCE parameters, run it again with `--no-pkce`.
   - If the redirect cannot reach your machine, use `--paste` and paste the full redirected URL.
   - To try the flow without real data, use a sandbox client id with `TRUELAYER_ENV=sandbox` and
     pick the mock bank.

6. Note the expiry date: the consent lasts about 90 days (see [Limitations](#limitations)).

Other helper commands:

PowerShell:

```powershell
uv run amex-mcp-link --status
uv run amex-mcp-link --extend --name "Your Name" --email "you@example.com"
uv run amex-mcp-link --reset
```

bash:

```bash
uv run amex-mcp-link --status
uv run amex-mcp-link --extend --name "Your Name" --email "you@example.com"
uv run amex-mcp-link --reset
```

- `--status` shows whether the connection works and when the consent expires.
- `--extend` asks TrueLayer to extend the connection for another 90 days without visiting Amex
  (`POST https://api.truelayer.com/connections/extend`). TrueLayer may answer that a further step is
  needed, or that the connection cannot be extended; the helper prints TrueLayer's own explanation.
  If it cannot be extended, link the card again.
- `--reset` deletes the stored tokens.
- `--print-refresh-token` (with a normal link run) prints the refresh token. **That is a secret**:
  anyone who has it, together with your client secret, can read your card data.

## Route 2: statement files

1. Sign in to the Amex UK website yourself, open *Statements & Activity*, and download the activity
   as **CSV** (include the additional transaction details if offered). OFX/QFX exports also work
   where your account offers them, and they carry a balance.
2. Put the file in a folder and point `AMEX_IMPORT_DIR` at it. With several cards, use one
   sub-folder per card; the folder name becomes the account id.

   ```text
   statements/
     activity-2026-10.csv        -> account "amex-import"
     gold/2026-10.qfx            -> account "gold"
   ```

3. Add newer exports whenever you like. Overlapping date ranges are fine: rows are de-duplicated on
   their id.

PowerShell:

```powershell
$env:AMEX_IMPORT_DIR = "C:\path\to\statements"
uv run amex-mcp
```

bash:

```bash
export AMEX_IMPORT_DIR="$HOME/statements"
uv run amex-mcp
```

Notes:

- Amex CSV exports show charges as positive numbers; the server flips them. If a file shows payments
  to the card as money out, parsing stops and tells you to set `AMEX_CSV_CHARGES_POSITIVE=false`.
- Dates are read day-first (UK). Set `AMEX_CSV_DATE_ORDER=mdy` for a US-format export.
- Files may be UTF-8 (with or without BOM), UTF-16 with a BOM, Windows-1252 or Latin-1. Files over
  10 MiB are rejected.
- Ids differ between formats (CSV uses Amex's `Reference`, OFX uses `FITID`), so do not mix CSV and
  OFX exports of the same dates in one folder.
- The `parse_statement` tool parses export text passed directly as an argument, for when the file
  lives somewhere the server cannot read.

## Running the server

PowerShell:

```powershell
# stdio (the default; for local MCP clients)
uv run amex-mcp

# streamable HTTP on loopback: http://127.0.0.1:8000/mcp
$env:AMEX_MCP_AUTH_TOKEN = "<long random string>"
uv run amex-mcp --transport http --port 8000

# streamable HTTP on all interfaces (a bearer token is mandatory)
$env:AMEX_MCP_AUTH_TOKEN = "<long random string>"
uv run amex-mcp --transport http --host 0.0.0.0 --port 8000
```

bash:

```bash
# stdio (the default; for local MCP clients)
uv run amex-mcp

# streamable HTTP on loopback: http://127.0.0.1:8000/mcp
export AMEX_MCP_AUTH_TOKEN="<long random string>"
uv run amex-mcp --transport http --port 8000

# streamable HTTP on all interfaces (a bearer token is mandatory)
export AMEX_MCP_AUTH_TOKEN="<long random string>"
uv run amex-mcp --transport http --host 0.0.0.0 --port 8000
```

`python -m amex_mcp` is equivalent to the `amex-mcp` command. The HTTP transport is stateless and
answers with JSON. It speaks **plain HTTP**: put it behind something that terminates TLS (see
[Security](#security)). HTTP clients send `Authorization: Bearer <AMEX_MCP_AUTH_TOKEN>`.

## MCP client configuration

Both examples take secrets from your environment through `${VAR}` placeholders. Do not paste secrets
into these files. Replace `/path/to/uk-bank-mcp/amex` with the folder you cloned into.

**Claude Code** (`.mcp.json` in your project, or `claude mcp add`):

```json
{
  "mcpServers": {
    "amex": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/uk-bank-mcp/amex", "amex-mcp"],
      "env": {
        "TRUELAYER_CLIENT_ID": "${TRUELAYER_CLIENT_ID}",
        "TRUELAYER_CLIENT_SECRET": "${TRUELAYER_CLIENT_SECRET}"
      }
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`): the same block works. Claude Desktop passes the
variables of the environment it was started from; if `${VAR}` expansion is not available in your
version, start Claude Desktop from a shell that has the variables set and omit the `env` block
rather than writing the secret into the file.

```json
{
  "mcpServers": {
    "amex": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/uk-bank-mcp/amex", "amex-mcp"],
      "env": {
        "TRUELAYER_CLIENT_ID": "${TRUELAYER_CLIENT_ID}",
        "TRUELAYER_CLIENT_SECRET": "${TRUELAYER_CLIENT_SECRET}"
      }
    }
  }
}
```

For the statement-file route, replace the `env` block with
`"env": { "AMEX_IMPORT_DIR": "/path/to/statements" }`.

A remote client uses the URL `https://<your-host>/mcp` with the header
`Authorization: Bearer ${AMEX_MCP_AUTH_TOKEN}`.

## Tools

All tools are read-only.

| Tool | Returns |
|---|---|
| `get_connection_status()` | `{connected, consent_expires_at, days_remaining, warning, ...}`. Never fails for provider, authentication, configuration or rate-limit problems: it returns `connected: false` with the bracketed error in `warning`. Call it first. |
| `list_accounts()` | `{accounts: [{id, account_id, name, type, card_network, last4, currency, provider, source}]}`. `account_id` duplicates `id`. |
| `get_balance(account_id)` | `{balance_minor, currency, available_credit_minor, credit_limit_minor, last_statement_balance_minor, last_statement_date, payment_due_minor, payment_due_date, as_of, notes}` |
| `list_transactions(account_id, since?, before?, include_pending=false)` | `{since, before, count, pending_supported, pending_excluded, earliest_pending_date, transactions: [...]}` |
| `parse_statement(content, filename?, account_id?)` | Normalises the text of a CSV/OFX/QFX export. Stores nothing and makes no network call. |

### `get_connection_status`

| Field | Meaning |
|---|---|
| `connected` | `true` when the data source can be used right now (`false` once the consent has expired) |
| `consent_expires_at` | ISO 8601 UTC timestamp ending in `Z`, or `null` (always `null` for statement files) |
| `days_remaining` | whole days until the consent expires, or `null` |
| `warning` | `null`, or a message when action is needed: under 14 days of consent left, consent expired, not linked, misconfigured, rate limited, statement files stale, or refreshed tokens that could not be saved |

Extra fields (`provider`, `message`, `needs_reauthorisation`, `error`, `checked_at`, and
provider-specific ones) are informational.

### `get_balance`

`balance_minor` is a signed integer in minor units (pence). **Negative means money is owed on the
card.** `currency` is the ISO 4217 code. Fields the source does not have are `null`; CSV exports
have no balance at all.

### `list_transactions`

- The window is `since <= date < before`. Both are `YYYY-MM-DD` calendar dates in
  **Europe/London**. A full ISO 8601 datetime is accepted and converted to its London date.
- Defaults: `before` = tomorrow (London), `since` = `before` minus 30 days.
- `since >= before` is rejected with `[invalid_request]`.
- A range longer than `AMEX_MAX_RANGE_DAYS` (default 400) is rejected with `[invalid_request]`
  before any request is sent.
- The effective `since` and `before` are echoed back as `YYYY-MM-DD`.

Each transaction:

```json
{
  "id": "amex_b26b957a4d9f688558359cb6",
  "source": "amex",
  "account_id": "f7b0...",
  "date": "2026-10-04",
  "amount_minor": -4321,
  "currency": "GBP",
  "description": "EXAMPLE DINER NEW YORK",
  "merchant": null,
  "category": "Entertainment-Restaurants",
  "pending": false,
  "notes": "foreign_amount=54.00 US DOLLARS; fx_rate=1.2871; fx_commission=1.25"
}
```

- **Sign:** `amount_minor` is signed minor units. **Negative = a card charge (money out). Positive =
  a refund or a payment to the card.**
- **`date`** is `YYYY-MM-DD` in Europe/London. A provider timestamp that carries a UTC offset is
  converted to London time first.
- **`notes`** is `null` when there is nothing to say, never an empty string. Foreign-currency
  details, when the source provides them, go here; the amount itself is the billed amount.
- **Ids are stable across calls for booked transactions.**
  - TrueLayer: a hash of `normalised_provider_transaction_id` (or `provider_transaction_id`). The
    id does not depend on TrueLayer's `account_id`. TrueLayer's own `transaction_id` is ignored
    because it is documented as changing between requests.
  - CSV: a hash of Amex's `Reference`. OFX/QFX: a hash of `FITID`.
  - When the source has no id (and for all pending items), the id is a hash of account, date,
    amount, currency, description and an occurrence number, so two identical purchases on one day
    stay distinct.
  - If two rows share a provider id but differ in date, amount or description, **both are kept**:
    the later one gets an id derived from its content, and a warning is logged. The same applies
    when a second card on the same consent reuses a provider id for a different transaction (in
    that case which card keeps the plain id depends on which card was fetched first in the running
    process).

## Pending transactions: always fetch overlapping windows

**This matters if you copy transactions somewhere else.**

Pending transactions are left out unless you pass `include_pending=true`. A transaction that is
pending when you fetch, and settles later, appears afterwards **under its original date**. If your
next fetch starts where the previous one ended, that transaction falls before your new window and
you never see it.

So:

1. **Always re-fetch an overlapping window.** Go back at least to the `earliest_pending_date` of
   your previous result; going back **14 days** is the recommended default.
2. **De-duplicate on `id`.** Booked ids are stable, so re-fetching the same transaction is harmless.

Every `list_transactions` result tells you what was left out:

| Field | Meaning |
|---|---|
| `pending_excluded` | number of pending items inside the window that were left out (`0` when `include_pending` is true) |
| `earliest_pending_date` | the earliest date of any pending item inside the window (`YYYY-MM-DD`), included or not; `null` if there are none |
| `pending_supported` | `false` when the source cannot report pending items at all: statement files, or a bank that does not offer the pending endpoint. The two fields above then say nothing, so use the 14-day overlap. |

If you do include pending items, remember that a pending item's id and amount can change when it is
booked: replace pending rows rather than accumulating them.

## Errors

Tool errors carry a stable code in square brackets. The MCP SDK prepends
`Error executing tool <name>: ` to the message, so **match the bracketed code as a substring**, not
as a prefix.

| Code | Meaning | What to do |
|---|---|---|
| `[consent_expired]` | The Open Banking consent has expired or been revoked | Link the card again (`amex-mcp-link`) |
| `[token_expired]` | The access token was rejected even after a refresh | Link the card again |
| `[not_connected]` | No credentials: the card was never linked | Run `amex-mcp-link` |
| `[rate_limited]` | The provider is rate limiting | Do not retry now; wait hours. The server never retries a 429 |
| `[sca_required]` | The bank wants a fresh authentication (typically history older than about 90 days) | Ask for a more recent range, or re-link and fetch the history straight away |
| `[invalid_request]` | Bad arguments, a malformed account id, or a range that is too long | Fix the arguments |
| `[account_not_found]` | Unknown account id | Call `list_accounts` again |
| `[provider_error]` | Any other upstream failure, including malformed or non-JSON responses and endpoints the bank does not offer | Usually safe to retry later |
| `[config_error]` | Missing or invalid configuration | Fix the environment variables |
| `[token_store_error]` | The token store could not be read or written | Fix the store; see [Tokens and the token store](#tokens-and-the-token-store) |
| `[malformed_import_file]` | A statement file or `content` could not be parsed (the message names the file and line) | Fix or replace the file |
| `[internal_error]` | A bug. No detail is exposed; see the server log | Report it |

## Configuration

Everything is configured through environment variables. Nothing is read from a `.env` file
automatically: export the variables, or use `uv run --env-file .env amex-mcp`.

| Variable | Default | Purpose |
|---|---|---|
| `AMEX_PROVIDER` | auto | `truelayer` or `import`. Unset: `truelayer` if `TRUELAYER_CLIENT_ID` is set, otherwise `import` if `AMEX_IMPORT_DIR` is set |
| `AMEX_MAX_RANGE_DAYS` | `400` | Longest `since`..`before` range `list_transactions` accepts |
| `TRUELAYER_CLIENT_ID` | - | TrueLayer application client id |
| `TRUELAYER_CLIENT_SECRET` | - | TrueLayer application client secret |
| `TRUELAYER_ENV` | `live` | `live` or `sandbox` |
| `TRUELAYER_REDIRECT_URI` | `http://localhost:3000/callback` | Redirect URI used by the link helper; must be allow-listed in the TrueLayer Console |
| `TRUELAYER_PROVIDERS` | `uk-ob-all uk-oauth-all` (sandbox adds `uk-cs-mock`) | `providers` value in the consent link (also `--providers`) |
| `TRUELAYER_TOKEN_ENCODING` | `json` | `json` or `form`: body encoding for the token endpoint |
| `TRUELAYER_WINDOW_DAYS` | `88` | Days per transactions request (2-88). Longer ranges are fetched in windows that overlap by one day |
| `AMEX_TRUELAYER_REFRESH_TOKEN` | - | Seed refresh token, used **only when the token store is empty** |
| `AMEX_TOKEN_STORE` | `file` | `file`, `memory`, or `package.module:ClassName` (a custom store; this imports and runs that code) |
| `AMEX_TOKEN_FILE` | `~/.amex-mcp/tokens.json` | Path of the token file for the `file` store |
| `AMEX_IMPORT_DIR` | - | Folder of statement exports (statement-file route) |
| `AMEX_IMPORT_ACCOUNT_ID` | `amex-import` | Account id for files directly inside `AMEX_IMPORT_DIR` |
| `AMEX_IMPORT_CURRENCY` | `GBP` | Currency of CSV exports (OFX files state their own) |
| `AMEX_CSV_CHARGES_POSITIVE` | `true` | CSV exports show charges as positive numbers |
| `AMEX_OFX_CHARGES_NEGATIVE` | `true` | OFX/QFX exports show charges as negative numbers |
| `AMEX_CSV_DATE_ORDER` | `dmy` | `dmy` (UK) or `mdy` (US) for ambiguous CSV dates |
| `AMEX_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (also `--transport`) |
| `AMEX_MCP_HOST` | `127.0.0.1` | Bind address for HTTP (also `--host`) |
| `AMEX_MCP_PORT` | `8000` | Port for HTTP (also `--port`) |
| `AMEX_MCP_PATH` | `/mcp` | URL path for HTTP (also `--path`) |
| `AMEX_MCP_AUTH_TOKEN` | - | Bearer token for HTTP. Required when the host is not loopback; recommended always |
| `AMEX_MCP_ALLOW_NO_AUTH` | - | `1` allows a non-loopback bind without a token. Only for use behind a gateway that authenticates every request |
| `AMEX_MCP_ALLOWED_HOSTS` | - | Comma-separated `Host` header allow-list for HTTP (turns on DNS-rebinding protection) |
| `AMEX_MCP_ALLOWED_ORIGINS` | `https://<each allowed host>` | Comma-separated `Origin` allow-list; only used with `AMEX_MCP_ALLOWED_HOSTS` |
| `AMEX_MCP_LOG_LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Logs go to stderr |
| `TRUELAYER_AUTH_BASE` | per `TRUELAYER_ENV` | **Test hook.** Overrides the auth base URL. Must be `https://` unless the host is loopback |
| `TRUELAYER_API_BASE` | per `TRUELAYER_ENV` | **Test hook.** Overrides the API base URL. Must be `https://` unless the host is loopback |

Not configuration, but read from the environment: `USERDOMAIN` and `SYSTEMROOT` on Windows (to
restrict the token file's ACL), and the standard proxy and certificate variables honoured by
[httpx](https://www.python-httpx.org/environment_variables/) (`HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`).

The generic `TRUELAYER_REFRESH_TOKEN` is deliberately **not** read: a refresh token belongs to one
connection, and a TrueLayer application can have several.

## Tokens and the token store

TrueLayer issues an access token that lasts about an hour and a long-lived refresh token. The server
refreshes automatically and saves whatever comes back, including a new refresh token if TrueLayer
issues one.

- **Default store:** `~/.amex-mcp/tokens.json` (override with `AMEX_TOKEN_FILE`). The file is
  **plaintext JSON**. It is written atomically (temporary file, `fsync`, rename) and restricted to
  the current user: mode `0600` on POSIX; on Windows inherited permissions are removed and only the
  current user is granted access (`icacls /inheritance:r /grant:r "<user>:(F)"`). If restricting the
  ACL fails, a warning is logged and the file is still written.
- **Precedence:** a token in the store always wins. `AMEX_TRUELAYER_REFRESH_TOKEN` is used only when
  the store is empty; once the store holds a token, changing that variable has no effect.
- **Re-linking:** run `amex-mcp-link`. It overwrites the store. To start from nothing, run
  `amex-mcp-link --reset` (or delete the token file); the seed variable, if set, then applies again.
- **If saving fails after a refresh:** the new tokens are kept in memory and used, the save is
  retried on every later call, and the old refresh token is never sent again. The data you asked for
  is still returned, with a `token_store_warning` field starting `[token_store_error]`, and
  `get_connection_status` reports the same text in `warning`. The error is raised only when the
  call itself cannot be served (for example by the link helper, which has nothing else to return). If the process stops before a save succeeds,
  you may have to link the card again.
- **One refresh at a time:** concurrent calls share a single refresh.
- **`AMEX_TOKEN_STORE=memory`** keeps nothing on disk. A refresh token issued during the run is lost
  on exit, so use it only for tests.
- **Custom store:** implement two methods and set `AMEX_TOKEN_STORE=your_module:YourStore`.

  ```python
  class YourStore:
      def load(self) -> dict | None: ...  # the stored document, or None
      def save(self, tokens: dict) -> None: ...  # replace the stored document
      def clear(self) -> None: ...  # optional; used by --reset
  ```

  The class is constructed with no arguments. The document is a JSON-serialisable dict with the keys
  `access_token`, `access_token_expires_at` (unix seconds), `refresh_token`, `connected_at` and
  `updated_at`.

## Limitations

- **Consent lasts about 90 days.** UK Open Banking requires the customer to reconfirm access at
  least every 90 days. After that, tools return `[consent_expired]` and a person must link the card
  again or use `--extend`. `get_connection_status` sets `warning` once fewer than 14 days remain.
- **Rate limits.** Banks limit how often data can be fetched when the customer is not present
  (commonly a handful of times a day per endpoint). This server never sends the `X-PSU-IP` header,
  so every request counts as unattended. A `[rate_limited]` error is never retried by the server;
  do not loop on it.
- **Requests per call.** One `list_transactions` call makes one request per 88-day window plus one
  for pending items, and never more than 30 upstream requests in total, retries included.
- **History.** Banks typically serve about 90 days of history without a fresh authentication.
  Older ranges may fail with `[sca_required]` unless requested within minutes of linking.
- **Refresh tokens may rotate.** Handled, provided the token store is persistent.
- **Account ids may change** when a card is re-linked. Call `list_accounts` rather than storing the
  id. Booked transaction ids do not depend on it when the bank supplies a transaction id; ids derived
  from content (pending items, and banks that supply no id) do.
- **One card per consent** is the expected case. A second card on the same consent works, with the
  id caveat described under [Tools](#tools).
- **Balance excludes pending transactions.**
- **Statement files** contain booked transactions only, are as fresh as your last export, and CSV
  exports carry no balance. `get_connection_status` reports `data_through` and `data_age_days`.
- **Dates** are calendar dates; the time of day is not exposed.

## Security

- **Treat every text field as untrusted third-party input.** Descriptions, merchant names, notes,
  categories, account names and the contents of statement files are written by merchants, banks or
  whoever produced the file. They can contain prompt-injection text aimed at the model reading
  them, or spreadsheet formulas.
  - Do not pair this server with tools that can run shell commands, send e-mail or move money
    without a human confirming each action.
  - Anything that writes these fields to a spreadsheet or CSV must neutralise cells that start with
    `=`, `+`, `-` or `@` (for example by prefixing a single quote).
- **Token files are plaintext.** Protection is file permissions only (`0600` on POSIX; an ACL
  restricted to the current user on Windows). Anyone who can read the file as you, or read a backup
  of it, can read your card data until the consent ends. Keep it off shared and synced folders.
- **`amex-mcp-link --print-refresh-token` prints a secret** to the terminal, where it may end up in
  scrollback or logs.
- **`AMEX_TOKEN_STORE=module:Class` executes code** named in the environment. Whoever controls the
  environment controls what runs.
- **The HTTP transport is plain HTTP with one static bearer token.** Put it behind a reverse proxy
  that terminates TLS, and always set `AMEX_MCP_AUTH_TOKEN`, even on loopback (other local processes
  and browser pages can reach loopback ports). The server refuses to bind a non-loopback address
  without a token unless `AMEX_MCP_ALLOW_NO_AUTH=1`. Use a long random token and rotate it if it
  leaks.
- **Proxy environment variables are honoured** (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`). A proxy
  you did not intend would see your requests' metadata; TLS still protects their contents unless
  `SSL_CERT_FILE` / `SSL_CERT_DIR` point at certificates you do not control.
- **`TRUELAYER_AUTH_BASE` / `TRUELAYER_API_BASE` are test hooks.** They redirect your client secret
  and tokens to another host, so they must be `https://` (plain `http://` is accepted only for
  loopback). Leave them unset in normal use.
- The link helper listens only on `127.0.0.1`, ignores requests whose `state` does not match,
  requires `state` in a pasted URL, uses PKCE, and never logs the redirect query string.
- Secrets are not written to logs or error messages. Statement parsing is size-capped and runs off
  the event loop.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Unverified assumptions

None of these could be checked without real credentials. If you run this against the live API,
these are the first things to check.

| Assumption | If it is wrong |
|---|---|
| TrueLayer may or may not rotate refresh tokens | Nothing: both are handled, as long as the store is persistent |
| Balance `current` is positive when money is owed; `available` is available credit | `balance_minor` and `last_statement_balance_minor` have the wrong sign, or `available_credit_minor` means something else. Compare with the Amex app |
| `account_id` may change on re-link | If it never changes, nothing. Content-derived ids (pending items, banks without transaction ids) change when it does |
| Pending items have no stable id | If they do, nothing is lost; ids for pending items are content-derived either way |
| `timestamp` is the date Amex shows for the transaction | `date` could be the posting date instead of the purchase date |
| `X-PSU-IP` is not needed | Without it requests are unattended and rate limited more tightly |
| An individual can get Live Data API access, at an acceptable price | The TrueLayer route is unusable; use statement files |
| `providers=uk-ob-all uk-oauth-all` offers American Express | The bank picker is empty or lacks Amex: set `TRUELAYER_PROVIDERS` / `--providers` |
| The consent link accepts `code_challenge` and `code_challenge_method=S256` (TrueLayer documents `code_verifier` on the token endpoint) | The link is rejected: use `--no-pkce` |
| `from` / `to` are inclusive calendar days, and a `to` in the future is rejected | Windows overlap by a day and `since` is padded by a day to tolerate an exclusive `to`; transactions dated today could still be missing until tomorrow |
| The token endpoint accepts JSON | Set `TRUELAYER_TOKEN_ENCODING=form` |
| `/connections/extend` accepts `user: {name, email}` | `--extend` reports TrueLayer's validation message; link the card again instead |
| Amex UK CSV headers are `Date`, `Description`, `Amount`, optionally `Extended Details`, `Reference`, `Category`, ... | Parsing fails with `[malformed_import_file]` naming what is missing; headers are matched by name, in any order |
| Amex data flows through TrueLayer at all (the provider is listed, not tested), with or without merchant names, categories and pending items | Fields come back `null`; `pending_supported` is `false` |

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `[config_error] No data source is configured` | Set `TRUELAYER_CLIENT_ID` and `TRUELAYER_CLIENT_SECRET`, or `AMEX_IMPORT_DIR` |
| `[not_connected]` | The card was never linked on this machine, or the token file is elsewhere. Run `amex-mcp-link`; check `AMEX_TOKEN_FILE` |
| `[consent_expired]` straight after changing `AMEX_TRUELAYER_REFRESH_TOKEN` | The store still holds the old token and wins. Run `amex-mcp-link --reset`, or link again |
| `[config_error] TrueLayer rejected the client credentials` | Wrong id or secret, or `TRUELAYER_ENV` does not match the credentials (sandbox ids start with `sandbox-`) |
| The consent link shows no American Express | Try `--providers`; check the application is Live |
| The consent link is rejected outright | Check the redirect URI is allow-listed; try `--no-pkce` |
| `Could not listen on port 3000` | Something else uses the port. Use another allow-listed `--redirect-uri`, or `--paste` |
| The helper keeps waiting after you approved | The redirect went to a different host or port. Use `--paste` |
| `[token_store_error]` | The token file or its folder is not readable or writable. Fix permissions, or point `AMEX_TOKEN_FILE` elsewhere |
| `[rate_limited]` | Wait several hours. Fetch less often |
| `[sca_required]` | Ask for a range within the last 90 days |
| `[malformed_import_file] ... sign convention` | Set `AMEX_CSV_CHARGES_POSITIVE=false` |
| `[malformed_import_file] ... invalid date` | A US-format export: set `AMEX_CSV_DATE_ORDER=mdy` |
| HTTP 401 from the server | Missing or wrong `Authorization: Bearer` header |
| `Refusing to serve card data on a non-loopback address` | Set `AMEX_MCP_AUTH_TOKEN` |
| HTTP 421 or "Invalid Host header" | Add the public host name to `AMEX_MCP_ALLOWED_HOSTS` |
| A transaction you expected is missing | It was pending when you fetched. See [Pending transactions](#pending-transactions-always-fetch-overlapping-windows) |

Set `AMEX_MCP_LOG_LEVEL=INFO` for more detail on stderr.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

PowerShell:

```powershell
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

bash:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Layout:

```text
src/amex_mcp/
  server.py               MCP tools, stdio + streamable HTTP, bearer-token gate
  link.py                 link helper (amex-mcp-link)
  models.py               normalised shapes, minor-unit maths, dates, stable ids
  errors.py               typed errors with stable codes
  token_store.py          TokenStore protocol, file + memory stores, plug-in loader
  providers/
    base.py               Provider interface
    truelayer.py          TrueLayer Data API v1 (cards)
    statement_import.py   Amex CSV / OFX / QFX parser + folder provider
tests/                    pytest suite + synthetic fixtures
docs/research.md          dated notes on the data-access options
```

To add another data source, subclass `Provider` and add a branch in `server.build_provider`.

## Licence

MIT. See [LICENSE](LICENSE). Provided as-is, without warranty of any kind.
