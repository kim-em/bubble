#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Install elan as user
su - user -c 'curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | bash -s -- -y --default-toolchain none'

# Add elan to PATH for all sessions
echo 'export PATH="$HOME/.elan/bin:$PATH"' >> /home/user/.bashrc
echo 'export PATH="/home/user/.elan/bin:$PATH"' >> /etc/profile.d/elan.sh

# Pre-install VS Code Lean 4 extension so it's ready on first connect
apt-get update -qq && apt-get install -y -qq python3 unzip < /dev/null

echo "Installing VS Code Lean 4 extension..."
python3 -c '
import json, urllib.request, os, sys, subprocess, tempfile, glob

EXTENSIONS_DIR = "/home/user/.vscode-server/extensions"

# Query marketplace for the latest VSIX download URL
query = json.dumps({
    "filters": [{"criteria": [{"filterType": 7, "value": "leanprover.lean4"}]}],
    "flags": 914,
}).encode()
req = urllib.request.Request(
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery",
    data=query,
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json;api-version=3.0-preview.1",
    },
)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
except Exception as e:
    print(f"Warning: could not query marketplace: {e}", file=sys.stderr)
    sys.exit(0)

# Find the VSIX URL and version
vsix_url = None
version = None
for ext in data["results"][0]["extensions"]:
    for ver in ext["versions"][:1]:
        version = ver["version"]
        for f in ver["files"]:
            if f["assetType"] == "Microsoft.VisualStudio.Services.VSIXPackage":
                vsix_url = f["source"]
                break

if not vsix_url:
    print("Warning: could not find VSIX download URL", file=sys.stderr)
    sys.exit(0)

# Remove any old versions
for old in glob.glob(os.path.join(EXTENSIONS_DIR, "leanprover.lean4-*")):
    import shutil
    shutil.rmtree(old)

# Download and extract (unzip preserves file permissions)
ext_dir = os.path.join(EXTENSIONS_DIR, f"leanprover.lean4-{version}")
os.makedirs(ext_dir, exist_ok=True)

with tempfile.NamedTemporaryFile(suffix=".vsix", delete=False) as tmp:
    tmp_path = tmp.name

try:
    print(f"  Downloading leanprover.lean4 v{version}...")
    urllib.request.urlretrieve(vsix_url, tmp_path)
    subprocess.run(
        ["unzip", "-q", "-o", tmp_path, "extension/*", "-d", ext_dir],
        check=True,
    )
    # Move contents from extension/ subdirectory up to ext_dir
    nested = os.path.join(ext_dir, "extension")
    if os.path.isdir(nested):
        for item in os.listdir(nested):
            os.rename(os.path.join(nested, item), os.path.join(ext_dir, item))
        os.rmdir(nested)
    # Write extensions.json so VS Code recognizes the pre-installed extension
    rel_location = f"leanprover.lean4-{version}"
    manifest = [
        {
            "identifier": {"id": "leanprover.lean4"},
            "version": version,
            "location": {
                "$mid": 1,
                "path": os.path.join(EXTENSIONS_DIR, rel_location),
                "scheme": "file",
            },
            "relativeLocation": rel_location,
            "metadata": {},
        }
    ]
    with open(os.path.join(EXTENSIONS_DIR, "extensions.json"), "w") as mf:
        json.dump(manifest, mf)
    print(f"  Installed to {ext_dir}")
finally:
    os.unlink(tmp_path)
'

# Fix ownership (script runs as root, extension dir must be owned by user)
if [ -d /home/user/.vscode-server ]; then
    chown -R user:user /home/user/.vscode-server
fi

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Lean image setup complete."
