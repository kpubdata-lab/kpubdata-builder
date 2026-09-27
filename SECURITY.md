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

- **A user's key being readable by the operator or by another user.** Provider
  credentials are stored encrypted per principal; `docs/CREDENTIAL_SURFACE.md`
  records every place a key is known to reach.
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

Only the latest release of each component receives fixes. Version and tag are
being reconciled (kpubdata-builder#690); until that lands, report against a
commit SHA rather than a version number.

## Known limits, stated deliberately

- Provider keys are stored encrypted, not kept out of storage. Whether that
  changes is being decided in #682; encrypted storage is not no storage, and an
  operator holding the master key can decrypt.
- `ENFORCE_OWNERSHIP` defaults off, which suits a single-user deployment and
  does not suit a multi-user one. `auth.py` warns when the combination is unsafe.
- A report in either Korean or English is fine.
