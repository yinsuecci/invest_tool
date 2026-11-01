# Privacy and release safety

## Public source

Run `python tools/release_source.py --check` before publishing. Run
`python tools/release_source.py` to create `release/dca-lab-source.zip` from an
explicit source allowlist. The archive excludes datasets, generated reports,
screenshots, journals, account exports, local agent settings and scratch notes.
It does not upload anything or delete local data. Do not publish a workspace ZIP.

The scanner reports only file names, line numbers and rule names, never matched
values. It detects common credential assignments, provider tokens, private keys,
credential URLs and personal home paths. It blocks symlinks and Git-tracked files
outside the release allowlist. This is defense in depth, not proof of absence of
secrets: arbitrary encodings, unusual secret formats and personal prose need review.

`.gitignore` does not remove already tracked files or Git history. The current
export does not contain Git history. If a credential was ever published, revoke
it at the provider; deleting a file or rewriting history does not invalidate it.
Never attach account screenshots, HAR files, memory dumps or private order
journals to public issues. Use fabricated fixtures for bug reports.

## Local service

API credentials stay in process memory and are cleared on disconnect/restart;
they are not encrypted memory and cannot be protected from local administrators,
debuggers, browser extensions or a compromised machine. Disable browser password
saving for API credentials. Do not submit secrets through public issues or chat.

The gateway binds only to loopback, checks Host/Origin/session/CSRF, disables
request logging and caching, and does not serve the runtime database. It is not
a multi-user server. Other processes running as the same local user may access
it. Do not expose it through tunnels, reverse proxies or shared hosting.
The production Binance transport does not inherit environment proxies, custom
CA environment variables or `.netrc` authentication. TLS verification stays on.

Order journals deliberately retain amounts, timestamps, request IDs and exchange
order IDs for idempotency and unknown-outcome recovery. They are private financial
data even though they contain no API secrets. They remain local and are excluded
from release. Do not delete pending/unknown journal entries merely to clear a
blocked request: reconcile exchange outcomes first to avoid duplicate orders.

Live mode remains opt-in. Use a dedicated restricted API key without withdrawals
and with appropriate provider IP restrictions. No real financial actions are
needed to run tests or build the public source archive.
