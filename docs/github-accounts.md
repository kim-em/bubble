# Multiple GitHub accounts

`bubble open owner/repo --github-account alice` selects a GitHub login for the
host GitHub operations and the container's scoped authentication proxy. Log
accounts into `gh auth login` once, then run bubbles for different logins
concurrently. `BUBBLE_GITHUB_ACCOUNT` supplies the same setting in the environment.

An inherited `GH_TOKEN` or `GITHUB_TOKEN` must match the requested login. Without
one, Bubble reads `gh auth token --hostname github.com --user alice`. Selection
verifies the identity through GitHub, never changes gh's active account, and stops
on missing or mismatched credentials. TauCeti's `--github-account` first resolves
the named account and supplies its verified token to Bubble.

The shared host daemon binds each container's generated scoped bearer token to
that container's selected credential. The credential lives only in the host's
owner-readable `~/.bubble/auth-tokens.json`, with temporary writes also mode 0600;
it is never included in argv or injected into the container in proxy mode.
Revoking the container's scoped token removes the credential record. Requests use
an instance-local credential, so a concurrent container cannot switch another
container's account. GraphQL ownership caches and in-flight queries are scoped to
the credential as well. The existing repository and GraphQL restrictions apply.
The explicit `direct` GitHub security setting retains its existing raw-token
injection behavior.

The daemon advertises the `github-account` capability. An old daemon is rejected
for account-selected bubbles and refreshed through the normal proxy lifecycle.
After replacing or revoking a selected credential, close and reopen the bubble;
a selected container never refreshes through gh's active account.

For TauCeti, use a stable installation of this checkout or point
`TAUCETI_BUBBLE` at its `.venv/bin/bubble` executable, then run
`tauceti work --github-account alice --bubble`. The proxy must be restarted from
the same installation with `bubble gh proxy start`.
