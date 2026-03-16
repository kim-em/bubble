#!/bin/bash
set -euo pipefail

# Install GitHub CLI and set up bubble-managed gh configuration.
# gh is configured to route all HTTP traffic through the auth proxy
# via http_unix_socket, so the host's GitHub token never enters the
# container.
export DEBIAN_FRONTEND=noninteractive

# Install gh from official apt repository
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null

apt-get update -qq
apt-get install -y -qq gh

# Create bubble-managed gh config directory.
# GH_CONFIG_DIR is set at container creation time (not image build time)
# because the auth token is per-container.
mkdir -p /etc/bubble/gh
chown 1001:1001 /etc/bubble/gh

# Pre-populate config.yml with http_unix_socket pointing to the
# auth proxy socket exposed by Incus proxy device.
cat > /etc/bubble/gh/config.yml <<'GHCONF'
version: "1"
http_unix_socket: /bubble/gh-proxy.sock
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

# Install a wrapper at /usr/local/bin/gh that ensures the bubble gh
# environment is set even in non-login shells where /etc/profile.d/
# isn't sourced.  GH_REPO tells gh which repo to target (bypassing
# remote URL parsing which fails due to url.insteadOf), and
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
exec /usr/bin/gh "$@"
WRAPPER
chmod 755 /usr/local/bin/gh

echo "gh installed: $(/usr/bin/gh --version 2>/dev/null | head -1 || echo 'unknown version')"
