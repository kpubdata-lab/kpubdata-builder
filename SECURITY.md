# Security Policy

## Reporting a vulnerability

**Do not open a public issue.** Use [Private Vulnerability Reporting](https://github.com/yeongseon/kpubdata-builder/security/advisories/new) — it is enabled on this repository.

Please include, as far as you can:

- What an attacker can do, not only what looks wrong
- The smallest way to reproduce it
- Which commit or release you looked at

You will get an acknowledgement. If the report turns out to be a defect rather
than a vulnerability, it is moved to a normal issue and you are told so.

## What counts as a vulnerability here

This project handles **user-supplied API keys for Korean public data services**
(BYOK). The things we most want to hear about:

- **A user's key being readable by the operator or by another user.** A
  multi-user deployment (OIDC, or `ENFORCE_OWNERSHIP`) does not store provider
  keys: a key lives only for its request or job, and there is no fallback to the
  operator's key. A single-user deployment stores them encrypted per principal.
  ADR 0012 (2026-09-30 amendment) and ADR 0020 hold the rules;
  `docs/CREDENTIAL_SURFACE.md` records every place a key is known to reach.
- **One user receiving another user's data**, including through a shared
  response cache.
- **A request reaching an address it should not** — the URL source guard in
  `ingestion/url_fetch.py` blocks non-public addresses and pins the resolved IP.
- **Publishing data whose terms forbid redistribution.**

Also in scope: anything that lets one user reach another user's data, and
anything that makes published data violate its provider's terms.

## What does not count

- A missing hardening measure with no reachable consequence
- Denial of service by simply sending a lot of requests
- Outdated dependencies with no exploitable path in this code — open a normal issue
- Findings from a scanner, pasted without a reachable path

## Supported versions

Only the latest release of each component receives fixes. Declared versions and
tags agree (#690, closed 2026-09-30): report against the release version that
KPubData Builder and KPubData Studio share (ADR 0004), or a commit SHA for
unreleased code.

## Known limits, stated deliberately

- In a single-user deployment provider keys are stored encrypted, not kept out
  of storage: an operator holding the master key can decrypt. That is by
  decision (#682, ADR 0020) — the operator is the user there. A multi-user
  deployment does not store them.
- A publish token (HF, Kaggle) is a key under the same multi-user rule (ADR 0020,
  confirmed 2026-10-01; #925): in a multi-user deployment it is sent with each
  request in the `X-Publish-Credential` header and held in memory for that
  request only. A stored publish token is not read and the server's `HF_TOKEN` /
  `KAGGLE_*` is never used, whatever
  `KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL` says.
- `ENFORCE_OWNERSHIP` defaults off only for a single-user deployment. When
  `OIDC_ISSUER` is set it is forced on whatever its value (#635), and another
  user's run answers 404, not 403 (#796).
- A report in either Korean or English is fine.
