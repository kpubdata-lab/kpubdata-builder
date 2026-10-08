# Replay fixtures shipped with KPubData Builder (#837)

Recorded provider responses that `kpubdata-builder serve --replay` serves instead of
calling the live API, so a client (Studio's end-to-end suite) can run the Public API
path without a provider key and without a checkout of another repository.

| Dataset | Example | Source |
|---|---|---|
| `datago.air_station` | `gangnam_full_page` | `kpubdata-lab/kpubdata` `tests/fixtures/datago/air_station/` at `8740e7d` |

Each `*.meta.json` records the request and the SHA-256 of its `*.raw.json`; the
package's tests check the two still agree. The service key in the recording is
`[REDACTED]`. To add a fixture, record it in kpubdata (`make record`) and copy both
files here — never write one by hand.

Replay matches every request parameter, the page size included. A Public API source
that replays `gangnam_full_page` must say `page_size: 100` in its `params`, as the
recording did: otherwise a build asks for the dataset's `max_page_size` and a preview
for its `limit` (#1185), and no recording matches.
