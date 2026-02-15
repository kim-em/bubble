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

echo "Installing VS Code extensions for Lean 4..."
python3 -c '
import json, urllib.request, os, sys, subprocess, tempfile, glob, shutil

EXTENSIONS_DIR = "/home/user/.vscode-server/extensions"
EXTENSIONS = ["leanprover.lean4", "tamasfe.even-better-toml"]

manifest_entries = []

for ext_id in EXTENSIONS:
    # Query marketplace for the latest VSIX download URL
    query = json.dumps({
        "filters": [{"criteria": [{"filterType": 7, "value": ext_id}]}],
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
        print(f"Warning: could not query marketplace for {ext_id}: {e}", file=sys.stderr)
        continue

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
        print(f"Warning: could not find VSIX download URL for {ext_id}", file=sys.stderr)
        continue

    # Remove any old versions
    for old in glob.glob(os.path.join(EXTENSIONS_DIR, f"{ext_id}-*")):
        shutil.rmtree(old)

    # Download and extract
    ext_dir = os.path.join(EXTENSIONS_DIR, f"{ext_id}-{version}")
    os.makedirs(ext_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".vsix", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        print(f"  Downloading {ext_id} v{version}...")
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
        print(f"  Installed to {ext_dir}")

        rel_location = f"{ext_id}-{version}"
        manifest_entries.append({
            "identifier": {"id": ext_id},
            "version": version,
            "location": {
                "$mid": 1,
                "path": os.path.join(EXTENSIONS_DIR, rel_location),
                "scheme": "file",
            },
            "relativeLocation": rel_location,
            "metadata": {},
        })
    finally:
        os.unlink(tmp_path)

# Write extensions.json with all installed extensions
if manifest_entries:
    os.makedirs(EXTENSIONS_DIR, exist_ok=True)
    with open(os.path.join(EXTENSIONS_DIR, "extensions.json"), "w") as mf:
        json.dump(manifest_entries, mf)
    print(f"  Registered {len(manifest_entries)} extensions in extensions.json")
'

# Fix ownership (script runs as root, extension dir must be owned by user)
if [ -d /home/user/.vscode-server ]; then
    chown -R user:user /home/user/.vscode-server
fi

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Lean image setup complete."
