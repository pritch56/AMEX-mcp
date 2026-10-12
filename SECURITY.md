# Security policy

## Reporting a vulnerability

Please report security problems **privately**, using GitHub's private vulnerability reporting for
this repository ("Security" tab, then "Report a vulnerability"). Do not open a public issue for a
suspected vulnerability.

Include what you found, how to reproduce it, and the version or commit. **Never include real
credentials, tokens, card numbers or statement files** in a report; use synthetic data.

This is a volunteer project: there is no guaranteed response time and no bug bounty.

## Supported versions

Only the latest release (and the main branch) receives fixes.

## Scope

In scope:

- leaking secrets (client secret, access or refresh tokens, bearer token) through logs, errors,
  tool results or files with loose permissions;
- bypassing the HTTP bearer-token gate, or the refusal to bind a non-loopback address without one;
- weaknesses in the link helper's OAuth flow (state, PKCE, loopback listener);
- requests being sent to a host other than the configured provider;
- denial of service or code execution through statement files or tool arguments;
- any way to make this read-only server change an account or move money.

Out of scope (documented behaviour; see the Security section of the README):

- token files being plaintext, protected only by file permissions;
- the HTTP transport being plain HTTP that expects TLS in front of it;
- `AMEX_TOKEN_STORE=module:Class` running code named in the environment;
- prompt-injection or formula text inside transaction data: the server passes third-party text
  through, and consumers must treat it as untrusted;
- problems in TrueLayer, American Express or their APIs (report those to them).

This project is unofficial and is not affiliated with American Express or TrueLayer.
