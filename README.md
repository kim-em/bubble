# bubble

Containerized development environments for the Lean language, powered by [Incus](https://linuxcontainers.org/incus/).

## Quick Start

```bash
# Install
uv tool install git+https://github.com/kim-em/bubble.git

# Also available on PyPI: uv tool install dev-bubble
# For development: uv pip install -e '.[dev]'

# Open a bubble for a GitHub PR — just paste the URL, and you get a containerized VSCode window!
bubble https://github.com/leanprover-community/mathlib4/pull/35219

# Shorter forms work too
bubble leanprover-community/mathlib4/pull/35219
bubble mathlib4/pull/35219    # after first use, short names are learned

# Branch or commit
bubble leanprover-community/mathlib4/tree/some-branch
bubble leanprover-community/mathlib4/commit/abc123

# Default branch
bubble leanprover-community/mathlib4

# From a local git repo — opens the current branch in a bubble
bubble .
bubble ./path/to/repo

# PR number shorthand (when in a cloned repo)
bubble 123                   # opens PR #123 for the current repo

# List your bubbles
bubble list

# SSH instead of VSCode
bubble https://github.com/leanprover/lean4 --ssh

# Just create, don't open anything
bubble leanprover/lean4 --no-interactive

# Pause a bubble
bubble pause mathlib4-pr-35219

# Destroy permanently
bubble destroy mathlib4-pr-35219
```

## How It Works

Each "bubble" is a lightweight Linux container (via Incus) with:
- Your project cloned and ready to work on
- SSH server for VSCode Remote connection
- Network restricted to allowed domains only
- Language-specific tooling when detected (e.g. Lean 4 via elan)

**URL-first interface**: The primary command is `bubble <target>`. Targets can be full GitHub URLs, partial URLs, org/repo paths, or learned short names. If a bubble already exists for that target, it re-attaches instead of creating a new one.

**Shared git objects**: A bare mirror of each repo is maintained on the host. Containers clone via `git --reference`, sharing the immutable object store. This means creating a new bubble for a mathlib PR downloads only the few new commits, not the entire 1.5GB repo.

**Language hooks**: bubble automatically detects the project's language and selects the right image. For Lean 4 projects (detected via `lean-toolchain`), the container includes elan, pre-installed VS Code extensions, and auto-downloads the mathlib cache when needed.

**Network allowlisting**: Containers can only reach allowed domains (GitHub by default, plus language-specific domains like `releases.lean-lang.org` for Lean). IPv6 is blocked, DNS is restricted to the container resolver, and outbound SSH is blocked. Configurable in `~/.bubble/config.toml`.

## Requirements

- **macOS**: Homebrew, then `brew install colima incus`
- **Linux**: Incus installed natively ([install guide](https://linuxcontainers.org/incus/docs/main/installing/))
- **VSCode** with [Remote - SSH](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-ssh) extension

## Commands

| Command | Description |
|---------|-------------|
| `bubble <target>` | Open (or create) a bubble for a GitHub URL/repo |
| `bubble list` | List all bubbles |
| `bubble pause <name>` | Freeze a bubble |
| `bubble destroy <name>` | Delete a bubble permanently |
| `bubble cleanup` | Destroy all clean bubbles (no unsaved work) |
| `bubble images list\|build` | Manage base images |
| `bubble git update` | Refresh shared git mirrors |
| `bubble network apply\|remove <name>` | Manage network restrictions |
| `bubble automation install\|remove\|status` | Manage periodic jobs |
| `bubble relay enable\|disable\|status` | Manage bubble-in-bubble relay |
| `bubble doctor` | Diagnose and fix common issues |

## Images

Images are built automatically on first use.

| Image | Contents |
|-------|----------|
| `base` | Ubuntu 24.04, git, openssh-server, build-essential, pre-baked VS Code Server |
| `lean` | base + elan, leantar, VS Code Lean 4 extension, auto-cache extension |
| `lean-v4.X.Y` | lean + specific toolchain pre-installed (built lazily on demand) |

`base` and `lean` are static images you can rebuild with `bubble images build <name>`. Versioned `lean-v4.X.Y` images are built automatically in the background when a project uses a stable/RC toolchain not yet cached — the current bubble proceeds immediately with elan downloading the toolchain on demand, and the next bubble for that version starts instantly.

For mathlib or mathlib-dependent projects, a VS Code terminal automatically runs `lake exe cache get` when the workspace opens.

## Configuration

Config lives at `~/.bubble/config.toml`. Created automatically on first use.

Set `BUBBLE_HOME` to override the data directory (default: `~/.bubble`):
```bash
export BUBBLE_HOME=/data/bubble
```

```toml
[runtime]
backend = "incus"
colima_cpu = 24          # macOS: CPUs for the Colima VM
colima_memory = 16       # macOS: GB of RAM
colima_vm_type = "vz"    # macOS: Apple Virtualization.Framework

[network]
allowlist = [
  "github.com",
  "raw.githubusercontent.com",
  "release-assets.githubusercontent.com",
  "objects.githubusercontent.com",
  "codeload.githubusercontent.com",
]
```

## Bubble-in-Bubble

You can run `bubble` from inside a container to open another bubble on the host. This is useful when reviewing a related PR while working on a feature branch.

```bash
# On the host: enable the relay (one-time setup)
bubble relay enable

# Inside a container: open another bubble
bubble leanprover/lean4/pull/456
bubble mathlib4
```

The relay only allows opening repos already cloned in `~/.bubble/git/` — it cannot trigger cloning of new repos. Local paths are rejected. Existing bubbles need to be recreated after enabling the relay to get the relay socket.

## Security

- **No sudo**: The `user` account has no sudo access and a locked password
- **Network allowlisting**: iptables rules restrict outbound connections to allowed domains only
- **IPv6 blocked**: All IPv6 traffic is dropped
- **DNS restricted**: DNS queries only go to the container's configured resolver
- **No outbound SSH**: Containers cannot SSH out (VSCode uses `incus exec` ProxyCommand)
- **SSH key-only auth**: Password authentication is disabled
- **Shell injection hardening**: All user-supplied values are quoted with `shlex.quote()`
- **Per-repo git mount**: Each container only sees its own bare repo, not the entire git store

## License

Apache 2.0 — see [LICENSE](LICENSE).
