# lean-bubbles Architecture Guide

This file helps Claude Code sessions understand the lean-bubbles codebase.

## What This Project Is

`lean-bubbles` (CLI: `bubble`) provides containerized Lean 4 development environments via Incus containers. Users create isolated "bubbles" for working on Lean/Mathlib PRs, with VSCode Remote SSH as the primary interface.

## Package Structure

```
lean_bubbles/
├── cli.py              # Click CLI. All commands defined here. Entry point: main()
├── config.py           # TOML config at ~/.lean-bubbles/config.toml
│                       # Also defines KNOWN_REPOS for short name resolution
├── naming.py           # Container name generation: <repo>-<source>-<id>
├── git_store.py        # Shared bare repo management at ~/.lean-bubbles/git/
├── lake_cache.py       # Shared .lake cache volume, keyed by repo+toolchain
├── lifecycle.py        # Archive/reconstitute, registry tracking
├── wrap.py             # `bubble wrap .` — move local working dir into a bubble
├── pr_metadata.py      # PR description HTML comment injection/parsing via `gh`
├── network.py          # Network allowlisting via iptables inside containers
├── vscode.py           # SSH config generation + `code --remote` launching
├── runtime/
│   ├── base.py         # Abstract ContainerRuntime interface
│   ├── incus.py        # IncusRuntime: shells out to `incus` CLI
│   └── colima.py       # macOS: ensure Colima VM is running with correct resources
├── images/
│   ├── builder.py      # Image build orchestration (launch container, run script, publish)
│   └── scripts/        # Shell scripts run inside containers during image build
│       ├── lean-base.sh
│       ├── lean-mathlib.sh
│       ├── lean-batteries.sh
│       └── lean-lean4.sh
└── extensions/         # Optional features
    ├── claude.py       # .jsonl extraction/injection, session persistence
    └── zulip.py        # Sandboxed Zulip access (read as user, write as AI)
```

## Key Design Decisions

### Runtime Abstraction
`ContainerRuntime` (base.py) is an abstract interface. `IncusRuntime` is the only implementation today. Docker/Podman support is a stretch goal — the abstraction exists to make that possible without refactoring.

### Git Object Sharing
The core performance optimization. Host maintains bare mirror repos (`git clone --bare`). Containers clone with `git clone --reference /shared/git/repo.git url` — git alternates share immutable objects. Each container has fully independent refs/branches/working tree.

### Colima on macOS
Incus requires Linux. On macOS, Colima runs a lightweight Linux VM with Apple's Virtualization.Framework (`--vm-type vz`). The `ensure_colima()` function starts it if needed.

### SSH via ProxyCommand
Each container runs sshd. Rather than port forwarding (which doesn't work well through Colima on macOS), we use `ProxyCommand incus exec <name> -- su - lean -c "nc localhost 22"`. SSH config entries are auto-generated in `~/.ssh/config.d/lean-bubbles`.

### Container Naming
Names are `<repo>-<source>-<id>` (e.g., `mathlib4-pr-12345`). Numeric suffix for collisions. Full reconstruction state lives in the registry and PR metadata, not the name.

### Container Lifecycle
```
created → running ⇄ paused → archived → (reconstituted → running)
             │                        → destroyed
             └→ destroyed
```

Archive checks git sync state (uncommitted changes, unpushed commits), extracts Claude sessions, saves metadata to `~/.lean-bubbles/registry.json`, then destroys the container. Reconstitute recreates from base image + saved state.

### Network Allowlisting
Uses iptables rules inside containers (not Incus ACLs) for portability across Colima/native setups. DNS is always allowed. Configurable domain list in config.toml.

### PR Metadata
Session state is stored as an invisible HTML comment in PR descriptions:
```html
<!-- lean-bubbles: {"session_id":"...","branch":"...","commit":"..."} -->
```
This allows `bubble resume <PR-URL>` to reconstitute a session on any machine.

## How to Add a New Command

1. Add a `@main.command()` function in `cli.py`
2. Load config with `load_config()`, get runtime with `get_runtime(config)`
3. Use `runtime.exec()`, `runtime.launch()`, etc. for container operations
4. Use `_ensure_running()`, `_setup_ssh()`, `_detect_project_dir()` helpers

## How to Add a New Base Image

1. Create a script in `images/scripts/<name>.sh`
2. Add the image name to `DERIVED_IMAGES` dict in `images/builder.py`
3. The `build_image()` function handles it automatically

## Data Locations

- `~/.lean-bubbles/config.toml` — user settings
- `~/.lean-bubbles/git/` — bare repo mirrors
- `~/.lean-bubbles/lake-cache/` — shared .lake caches (keyed by repo+toolchain)
- `~/.lean-bubbles/registry.json` — bubble state tracking (active + archived)
- `~/.lean-bubbles/sessions/` — archived Claude .jsonl files
- `~/.ssh/config.d/lean-bubbles` — auto-managed SSH config

## Automation

On macOS, `bubble init` installs launchd jobs:
- `com.lean-bubbles.git-update` — hourly git store refresh
- `com.lean-bubbles.image-refresh` — weekly base image rebuild
