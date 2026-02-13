# lean-bubbles

Containerized development environments for Lean 4 and Mathlib, powered by [Incus](https://linuxcontainers.org/incus/).

## Why?

- **Safety**: Run untrusted PRs in isolated containers with network allowlisting
- **Convenience**: Spin up 5-10+ concurrent development sessions without managing multiple clones
- **Speed**: Shared git objects and build caches mean new bubbles start in seconds, not minutes
- **Persistence**: Archive a session, come back to it later from any machine via a PR URL
- **Claude Code**: Session persistence across archive/reconstitute cycles

## Quick Start

```bash
pip install lean-bubbles

# First-time setup (installs Colima on macOS, builds base image, clones shared repos)
bubble init

# Create a bubble for a mathlib PR and open VSCode
bubble new mathlib4 --pr 12345

# List your bubbles
bubble list

# Open VSCode for an existing bubble
bubble attach mathlib4-pr-12345

# Drop into a shell instead
bubble shell mathlib4-pr-12345

# Archive when done (saves state, destroys container)
bubble archive mathlib4-pr-12345

# Resume later, even on a different machine
bubble resume mathlib4-pr-12345
bubble resume https://github.com/leanprover-community/mathlib4/pull/12345

# Destroy permanently
bubble destroy mathlib4-pr-12345
```

## Moving Local Work Into a Bubble

Already working on a local checkout? Move it into a bubble:

```bash
cd ~/projects/lean/mathlib4
bubble wrap .                    # Move state into a bubble, opens VSCode
bubble wrap . --copy             # Copy instead (leave local dir unchanged)
bubble wrap . --pr 12345         # Associate with a PR for future resume
```

## How It Works

Each "bubble" is a lightweight Linux container (via Incus) with:
- Lean 4 toolchain (via elan)
- Your project cloned and ready to build
- SSH server for VSCode Remote connection
- Network restricted to allowed domains only

**Shared git objects**: A bare mirror of each repo is maintained on the host. Containers clone via `git --reference`, sharing the immutable object store. This means creating a new bubble for a mathlib PR downloads only the few new commits, not the entire 1.5GB repo.

**Shared build caches**: `.lake` caches are shared across containers with matching toolchains, avoiding redundant `lake exe cache get` downloads.

**Network allowlisting**: Containers can only reach allowed domains (GitHub, Lean releases, Anthropic API, etc.). Configurable in `~/.lean-bubbles/config.toml`.

## Requirements

- **macOS**: Homebrew, then `brew install colima incus`
- **Linux**: Incus installed natively ([install guide](https://linuxcontainers.org/incus/docs/main/installing/))
- **VSCode** with [Remote - SSH](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-ssh) extension

## Commands

| Command | Description |
|---------|-------------|
| `bubble init` | First-time setup |
| `bubble new <repo> [--pr N] [--branch B]` | Create a new bubble |
| `bubble list [--archived]` | List all bubbles |
| `bubble attach <name>` | Open VSCode for a bubble |
| `bubble shell <name>` | Shell into a bubble |
| `bubble wrap [dir] [--copy] [--pr N]` | Move/copy local work into a bubble |
| `bubble pause <name>` | Freeze a bubble |
| `bubble archive <name>` | Archive (save state, destroy container) |
| `bubble resume <name\|PR-URL>` | Resume from archive or PR URL |
| `bubble claude <name>` | Start/resume Claude Code in a bubble |
| `bubble destroy <name>` | Delete a bubble permanently |
| `bubble images list\|build` | Manage base images |
| `bubble git update` | Refresh shared git mirrors |
| `bubble network apply\|remove <name>` | Manage network restrictions |
| `bubble claude-skill` | Install Claude Code skill |

## Base Images

| Image | Contents |
|-------|----------|
| `lean-base` | Ubuntu 24.04, elan, git, openssh-server |
| `lean-mathlib` | lean-base + mathlib4 cloned + .olean cache |
| `lean-batteries` | lean-base + batteries cloned + built |
| `lean-lean4` | lean-base + lean4 cloned + build deps |

Build derived images with `bubble images build lean-mathlib`.

## Supported Repos

Out of the box, `bubble new` recognizes these short names:

| Short name | Repository |
|------------|-----------|
| `mathlib4` / `mathlib` | leanprover-community/mathlib4 |
| `lean4` / `lean` | leanprover/lean4 |
| `batteries` | leanprover-community/batteries |
| `aesop` | leanprover-community/aesop |
| `proofwidgets4` | leanprover-community/ProofWidgets4 |

You can also use any `org/repo` directly: `bubble new leanprover-community/quote4`

## Configuration

Config lives at `~/.lean-bubbles/config.toml`. Created automatically on `bubble init`.

```toml
[runtime]
backend = "incus"
colima_cpu = 24          # macOS: CPUs for the Colima VM
colima_memory = 16       # macOS: GB of RAM
colima_vm_type = "vz"    # macOS: Apple Virtualization.Framework

[git]
shared_repos = [
  "leanprover-community/mathlib4",
  "leanprover/lean4",
  "leanprover-community/batteries",
]

[network]
allowlist = [
  "github.com", "*.githubusercontent.com",
  "releases.lean-lang.org",
  "api.anthropic.com", "statsig.anthropic.com",
  "registry.npmjs.org",
]

[extensions.claude]
enabled = false          # Enable Claude Code session persistence
unset_api_key = true     # Force subscription auth inside containers

[extensions.zulip]
enabled = false          # Enable sandboxed Zulip access
```

## Performance

On Apple Silicon (M-series) with Apple's Virtualization.Framework, container builds run at essentially native speed. In benchmarks, building batteries takes ~19.7s in a container vs ~18.8s natively.

## Claude Code Integration

Run `bubble claude-skill` to install a Claude Code skill that teaches Claude how to use bubble commands during your development sessions.

Use `bubble claude <name>` to start Claude Code inside a container with optional session resume.

## License

MIT
