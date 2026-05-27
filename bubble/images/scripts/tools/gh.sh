#!/bin/bash
set -euo pipefail

# Install GitHub CLI and set up bubble-managed gh configuration.
# gh is configured to route all HTTP traffic through the auth proxy
# via http_unix_socket, so the host's GitHub token never enters the
# container. The socket is served by a small in-container socat
# forwarder (started lazily by the gh wrapper below) that relays to the
# host auth proxy's TCP listener on the incus bridge — no incus proxy
# device, so no leaking forkproxy helper.
export DEBIAN_FRONTEND=noninteractive

# Install gh from official apt repository (socat backs the gh forwarder)
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null

apt-get update -qq
apt-get install -y -qq gh socat

# Create bubble-managed gh config directory.
# GH_CONFIG_DIR is set at container creation time (not image build time)
# because the auth token is per-container.
mkdir -p /etc/bubble/gh
chown 1001:1001 /etc/bubble/gh

# Pre-populate config.yml with http_unix_socket pointing to the gh
# forwarder socket (served by the socat helper in the wrapper below).
# bubble rewrites this at container-creation time, but the path is
# stable so we bake the default.
cat > /etc/bubble/gh/config.yml <<'GHCONF'
version: "1"
http_unix_socket: /home/user/.bubble/gh.sock
GHCONF
chown 1001:1001 /etc/bubble/gh/config.yml

# Tell gh that github.com is a known host.  Without this, overriding
# GH_CONFIG_DIR wipes the default host list and gh can't resolve repos
# from git remotes ("none of the git remotes ... point to a known GitHub
# host").  GH_TOKEN handles auth; this just registers the host.
cat > /etc/bubble/gh/hosts.yml <<'GHHOSTS'
github.com:
  git_protocol: https
GHHOSTS
chown 1001:1001 /etc/bubble/gh/hosts.yml

# Install a wrapper at /usr/local/bin/gh that (1) ensures the bubble gh
# environment is set even in non-login shells where /etc/profile.d/
# isn't sourced, and (2) lazily starts the unix→TCP forwarder that
# backs gh's http_unix_socket. GH_REPO tells gh which repo to target
# (bypassing remote URL parsing which fails due to url.insteadOf);
# GH_CONFIG_DIR + GH_TOKEN configure proxy auth.
cat > /usr/local/bin/gh <<'WRAPPER'
#!/bin/bash
# Source the bubble gh environment if not already set.
# Non-login shells skip /etc/profile.d/, so we source it here.
if [ -z "$GH_CONFIG_DIR" ] && [ -f /etc/profile.d/bubble-gh.sh ]; then
    . /etc/profile.d/bubble-gh.sh
fi
# Fallback: read GH_REPO from file if still unset
if [ -z "$GH_REPO" ] && [ -f /etc/bubble/gh/repo ]; then
    export GH_REPO="$(cat /etc/bubble/gh/repo)"
fi

# Ensure the unix→TCP forwarder backing gh's http_unix_socket is up.
# gh only speaks to a Unix socket, but the auth proxy is a TCP listener
# on the incus bridge. socat bridges the two — entirely inside this
# container, as this (unprivileged) user, so there's no incus proxy
# device / forkproxy. The bridge endpoint is recorded by bubble at
# /etc/bubble/gh/bridge.
_sock=/home/user/.bubble/gh.sock
if [ -f /etc/bubble/gh/bridge ] && command -v socat >/dev/null 2>&1; then
    if ! { [ -S "$_sock" ] && socat -u OPEN:/dev/null UNIX-CONNECT:"$_sock" 2>/dev/null; }; then
        rm -f "$_sock"
        mkdir -p "$(dirname "$_sock")"
        _ep="$(cat /etc/bubble/gh/bridge)"
        setsid socat "UNIX-LISTEN:$_sock,fork,mode=0600" "TCP:${_ep}" \
            >/dev/null 2>&1 < /dev/null &
        # Wait briefly for the socket to appear.
        for _ in $(seq 1 50); do [ -S "$_sock" ] && break; sleep 0.1; done
    fi
fi
exec /usr/bin/gh "$@"
WRAPPER
chmod 755 /usr/local/bin/gh

echo "gh installed: $(/usr/bin/gh --version 2>/dev/null | head -1 || echo 'unknown version')"
