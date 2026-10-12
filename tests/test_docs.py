"""The documentation must keep up with the code: every setting documented, no stale names."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "src").rglob("*.py")))
README = (ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
DOCS = {p.name: p.read_text(encoding="utf-8") for p in [*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")]}

# Names that appear in the source as string literals and are read from the environment.
ENV_NAMES = sorted(
    set(re.findall(r'"((?:AMEX|TRUELAYER)_[A-Z0-9_]+)"', SOURCE))
    - {"TRUELAYER_REFRESH_TOKEN"}  # mentioned only to say it is NOT read
)
REMOVED = ("AMEX_MCP_ALLOW_UNAUTHENTICATED", "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MCP_AUTH_TOKEN", "LOG_LEVEL")
ERROR_CODES = (
    "consent_expired",
    "token_expired",
    "not_connected",
    "rate_limited",
    "sca_required",
    "invalid_request",
    "account_not_found",
    "provider_error",
    "config_error",
    "token_store_error",
    "malformed_import_file",
    "internal_error",
)


def test_every_setting_is_documented():
    assert len(ENV_NAMES) >= 28
    missing_readme = [name for name in ENV_NAMES if f"| `{name}` |" not in README]
    missing_example = [name for name in ENV_NAMES if not re.search(rf"^#? ?{name}=", ENV_EXAMPLE, re.M)]
    assert missing_readme == [] and missing_example == []


def test_no_documented_setting_is_unknown_to_the_code():
    documented = set(re.findall(r"^\| `((?:AMEX|TRUELAYER)_[A-Z0-9_]+)` \|", README, re.M))
    assert documented - set(ENV_NAMES) == set()
    in_example = set(re.findall(r"^#? ?((?:AMEX|TRUELAYER)_[A-Z0-9_]+)=", ENV_EXAMPLE, re.M))
    assert in_example - set(ENV_NAMES) == set()


def test_removed_generic_names_are_not_read_by_the_code():
    for name in REMOVED:
        assert not re.search(rf'"{name}"', SOURCE), name
    assert '"PORT"' not in SOURCE
    # the generic refresh-token variable is never read from the environment
    assert not re.search(r'get\("TRUELAYER_REFRESH_TOKEN"', SOURCE)


def test_every_error_code_the_code_can_raise_is_documented():
    from amex_mcp import errors

    codes = {
        cls.code for cls in vars(errors).values() if isinstance(cls, type) and issubclass(cls, errors.AmexMcpError)
    } - {"error"}
    assert codes | {"internal_error"} == set(ERROR_CODES)
    for code in ERROR_CODES:
        assert f"`[{code}]`" in README, code


def test_contract_notes_are_documented():
    for needle in (
        "earliest_pending_date",
        "pending_excluded",
        "pending_supported",
        "token_store_warning",
        "balance_minor",
        "Error executing tool",
        "de-duplicate on `id`",
        "14 days",
        "Europe/London",
        "${TRUELAYER_CLIENT_SECRET}",
        "not affiliated with",
        "prompt-injection",
        "`=`, `+`, `-` or `@`",
        "--print-refresh-token",
        "X-PSU-IP",
    ):
        assert needle.lower() in README.lower(), needle
    assert README.splitlines()[0] == "# amex-mcp" and "unofficial" in README[:300].lower()
    assert "Status:" in README[:1200] and "alpha" in README[:1200]


def test_no_inline_secrets_machine_paths_or_private_setup_in_the_docs():
    everything = [README, ENV_EXAMPLE, SOURCE, *DOCS.values()]
    for text in everything:
        assert not re.search(r"[A-Z]:\\\\?Finance", text)
        for phrase in (
            "the routine",
            "weekly job",
            "finance sheet",
            "sibling server",
            "the amex agent",
            "on your behalf",
            "while building",
            "--directory mcp/",
        ):
            assert phrase not in text.lower(), phrase
    assert not re.search(r'"TRUELAYER_CLIENT_SECRET":\s*"(?!\$\{)', README)
    assert not re.search(r"\b\d+ tests\b", README)  # no hard-coded test counts
    # "spreadsheet" may appear only as the security warning about formula cells
    for line in README.splitlines():
        if "spreadsheet" in line.lower():
            assert "formula" in line.lower() or "neutralise" in line.lower() or "csv" in line.lower(), line


def test_packaging_metadata():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    meta = project["project"]
    assert meta["license"] == "MIT" and meta["authors"] == [{"name": "pritch56"}]
    assert meta["description"].startswith("Unofficial ") and meta["requires-python"] == ">=3.11"
    assert "mcp>=2.3,<3" in meta["dependencies"]
    declared = {re.split(r"[<>=\[ ]", d)[0] for d in meta["dependencies"]}
    assert {"mcp", "httpx", "anyio", "uvicorn", "tzdata"} <= declared
    assert project["build-system"]["build-backend"] == "hatchling.build"
    assert (ROOT / "LICENSE").exists() and not (ROOT / ".python-version").exists()
    assert not (ROOT / "scripts").exists()
    for name in ("SECURITY.md", "CHANGELOG.md", "CONTRIBUTING.md", "research.md"):
        assert name in DOCS, name
    for entry in (".env", ".env.*", "!.env.example", "tokens.json", ".amex-mcp/", "statements/", "ruvector.db", "*.db"):
        assert entry in GITIGNORE, entry
