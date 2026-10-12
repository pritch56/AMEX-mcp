# Data-access options for American Express UK card data

**Dated notes: as observed on 10 October 2026.** This is a reading of public web pages on that
date. Nothing here was confirmed by signing up, by contacting any provider, or by making a live API
call. Offerings, prices and sign-up rules change; check the linked pages yourself before relying on
any line below. None of the companies named has reviewed or endorsed this document.

## Background

American Express does not appear to offer an API for individual cardholders. Its developer portal is
aimed at merchants and partners, and its UK Open Banking interface is for regulated account
information service providers. In practice an individual therefore needs either a regulated
aggregator that resells access, or the statement files that the Amex website lets a cardholder
download.

## Options looked at

| Option | Observation on 10 October 2026 | Used here |
|---|---|---|
| TrueLayer Data API | The public provider list included an `ob-amex` entry ("American Express", UK) with the scopes `info`, `cards`, `balance`, `transactions`, `offline_access`. Console sign-up was self-serve. No public price list was found, and it was not confirmed that an individual can take an application Live. | Yes (automatic route) |
| Amex statement export (CSV / OFX / QFX) | Available to cardholders from the website, free, no third party. Manual. | Yes (file route) |
| GoCardless Bank Account Data (formerly Nordigen) | The sign-up page appeared to say that new sign-ups were disabled. | No |
| Enable Banking | A "restricted mode" for one's own accounts was described, but no UK institutions were found in its public institution list on that date. | No |
| Yapily | American Express UK appeared in its institution documentation, with a per-application registration process that looked aimed at regulated businesses. | No |
| Plaid | The plans visible to UK customers appeared to be sales-led; the American Express institution page found was for the US. | No |
| Tink, Salt Edge, Moneyhub | No self-serve tier for individuals was found. | No |
| Signing in to the Amex website with stored credentials | Likely to breach the cardholder terms, and a security risk. | Never |

"No" means only that the option did not look usable by an individual on that date, judged from
public information. It is not a statement about the quality of any service.

## Costs

- TrueLayer: no public price list was found. Some community posts described personal Live use
  without being asked for a payment method; that is anecdotal and was not verified. Check the
  TrueLayer Console and terms.
- Statement export: free.

## Sources (accessed 10 October 2026)

- TrueLayer provider list: <https://auth.truelayer.com/api/providers>
- TrueLayer card endpoints and sign convention: <https://docs.truelayer.com/docs/card-data-requests>
- TrueLayer token endpoint: <https://docs.truelayer.com/reference/generateaccesstoken>
- TrueLayer connection lifetime: <https://docs.truelayer.com/docs/general-data-api-concepts>
- TrueLayer errors: <https://docs.truelayer.com/docs/data-api-errors>
- TrueLayer extend-connection: <https://docs.truelayer.com/reference/extendconnection>
- GoCardless Bank Account Data sign-up notice: <https://bankaccountdata.gocardless.com/new-signups-disabled>
- Enable Banking institution list and FAQ: <https://enablebanking.com/api/aspsps>, <https://enablebanking.com/docs/faq/>
- Yapily American Express configuration notes: <https://docs.yapily.com/institution-configurations/uk/AMEX>
- Plaid billing documentation: <https://plaid.com/docs/account/billing>
- Community notes on the Amex CSV layout: <https://github.com/jmcameron/amex2qif>

Re-read on 11 October 2026:

- the extend-connection reference: the endpoint is `https://api.truelayer.com/connections/extend`
  (no `/data/v1` prefix), and errors are returned as `application/problem+json`;
- the token endpoint reference: `code_verifier` is an optional field, "Required only if using PKCE
  flow". A parameter table for the consent link itself was not found, so the `code_challenge` and
  `code_challenge_method` names used by the link helper are the standard OAuth ones, unconfirmed.
