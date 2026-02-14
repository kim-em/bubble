#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Install elan as user
su - user -c 'curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | bash -s -- -y --default-toolchain none'

# Add elan to PATH for all sessions
echo 'export PATH="$HOME/.elan/bin:$PATH"' >> /home/user/.bashrc
echo 'export PATH="/home/user/.elan/bin:$PATH"' >> /etc/profile.d/elan.sh

# Install recent Lean toolchains (latest 2 stable + latest RC for 2 most recent RC versions)
# Uses GitHub API to determine which versions to install
apt-get update -qq && apt-get install -y -qq python3 < /dev/null

python3 -c '
import json, urllib.request, re, sys

url = "https://api.github.com/repos/leanprover/lean4/releases?per_page=50"
try:
    with urllib.request.urlopen(url, timeout=30) as resp:
        releases = json.loads(resp.read())
except Exception as e:
    print(f"Warning: could not fetch releases: {e}", file=sys.stderr)
    sys.exit(0)

stables = []
rcs = {}  # major_minor -> latest_rc_tag

for r in releases:
    tag = r["tag_name"]
    if r["draft"]:
        continue
    if r["prerelease"]:
        # RC release — extract major.minor
        m = re.match(r"v(\d+\.\d+)\.\d+-rc\d+", tag)
        if m:
            major_minor = m.group(1)
            if major_minor not in rcs:
                rcs[major_minor] = tag
    else:
        # Stable release
        if len(stables) < 2:
            stables.append(tag)

# Latest RC for 2 most recent major.minor versions with RCs
rc_versions = sorted(rcs.keys(), key=lambda v: [int(x) for x in v.split(".")], reverse=True)[:2]
rc_tags = [rcs[v] for v in rc_versions]

toolchains = stables + rc_tags
for t in toolchains:
    print(t)
' | while read -r version; do
    echo "Installing toolchain: leanprover/lean4:$version"
    su - user -c "export PATH=\"\$HOME/.elan/bin:\$PATH\" && elan toolchain install \"leanprover/lean4:$version\"" || \
        echo "Warning: failed to install $version"
done

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "bubble-lean setup complete."
