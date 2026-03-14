# bubble Architecture Guide

This file helps Claude Code sessions understand the bubble codebase.

## What This Project Is

`bubble` provides containerized development environments via Incus containers. The primary interface is URL-based: `bubble <github-url>` creates (or re-attaches to) an isolated container and opens it in VSCode via Remote SSH (or a plain SSH shell with `--shell`). Language-specific hooks (currently Lean 4) auto-detect the project type and select the right image. Bubbles can run locally or on a remote SSH host.

## Package Structure

```
bubble/
├── cli.py              # Click CLI with BubbleGroup (routes unknown args to `open` command)
├── config.py           # TOML config at ~/.bubble/config.toml
├── target.py           # Target parsing: GitHub URLs, issues, local paths, bare PR/issue numbers
├── repo_registry.py    # Learned short name → owner/repo mappings (~/.bubble/repos.json)
├── naming.py           # Container name generation: <repo>-<source>-<id>
├── git_store.py        # Shared bare repo management at ~/.bubble/git/
├── clean.py            # Container cleanness checking (safe to discard?)
├── lifecycle.py        # Registry tracking for active bubbles
├── network.py          # Network allowlisting via iptables inside containers
├── vscode.py           # SSH config generation + editor launching (VSCode, SSH shell)
├── automation.py       # Periodic jobs: launchd (macOS), systemd (Linux)
├── relay.py            # Bubble-in-bubble relay daemon (Unix socket, validation, rate limiting)
├── auth_proxy.py       # HTTP reverse proxy for repo-scoped GitHub auth (token stays on host)
├── graphql_validator.py # GraphQL tokenizer, parser, and allowlist validation for auth proxy
├── tunnel.py           # SSH reverse tunnel management for remote auth proxy access
├── remote.py           # Remote SSH host support: run bubbles on remote machines
├── cloud.py            # Hetzner Cloud auto-provisioning (provision, destroy, start, stop)
├── claude.py           # Claude Code integration: prompt generation, VS Code task injection
├── tools.py            # Pluggable tool installation: registry, resolution, hash computation
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
│       ├── lean.sh     # Fallback elan install (skipped if elan tool already installed)
│       ├── lean-toolchain.sh  # Installs one specific Lean toolchain (for versioned images)
│       └── tools/      # Per-tool install scripts (claude.sh, codex.sh, elan.sh, emacs.sh, neovim.sh, vscode.sh)
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
Images are defined in `builder.py`'s `IMAGES` dict with script and parent references. Building is recursive — if a parent image is missing, it's built first. There are only two static images: `base` (from Ubuntu 24.04) and `lean` (from base, fallback elan install). Editors and language tools are installed as pluggable tools on the base image, eliminating editor-specific image variants.

### Pluggable Tool Installation
Tools are installed in container images via the `[tools]` config section. Each tool has a self-contained install script in `bubble/images/scripts/tools/` and is registered in the `TOOLS` dict in `tools.py` with its script filename, host detection command, required network domains, and a priority for install ordering. Tools include:

- **Language tools** (priority 10): `elan` — auto-detected if elan is on the host
- **General tools** (priority 50): `claude`, `codex`, `gh` — auto-detected via host commands
- **Editors** (priority 90): `vscode`, `emacs`, `neovim` — driven by the `editor` config key

Config values are `"yes"`, `"no"`, or `"auto"` (default). Editor tools are special: the configured editor (default: vscode) is treated as `"yes"` unless explicitly `"no"` in `[tools]`. Tools are installed into the `base` image during `build_image("base")` in priority order (language tools before editors, so vscode can detect elan and install Lean extensions). When the resolved tool set changes (detected via a content-aware hash stored in `~/.bubble/tools-hash`), the `base` image is rebuilt synchronously, and stale derived images are purged.

### Lazy Lean Toolchain Images
The `lean` image provides elan as a fallback for users who don't have elan on their host. When `LeanHook` detects a project, it reads `lean-toolchain` and parses the version. For stable/RC versions (v4.X.Y, v4.X.Y-rcK), it requests image `lean-v4.X.Y`. If that image exists, it's used directly. If not, the plain `lean` image is used (elan downloads the toolchain on demand) and a background build of the versioned image is triggered for next time. Dynamic images are built via `build_lean_toolchain_image()` in `builder.py`. Nightlies and custom toolchains always use the plain `lean` image.

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
Uses iptables rules inside containers (not Incus ACLs) for portability across Colima/native setups. IPv6 is blocked entirely. DNS restricted to container resolver only. No outbound SSH. Base allowlist comes from config.toml; hooks contribute additional domains (e.g., Lean adds `releases.lean-lang.org`); enabled tools contribute runtime domains (e.g., vscode adds marketplace.visualstudio.com).

### Editor Selection
The default editor is VSCode via Remote SSH. Use `--shell` for a plain SSH session, `--emacs` or `--neovim` for those editors. The editor is installed as a tool in the base image (so it's pre-baked, not installed per-container). The `open_editor()` function in `vscode.py` dispatches to the appropriate launcher.

### Remote SSH Hosts
Bubbles can run on a remote machine instead of locally. The `--ssh HOST` flag (or a configured `[remote] default_host`) causes `bubble open` to SSH to the remote, run `bubble open --machine-readable` there, then set up a chained SSH ProxyCommand locally. The `--local` flag overrides a configured default. Remote bubble lifecycle commands (`pause`, `pop`) auto-route to the correct host via the local registry. Code is in `remote.py`.

### Hetzner Cloud Support
`bubble open --cloud <target>` auto-provisions a Hetzner Cloud server as a remote host. A single server runs Incus and hosts multiple containers, reusing the existing `remote.py` infrastructure. Code is in `cloud.py`.

**Flow:** `--cloud` flag → `cloud.get_cloud_remote_host(config)` → loads `~/.bubble/cloud.json` for existing server → if stopped, auto-starts → returns `RemoteHost(hostname=ipv4, user="root")` → existing `_open_remote()` flow handles the rest.

**Provisioning:** `bubble cloud provision --type ccx43` creates a server with cloud-init that installs Incus via Zabbly, runs `incus admin init --auto`, and installs an idle auto-shutdown timer. Bubble generates its own ed25519 SSH keypair at `~/.bubble/cloud_key` (isolated from `~/.ssh/`).

**Idle auto-shutdown:** A systemd timer checks every 5 minutes for SSH connections and CPU load. After 15 minutes with no SSH connections and low CPU, the server shuts down (stops Hetzner billing). Containers survive shutdown — they're still on disk when the server restarts. Running containers do NOT prevent shutdown; only active SSH sessions and high CPU load do.

**Priority chain for remote host resolution:** `--local` > `--ssh HOST` > `--cloud` > `[cloud] default` > `[remote] default_host`

**State:** `~/.bubble/cloud.json` tracks server ID, IP, SSH key ID. Token comes from `HCLOUD_TOKEN` env var (never stored).

### User Customization Script
Users can place a `customize.sh` script at `~/.bubble/customize.sh` to run custom setup in all container images. The script runs as root as the final step when building any image (base, lean, lean-v4.X.Y). This lets users add tools, dotfiles, shell config, etc. without forking image scripts. The script's content hash is tracked in `~/.bubble/customize-hash`; on `bubble open`, if the hash differs from the stored value, a background rebuild of the base image is triggered (same pattern as VS Code commit hash drift). Code is in `builder.py` (`customize_hash()`, `_run_customize_script()`).

### GitHub Auth Proxy
The auth proxy (`auth_proxy.py`) provides repo-scoped GitHub authentication without injecting the host's token into containers. It's an HTTP reverse proxy that runs on the host. The access level is controlled by the unified `github` security setting (`security.py`), which picks one level from a graduated escalation ladder:

| `github` level | Behavior |
|----------------|----------|
| `off` | no GitHub access at all |
| `basic` | git push/pull only (proxy rewrites, repo-scoped) |
| `rest` | + repo-scoped REST API |
| `allowlist-read-graphql` | + allowlisted GraphQL queries |
| `allowlist-write-graphql` | + allowlisted GraphQL mutations (default) |
| `write-graphql` | + arbitrary GraphQL, no allowlist filtering |
| `direct` | inject the raw token, no proxy |

`auto` defaults to `allowlist-write-graphql`. The old `github-auth`, `github-api`, and `github-token-inject` settings are deprecated but migrated automatically.

**Git flow:** Container git → `url.insteadOf` rewrites to `http://127.0.0.1:7654/git/...` → proxy validates `X-Bubble-Token` header → checks path matches allowed `owner/repo` → adds `Authorization: token <real-token>` → forwards to `https://github.com` → returns response.

**gh CLI flow:** `gh` configured with `http_unix_socket: /bubble/gh-proxy.sock` (via `GH_CONFIG_DIR=/etc/bubble/gh`) → sends requests through Unix socket → proxy validates token from `Authorization` header → enforces access level (REST repo-scoping, GraphQL mutation filtering) → adds real token → forwards to `https://api.github.com`.

**GraphQL validation** (`graphql_validator.py`): GraphQL access is controlled by two independent axes — `graphql_read` and `graphql_write` — each supporting `whitelisted`, `unrestricted`, or `none` modes. The default is `whitelisted` for both. In whitelisted mode, a lightweight tokenizer/parser validates structure (single operation, single top-level field, no aliases/directives, no fragments in mutations) and semantics. Read validation repo-scopes `repository` queries via variables, verifies `node` queries via pre-flight ownership checks, and checks second-level fields against an allowlist. Write validation checks mutations against an allowlist (createPullRequest, addComment, mergePullRequest, etc.) with repo-scoping via repositoryId comparison or pre-flight node ownership verification.

**REST security:** REST paths validated against `/repos/{owner}/{repo}/...` (repo-scoped). All HTTP methods are allowed when REST access is enabled, since path validation already constrains access to the scoped repo. API redirects (e.g. CI log downloads) followed with hardened rules: GET/HEAD only, HTTPS only, allowlisted hosts, max 2 hops, auth headers stripped. GitHub 4xx errors are passed through to clients (not collapsed to 502).

**Local bubbles:** Exposed via Incus proxy devices — TCP for git, Unix socket for gh (`listen=unix:/bubble/gh-proxy.sock`).

**Remote/cloud bubbles:** SSH reverse tunnel forwards the local proxy port. Incus proxy devices on the remote expose both TCP and Unix socket endpoints.

**Token management:** Per-container tokens in `~/.bubble/auth-tokens.json` map to `{container, owner, repo, rest_api, graphql_read, graphql_write}`. Tokens are cleaned up on `bubble pop`. The daemon is managed via launchd/systemd.

### Security Model
The `user` account has no sudo and a locked password. Network allowlisting is applied on container creation. SSH keys are injected via `incus file push` (not shell interpolation). All user-supplied values in shell commands are quoted with `shlex.quote()`. Each container mounts only its specific bare repo, not the entire git store.

## How to Add a New Language Hook

1. Create `bubble/hooks/<language>.py` with a class extending `Hook`
2. Implement `detect()` to check for language markers in the bare repo
3. Implement `image_name()` to return the image to use
4. Add image script in `images/scripts/<name>.sh` and entry in `builder.py`'s `IMAGES` dict
5. Register the hook in `hooks/__init__.py`'s `discover_hooks()`

## How to Add a New Tool

1. Create `bubble/images/scripts/tools/<name>.sh` — a self-contained install script that runs as root
2. Add an entry to the `TOOLS` dict in `bubble/tools.py` with `script`, `host_cmd`, `network_domains`, `runtime_domains`, and `priority`
3. Test with `bubble tools set <name> yes && bubble images build base`

## Code Quality

Before committing, run `uv run ruff check --fix . && uv run ruff format .` to ensure code passes linting and formatting.

## Running Tests

Always use `uv run pytest` to run tests (not bare `pytest` or `python3 -m pytest`).

**NEVER run `uv pip install -e .` in a worktree.** The `VIRTUAL_ENV` environment variable may point to the main worktree's venv (`~/projects/bubble/.venv`), so `uv pip install` will corrupt it by installing an editable pointing to the wrong directory. Use `uv run` instead — it creates a per-directory `.venv` automatically and ignores mismatched `VIRTUAL_ENV`.

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
- `~/.bubble/tools-hash` — hash of installed tools + script contents (for drift detection)
- `~/.bubble/customize.sh` — user customization script (run as final step in all image builds)
- `~/.bubble/customize-hash` — hash of customize.sh (for drift detection)
- `~/.bubble/auth-tokens.json` — auth proxy token→{container, owner, repo} mapping (mode 0600)
- `~/.bubble/auth-proxy.port` — auth proxy daemon TCP port
- `~/.bubble/auth-proxy.log` — auth proxy request log
- `~/.bubble/tunnels/` — SSH tunnel PID files (keyed by remote host spec)
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
The workspace trust dialog is a **local VS Code client** decision, not controlled by remote server settings. We pass `--disable-workspace-trust` when launching VS Code in `open_vscode()` — this works on the desktop CLI but is unsupported by the VS Code Remote CLI helper, so we suppress stderr to avoid the warning. Writing to `.vscode-server/data/Machine/settings.json` or `User/settings.json` inside the container does NOT suppress the trust prompt.

### Clearing Trust State for Testing
VS Code stores trusted workspace URIs in a SQLite database. To clear bubble-related trust entries:
```python
import json, sqlite3
db = "/Users/<USERNAME>/Library/Application Support/Code/User/globalStorage/state.vscdb"
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
When vscode is enabled as a tool (the default), the base image pre-installs the VS Code Server binary matching the host's `code --version` commit hash. On each `bubble open`, if the hash has changed (VS Code updated), a background `bubble images build base` is triggered. The current bubble proceeds immediately; the next one gets the pre-baked server.

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
