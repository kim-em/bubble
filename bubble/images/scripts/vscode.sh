#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Pre-install VS Code Server if commit hash was provided at build time
if [ -n "${VSCODE_COMMIT:-}" ]; then
    echo "Installing VS Code Server (commit: $VSCODE_COMMIT)..."
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in
        amd64) VSCODE_ARCH="x64" ;;
        arm64) VSCODE_ARCH="arm64" ;;
        *) VSCODE_ARCH="$ARCH" ;;
    esac
    SERVER_URL="https://update.code.visualstudio.com/commit:${VSCODE_COMMIT}/server-linux-${VSCODE_ARCH}/stable"
    SERVER_DIR="/home/user/.vscode-server/cli/servers/Stable-${VSCODE_COMMIT}/server"
    su - user -c "mkdir -p '$SERVER_DIR' && curl -sSL '$SERVER_URL' | tar xz -C '$SERVER_DIR' --strip-components=1" \
        || echo "Warning: failed to pre-install VS Code Server"
fi

# If elan is installed, this is a Lean image — install Lean VS Code extensions
if [ -d /home/user/.elan ]; then

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

# Create bubble-lean-cache extension: opens a terminal to run build commands
BUBBLE_EXT_DIR="/home/user/.vscode-server/extensions/bubble.lean-cache-0.1.0"
mkdir -p "$BUBBLE_EXT_DIR"

cat > "$BUBBLE_EXT_DIR/package.json" << 'EXTJSON'
{
    "name": "lean-cache",
    "displayName": "Bubble Lean Cache",
    "publisher": "bubble",
    "version": "0.1.0",
    "engines": { "vscode": "^1.80.0" },
    "activationEvents": ["onStartupFinished"],
    "main": "./extension.js"
}
EXTJSON

cat > "$BUBBLE_EXT_DIR/extension.js" << 'EXTJS'
const vscode = require('vscode');
const fs = require('fs');
const path = require('path');

function activate() {
    const marker = path.join(require('os').homedir(), '.bubble-fetch-cache');
    if (!fs.existsSync(marker)) return;
    let cmd;
    try { cmd = fs.readFileSync(marker, 'utf8').trim(); } catch (_) { return; }
    if (!cmd) return;
    const folders = vscode.workspace.workspaceFolders;
    if (!folders || folders.length === 0) return;
    try { fs.unlinkSync(marker); } catch (_) {}
    const terminal = vscode.window.createTerminal({
        name: 'Build',
        cwd: folders[0].uri,
    });
    terminal.show();
    terminal.sendText(cmd);
}

function deactivate() {}
module.exports = { activate, deactivate };
EXTJS

# Register bubble extension in extensions.json
python3 -c '
import json, os
ext_dir = "/home/user/.vscode-server/extensions"
manifest_path = os.path.join(ext_dir, "extensions.json")
entries = []
if os.path.exists(manifest_path):
    with open(manifest_path) as f:
        entries = json.load(f)
entries.append({
    "identifier": {"id": "bubble.lean-cache"},
    "version": "0.1.0",
    "location": {
        "$mid": 1,
        "path": os.path.join(ext_dir, "bubble.lean-cache-0.1.0"),
        "scheme": "file",
    },
    "relativeLocation": "bubble.lean-cache-0.1.0",
    "metadata": {},
})
with open(manifest_path, "w") as f:
    json.dump(entries, f)
print("  Registered bubble.lean-cache extension")
'

fi  # end of Lean extensions conditional

# Fix ownership (script runs as root, extension dir must be owned by user)
if [ -d /home/user/.vscode-server ]; then
    chown -R user:user /home/user/.vscode-server
fi

echo "VS Code setup complete."
