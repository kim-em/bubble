# Multiple GitHub accounts

`bubble open owner/repo --github-account alice` selects a GitHub login for the
host GitHub CLI operations and the container's scoped authentication proxy. Log
accounts into `gh auth login` once, then run bubbles for different logins
concurrently. `BUBBLE_GITHUB_ACCOUNT` supplies the same setting in the environment.

An inherited `GH_TOKEN` or `GITHUB_TOKEN` must match the requested login. Without
one, Bubble reads `gh auth token --hostname github.com --user alice`. Selection
verifies the identity through GitHub, never changes gh's active account, and stops
on missing or mismatched credentials. TauCeti's `--github-account` first resolves
the named account and supplies its verified token to Bubble.

The shared host daemon binds each container's generated scoped bearer token to
that container's selected credential. The credential lives only in the host's
owner-readable `~/.bubble/github-account-tokens.json`, with temporary writes also mode 0600;
it is never included in argv or injected into the container in proxy mode.
This file contains raw GitHub credentials and must be excluded from shared backups.
Popping the container removes the credential record, and daemon startup prunes
records for containers that no longer exist when runtime inventory succeeds.
Cleanup runs after the listener is published, with a bounded inventory call;
unavailable runtimes retain records and never trigger dependency installation.
Credentials for retained containers remain until those containers are popped. Requests use
an instance-local credential, so a concurrent container cannot switch another
container's account. GraphQL ownership caches and in-flight queries are scoped to
the credential as well. The existing repository and GraphQL restrictions apply.
The explicit `direct` GitHub security setting retains its existing raw-token
injection behavior.

The daemon advertises the `github-account` capability. An old daemon is rejected
for account-selected bubbles. Local setup refreshes it through the normal proxy
lifecycle; remote setup reports an error and requires `bubble gh proxy start`
from the account-aware installation.
Account-bound tokens use a separate registry, so even a downgrade of a running
daemon fails closed for existing selected-account containers. A daemon can serve
selected accounts without any default gh login; unselected containers receive
403 until a default credential is available.
After replacing or revoking a selected credential, pop and recreate the bubble;
a selected container never refreshes through gh's active account. Reattaching an
existing bubble keeps its original credential; passing `--github-account` on a
reattach does not switch that container's account. TauCeti pops and recreates its
worker bubble before each round.

For TauCeti, use a stable installation of this checkout or point
`TAUCETI_BUBBLE` at its `.venv/bin/bubble` executable, then run
`tauceti work --github-account alice --bubble`. The proxy must be restarted from
the same installation with `bubble gh proxy start`.

Account selection does not partition the host Git object store: bubbles for the
same repository can reuse cached objects on the same host. Relay-created bubbles
use the host's default account; pass `--github-account` on an explicit `bubble open`
to select one. Host Git subprocesses require a credential helper that uses the
selected token; TauCeti supplies this helper when selecting an account.
