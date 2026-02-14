# bubble

Containerized development environments powered by [Incus](https://linuxcontainers.org/incus/).

## Why?

- **Safety**: Run untrusted PRs in isolated containers with network allowlisting
- **Convenience**: Spin up 5-10+ concurrent development sessions without managing multiple clones
- **Speed**: Shared git objects mean new bubbles start in seconds, not minutes
- **Language hooks**: Automatic detection of Lean 4 (and more to come) with pre-configured toolchains

## Quick Start

```bash
pip install bubble

# First-time setup (installs Colima on macOS, builds base image)
bubble init

# Open a bubble for a GitHub PR — just paste the URL
bubble https://github.com/leanprover-community/mathlib4/pull/35219

# Shorter forms work too
bubble leanprover-community/mathlib4/pull/35219
bubble mathlib4/pull/35219    # after first use, short names are learned

# Branch or commit
bubble leanprover-community/mathlib4/tree/some-branch
bubble leanprover-community/mathlib4/commit/abc123

# Default branch
bubble leanprover-community/mathlib4

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

**Language hooks**: bubble automatically detects the project's language and selects the right image. For Lean 4 projects (detected via `lean-toolchain`), the `bubble-lean` image comes pre-loaded with recent stable and RC toolchains.

**Network allowlisting**: Containers can only reach allowed domains (GitHub by default, plus language-specific domains like `releases.lean-lang.org` for Lean). IPv6 is blocked, DNS is restricted to the container resolver, and outbound SSH is blocked. Configurable in `~/.bubble/config.toml`.

## Requirements

- **macOS**: Homebrew, then `brew install colima incus`
- **Linux**: Incus installed natively ([install guide](https://linuxcontainers.org/incus/docs/main/installing/))
- **VSCode** with [Remote - SSH](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-ssh) extension

## Commands

| Command | Description |
|---------|-------------|
| `bubble <target>` | Open (or create) a bubble for a GitHub URL/repo |
| `bubble init` | First-time setup |
| `bubble list` | List all bubbles |
| `bubble pause <name>` | Freeze a bubble |
| `bubble destroy <name>` | Delete a bubble permanently |
| `bubble images list\|build` | Manage base images |
| `bubble git update` | Refresh shared git mirrors |
| `bubble network apply\|remove <name>` | Manage network restrictions |
| `bubble automation install\|remove\|status` | Manage periodic jobs |

## Images

| Image | Contents |
|-------|----------|
| `bubble-base` | Ubuntu 24.04, git, openssh-server, build-essential |
| `bubble-lean` | bubble-base + elan + latest stable/RC toolchains |

Build images with `bubble images build bubble-base` or `bubble images build bubble-lean`.

## Configuration

Config lives at `~/.bubble/config.toml`. Created automatically on `bubble init`.

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
  "*.githubusercontent.com",
]
```

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

MIT
