# bubble Architecture Guide

This file helps Claude Code sessions understand the bubble codebase.

## What This Project Is

`bubble` provides containerized development environments via Incus containers. The primary interface is URL-based: `bubble <github-url>` creates (or re-attaches to) an isolated container and opens it in your preferred editor (VSCode Remote SSH by default, with Emacs, Neovim, and plain SSH also supported). Language-specific hooks (currently Lean 4) auto-detect the project type and select the right image. Bubbles can run locally or on a remote SSH host.

## Package Structure

```
bubble/
├── cli.py              # Click CLI with BubbleGroup (routes unknown args to `open` command)
├── config.py           # TOML config at ~/.bubble/config.toml
├── target.py           # Target parsing: GitHub URLs, local paths, bare PR numbers
├── repo_registry.py    # Learned short name → owner/repo mappings (~/.bubble/repos.json)
├── naming.py           # Container name generation: <repo>-<source>-<id>
├── git_store.py        # Shared bare repo management at ~/.bubble/git/
├── clean.py            # Container cleanness checking (safe to discard?)
├── lifecycle.py        # Registry tracking for active bubbles
├── network.py          # Network allowlisting via iptables inside containers
├── vscode.py           # SSH config generation + editor launching (VSCode, Emacs, Neovim, shell)
├── automation.py       # Periodic jobs: launchd (macOS), systemd (Linux)
├── relay.py            # Bubble-in-bubble relay daemon (Unix socket, validation, rate limiting)
├── remote.py           # Remote SSH host support: run bubbles on remote machines
├── cloud.py            # Hetzner Cloud auto-provisioning (provision, destroy, start, stop)
├── hooks/
│   ├── __init__.py     # Hook ABC, discover_hooks(), select_hook()
│   └── lean.py         # LeanHook: detects lean-toolchain, uses lean image
├── runtime/
│   ├── base.py         # Abstract ContainerRuntime interface
│   ├── incus.py        # IncusRuntime: shells out to `incus` CLI
│   └── colima.py       # macOS: ensure Colima VM is running with correct resources
├── images/
│   ├── builder.py      # Image build via IMAGES registry dict (recursive parent building)
│   └── scripts/
│       ├── base.sh     # Ubuntu 24.04 + git + ssh + build-essential (user: "user")
│       ├── lean.sh     # elan + VS Code Lean extension (derives from base, no toolchains)
│       └── lean-toolchain.sh  # Installs one specific Lean toolchain (for versioned images)
```

## Key Design Decisions

### URL-First Interface
The primary command is `bubble <target>`. A custom `BubbleGroup(click.Group)` routes any unknown first argument to the implicit `open` command. Targets are parsed by `target.py` into a `Target(owner, repo, kind, ref, local_path)` dataclass. Supported target forms:
- GitHub URLs: `https://github.com/owner/repo/pull/123`
- Shorthand: `owner/repo`, `mathlib4/pull/123`, `mathlib4`
- Local paths: `.`, `./path`, `/absolute/path` (extracts owner/repo from git remote)
- Bare PR numbers: `123` (uses current directory's repo)
- `--path` flag for disambiguation: `bubble --path mydir`

Short names are resolved via `RepoRegistry`, which learns mappings automatically on first use. Local paths use the local `.git` as the `--reference` source for fast cloning, and support unpushed branches by fetching refs from the mounted local repo.

### Language Hooks
The `hooks/` package provides a pluggable system for language-specific behavior. Each `Hook` subclass implements `detect()` (check bare repo for language markers), `image_name()`, `post_clone()`, `network_domains()`, and `shared_mounts()`. Hook detection runs against the host bare repo via `git show <ref>:<file>` — no container needed. The `shared_mounts()` method returns `(host_dir_name, container_path, env_var)` tuples for writable mounts shared across containers (e.g., Lean's mathlib cache at `~/.bubble/mathlib-cache/`).

### Runtime Abstraction
`ContainerRuntime` (base.py) is an abstract interface. `IncusRuntime` is the only implementation today. Docker/Podman support is a stretch goal — the abstraction exists to make that possible without refactoring.

### Git Object Sharing
The core performance optimization. Host maintains bare mirror repos (`git clone --bare`). Containers clone with `git clone --reference /shared/git/repo.git url` — git alternates share immutable objects. Each container has fully independent refs/branches/working tree. `update_all_repos()` discovers repos from the `~/.bubble/git/*.git` directory listing.

### Image Registry
Images are defined in `builder.py`'s `IMAGES` dict with script and parent references. Building is recursive — if a parent image is missing, it's built first. Static images: `base` (from Ubuntu 24.04) and `lean` (from base, elan + VS Code extension only, no toolchains).

### Lazy Lean Toolchain Images
The `lean` image has only elan (no toolchains pre-installed). When `LeanHook` detects a project, it reads `lean-toolchain` and parses the version. For stable/RC versions (v4.X.Y, v4.X.Y-rcK), it requests image `lean-v4.X.Y`. If that image exists, it's used directly. If not, the plain `lean` image is used (elan downloads the toolchain on demand) and a background build of the versioned image is triggered for next time. Dynamic images are built via `build_lean_toolchain_image()` in `builder.py`. Nightlies and custom toolchains always use the plain `lean` image.

### Colima on macOS
Incus requires Linux. On macOS, Colima runs a lightweight Linux VM with Apple's Virtualization.Framework (`--vm-type vz`). The `ensure_colima()` function starts it if needed.

### SSH via ProxyCommand
Each container runs sshd. Rather than port forwarding (which doesn't work well through Colima on macOS), we use `ProxyCommand incus exec <name> -- su - user -c "nc localhost 22"`. SSH config entries are auto-generated in `~/.ssh/config.d/bubble`.

### Container Naming
Names are `<repo>-<source>-<id>` (e.g., `mathlib4-pr-12345`). Numeric suffix for collisions. The `open` command checks for existing containers by generated name and registry lookup before creating new ones.

### Container Lifecycle
```
created → running ⇄ paused → destroyed
```

### Network Allowlisting
Uses iptables rules inside containers (not Incus ACLs) for portability across Colima/native setups. IPv6 is blocked entirely. DNS restricted to container resolver only. No outbound SSH. Base allowlist comes from config.toml; hooks contribute additional domains (e.g., Lean adds `releases.lean-lang.org`).

### Editor Selection
The default editor is VSCode via Remote SSH, but users can choose Emacs (TRAMP), Neovim (over SSH), or a plain SSH shell. Set per-invocation with `--emacs`, `--neovim`, `--shell`, or `--editor <choice>`. Set the persistent default with `bubble editor <choice>` (stored as `editor` key in `config.toml`). The `open_editor()` function in `vscode.py` dispatches to the appropriate launcher.

### Remote SSH Hosts
Bubbles can run on a remote machine instead of locally. The `--ssh HOST` flag (or a configured `[remote] default_host`) causes `bubble open` to SSH to the remote, run `bubble open --machine-readable` there, then set up a chained SSH ProxyCommand locally. The `--local` flag overrides a configured default. Remote bubble lifecycle commands (`pause`, `destroy`) auto-route to the correct host via the local registry. Code is in `remote.py`.

### Hetzner Cloud Support
`bubble open --cloud <target>` auto-provisions a Hetzner Cloud server as a remote host. A single server runs Incus and hosts multiple containers, reusing the existing `remote.py` infrastructure. Code is in `cloud.py`.

**Flow:** `--cloud` flag → `cloud.get_cloud_remote_host(config)` → loads `~/.bubble/cloud.json` for existing server → if stopped, auto-starts → returns `RemoteHost(hostname=ipv4, user="root")` → existing `_open_remote()` flow handles the rest.

**Provisioning:** `bubble cloud provision --type ccx43` creates a server with cloud-init that installs Incus via Zabbly, runs `incus admin init --auto`, and installs an idle auto-shutdown timer. Bubble generates its own ed25519 SSH keypair at `~/.bubble/cloud_key` (isolated from `~/.ssh/`).

**Idle auto-shutdown:** A systemd timer checks every 5 minutes for SSH connections and CPU load. After 15 minutes with no SSH connections and low CPU, the server shuts down (stops Hetzner billing). Containers survive shutdown — they're still on disk when the server restarts. Running containers do NOT prevent shutdown; only active SSH sessions and high CPU load do.

**Priority chain for remote host resolution:** `--local` > `--ssh HOST` > `--cloud` > `[cloud] default` > `[remote] default_host`

**State:** `~/.bubble/cloud.json` tracks server ID, IP, SSH key ID. Token comes from `HETZNER_TOKEN` env var (never stored).

### Security Model
The `user` account has no sudo and a locked password. Network allowlisting is applied on container creation. SSH keys are injected via `incus file push` (not shell interpolation). All user-supplied values in shell commands are quoted with `shlex.quote()`. Each container mounts only its specific bare repo, not the entire git store.

## How to Add a New Language Hook

1. Create `bubble/hooks/<language>.py` with a class extending `Hook`
2. Implement `detect()` to check for language markers in the bare repo
3. Implement `image_name()` to return the image to use
4. Add image script in `images/scripts/<name>.sh` and entry in `builder.py`'s `IMAGES` dict
5. Register the hook in `hooks/__init__.py`'s `discover_hooks()`

## Running Tests

Always use `uv run pytest` to run tests (not bare `pytest` or `python3 -m pytest`).

## How to Add a New Command

1. Add a `@main.command()` function in `cli.py`
2. Load config with `load_config()`, get runtime with `get_runtime(config)`
3. Use `runtime.exec()`, `runtime.launch()`, etc. for container operations

## Data Locations

- `~/.bubble/config.toml` — user settings
- `~/.bubble/git/` — bare repo mirrors
- `~/.bubble/repos.json` — learned repo short name mappings
- `~/.bubble/registry.json` — bubble state tracking
- `~/.bubble/relay.sock` — relay daemon Unix socket (when enabled)
- `~/.bubble/relay-tokens.json` — relay auth tokens per container
- `~/.bubble/relay.log` — relay request log
- `~/.bubble/mathlib-cache/` — shared writable mathlib cache (mounted into Lean containers)
- `~/.bubble/vscode-commit` — VS Code commit hash baked into current base image
- `~/.bubble/cloud.json` — Hetzner Cloud server state (ID, IP, SSH key ID)
- `~/.bubble/cloud_key` — SSH private key for cloud server (ed25519, mode 0600)
- `~/.bubble/known_hosts` — SSH known_hosts for cloud server (isolated from ~/.ssh/)
- `~/.ssh/config.d/bubble` — auto-managed SSH config

## Automation

Automation is installed automatically on first bubble creation. On macOS, launchd jobs:
- `com.bubble.git-update` — hourly git store refresh
- `com.bubble.image-refresh` — weekly base image rebuild
- `com.bubble.relay-daemon` — persistent relay daemon (installed via `bubble relay enable`)

On Linux, equivalent systemd user timers/services are installed.

## Bubble-in-Bubble Relay

The relay allows running `bubble` from inside a container. Architecture:

```
Container                              Host
────────                              ────
/usr/local/bin/bubble (stub)          bubble relay daemon
  → /bubble/relay.sock (Incus proxy)  ← ~/.bubble/relay.sock
  sends {"target": "..."}              validates, rate-limits, logs
  reads {"status": "ok", ...}          calls bubble open
```

- Opt-in via `bubble relay enable` (installs daemon, sets config)
- Security: known repos only (`~/.bubble/git/` must exist), no local paths, rate limited (3/min, 10/10min, 20/hr per container), all requests logged
- Container identifies itself via `/bubble/container-id` file
- Relay daemon runs as launchd (macOS) or systemd (Linux) service
- Code: `bubble/relay.py` (daemon + validation), `bubble/images/scripts/base.sh` (stub + client)

## Testing Bubbles

**Never run `bubble` with `--no-interactive` on the user's behalf.** When the user wants to test bubble, tell them the command and let them run it themselves. The user wants to see the live output in their terminal and interact with the result (VS Code window, SSH session). Running it non-interactively from a tool call hides the output and wastes time.

## VS Code Integration Notes

### Workspace Trust
The workspace trust dialog is a **local VS Code client** decision, not controlled by remote server settings. We pass `--disable-workspace-trust` when launching VS Code in `open_vscode()`. Writing to `.vscode-server/data/Machine/settings.json` or `User/settings.json` inside the container does NOT suppress the trust prompt.

### Clearing Trust State for Testing
VS Code stores trusted workspace URIs in a SQLite database. To clear bubble-related trust entries:
```python
import json, sqlite3
db = "/Users/kim/Library/Application Support/Code/User/globalStorage/state.vscdb"
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("SELECT value FROM ItemTable WHERE key = 'content.trust.model.key'")
data = json.loads(cur.fetchone()[0])
data["uriTrustInfo"] = [e for e in data["uriTrustInfo"] if "bubble-" not in e["uri"].get("authority", "")]
cur.execute("UPDATE ItemTable SET value = ? WHERE key = 'content.trust.model.key'", [json.dumps(data)])
conn.commit()
```
VS Code must be restarted after modifying this database.

### Pre-baked VS Code Server
The base image pre-installs the VS Code Server binary matching the host's `code --version` commit hash. On each `bubble open`, if the hash has changed (VS Code updated), a background `bubble images build base` is triggered. The current bubble proceeds immediately; the next one gets the pre-baked server.

## Changelog

`CHANGELOG.md` in the project root tracks user-visible changes by version. When making changes that will be released, add a brief entry under the current version heading. When tagging a new release, add a new version heading.

## PyPI Publishing

The package is published to PyPI as **`dev-bubble`** (the CLI command is still `bubble`). Users install with `pipx install dev-bubble` or `uv tool install dev-bubble`.

### Releasing a New Version

When making changes that warrant a release (new features, bug fixes, improvements), create a new version tag:

1. Bump `version` in `pyproject.toml` (use semver: patch for fixes, minor for features)
2. Commit the version bump
3. Tag and push:
   ```bash
   git tag v0.X.Y
   git push origin v0.X.Y
   ```

The `.github/workflows/publish.yml` workflow runs tests then publishes to PyPI automatically via trusted publishing (no API tokens needed).

### When to Release

**Tag a release every time you push a new feature or significant bug fix.** Don't let changes accumulate unreleased — small frequent releases are preferred. After completing work that changes user-visible behavior:

1. Bump `version` in `pyproject.toml` (patch for fixes, minor for features)
2. Commit the version bump
3. `git tag v0.X.Y && git push origin v0.X.Y`

This is part of the normal workflow, not a separate step to remember later.

**Prefer patch versions.** Use patch bumps (0.X.Y → 0.X.Y+1) for most changes, including small features. Reserve minor bumps (0.X → 0.X+1) for large architectural changes or breaking changes. When in doubt, use a patch bump.

### Trusted Publisher Setup

PyPI is configured to trust GitHub Actions from `kim-em/bubble` with the `publish.yml` workflow and `pypi` environment. No secrets or API tokens are stored in the repository.
