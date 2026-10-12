# Contributing

Thanks for helping. This is a small, unofficial project; please keep changes focused.

## Setup

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). The commands are the same in PowerShell
and bash.

```
uv sync
```

## Before you open a pull request

```
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

- `uv run ruff format .` fixes formatting; `uv run ruff check --fix .` fixes most lint findings.
- The tests need no network and no credentials. They write temporary files under `.pytest_tmp/`
  in the project folder (git-ignored).
- Every change in behaviour needs a test that fails without it.

## Rules

- **No real credentials or personal data, anywhere**: not in tests, fixtures, issues, pull requests
  or logs pasted into them. That means no client secrets, tokens, card or account numbers, real
  transactions or real statement files. Use obviously synthetic values (`test-client`,
  `EXAMPLE DINER`).
- Tests must not call the real TrueLayer or American Express services. Use `respx` mocks or the
  local fake server in `tests/conftest.py`.
- The server is read-only. Changes that add tools which move money or modify an account will not
  be accepted.
- Keep the token-store interface (`load()` / `save()`, the keys of the stored document, and
  `AMEX_TOKEN_STORE`) backward compatible: other code may implement it.
- Keep the tool contract in the README (field names, error codes, date and sign conventions) in step
  with the code. A test checks that every environment variable the code reads is in the README
  table and in `.env.example`.
- When you rely on something about a provider's API that you could not verify, say so under
  "Unverified assumptions" in the README.
- If you start a server or any other process while testing, stop that process by its id. Do not
  kill processes by name.

## Reporting bugs

Open an issue with what you did, what you expected and what happened, plus the version and your
operating system. Redact anything private first. For security problems, see `SECURITY.md` instead.
