# bubble Architecture Guide

This file helps Claude Code sessions understand the bubble codebase.

## What This Project Is

`bubble` provides containerized development environments via Incus containers. The primary interface is URL-based: `bubble <github-url>` creates (or re-attaches to) an isolated container with VSCode Remote SSH. Language-specific hooks (currently Lean 4) auto-detect the project type and select the right image.

## Package Structure

```
bubble/
├── cli.py              # Click CLI with BubbleGroup (routes unknown args to `open` command)
├── config.py           # TOML config at ~/.bubble/config.toml
├── target.py           # Target parsing: GitHub URLs, local paths, bare PR numbers
├── repo_registry.py    # Learned short name → owner/repo mappings (~/.bubble/repos.json)
├── naming.py           # Container name generation: <repo>-<source>-<id>
├── git_store.py        # Shared bare repo management at ~/.bubble/git/
├── lifecycle.py        # Registry tracking for active bubbles
├── network.py          # Network allowlisting via iptables inside containers
├── vscode.py           # SSH config generation + `code --remote` launching
├── automation.py       # Periodic jobs: launchd (macOS), systemd (Linux)
├── relay.py            # Bubble-in-bubble relay daemon (Unix socket, validation, rate limiting)
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
│       └── lean.sh     # elan + latest stable/RC toolchains (derives from base)
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
The `hooks/` package provides a pluggable system for language-specific behavior. Each `Hook` subclass implements `detect()` (check bare repo for language markers), `image_name()`, `post_clone()`, `vscode_extensions()`, and `network_domains()`. Hook detection runs against the host bare repo via `git show <ref>:<file>` — no container needed.

### Runtime Abstraction
`ContainerRuntime` (base.py) is an abstract interface. `IncusRuntime` is the only implementation today. Docker/Podman support is a stretch goal — the abstraction exists to make that possible without refactoring.

### Git Object Sharing
The core performance optimization. Host maintains bare mirror repos (`git clone --bare`). Containers clone with `git clone --reference /shared/git/repo.git url` — git alternates share immutable objects. Each container has fully independent refs/branches/working tree. `update_all_repos()` discovers repos from the `~/.bubble/git/*.git` directory listing.

### Image Registry
Images are defined in `builder.py`'s `IMAGES` dict with script and parent references. Building is recursive — if a parent image is missing, it's built first. Currently: `base` (from Ubuntu 24.04) and `lean` (from base).

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

### Security Model
The `user` account has no sudo and a locked password. Network allowlisting is applied on container creation. SSH keys are injected via `incus file push` (not shell interpolation). All user-supplied values in shell commands are quoted with `shlex.quote()`. Each container mounts only its specific bare repo, not the entire git store.

## How to Add a New Language Hook

1. Create `bubble/hooks/<language>.py` with a class extending `Hook`
2. Implement `detect()` to check for language markers in the bare repo
3. Implement `image_name()` to return the image to use
4. Add image script in `images/scripts/<name>.sh` and entry in `builder.py`'s `IMAGES` dict
5. Register the hook in `hooks/__init__.py`'s `discover_hooks()`

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
