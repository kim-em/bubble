# bubble

Containerized development environments for the Lean language, powered by [Incus](https://linuxcontainers.org/incus/).

## Quick Start

```bash
# Install
uv tool install dev-bubble

# Open a bubble for a GitHub PR — just paste the URL, and you get a containerized VSCode window!
bubble https://github.com/leanprover-community/mathlib4/pull/35219

# List your bubbles
bubble list
```

See [Examples](#examples) for branches, local repos, issues, remote, and non-interactive use.

## Examples

```bash
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

# GitHub issues
bubble https://github.com/owner/repo/issues/42
bubble mathlib4/issues/42

# PR or issue number shorthand (when in a cloned repo, requires gh CLI)
bubble 123                   # auto-detects PR vs issue via GitHub API

# Drop into an SSH session instead of VSCode
bubble leanprover/lean4 --shell

# Just create, don't open anything
bubble leanprover/lean4 --no-interactive

# Run on a remote host
bubble leanprover/lean4 --ssh myserver
bubble leanprover/lean4 --ssh user@host:2222
bubble remote set-default myserver         # all future bubbles go remote
bubble leanprover/lean4 --local            # override remote default

# Pause a bubble
bubble pause mathlib4-pr-35219

# Pop (destroy permanently)
bubble pop mathlib4-pr-35219
```

## Development Install

```bash
# Install from GitHub
uv tool install git+https://github.com/kim-em/bubble.git

# For development
uv pip install -e '.[dev]'
```

## How It Works

Each "bubble" is a lightweight Linux container (via Incus) with:
- Your project cloned and ready to work on
- SSH server for VSCode Remote connection
- Network restricted to allowed domains only
- Language-specific tooling when detected (e.g. Lean 4 via elan)

**URL-first interface**: The primary command is `bubble <target>`. Targets can be full GitHub URLs, partial URLs, org/repo paths, or learned short names. If a bubble already exists for that target, it re-attaches instead of creating a new one.

**Shared git objects**: A bare mirror of each repo is maintained on the host. Containers clone via `git --reference`, sharing the immutable object store. This means creating a new bubble for a mathlib PR downloads only the few new commits, not the entire 1.5GB repo.

**Issue targets**: When you open an issue, bubble creates a branch named `issue-<number>` from the default branch, ready for you to start working on the fix.

**Language hooks**: bubble automatically detects the project's language and selects the right image. For Lean 4 projects (detected via `lean-toolchain`), the container includes elan, pre-installed VS Code extensions, and auto-downloads the mathlib cache when needed.

**Network allowlisting**: Containers can only reach allowed domains (GitHub by default, plus language-specific domains like `releases.lean-lang.org` for Lean). IPv6 is blocked, DNS is restricted to the container resolver, and outbound SSH is blocked. Configurable in `~/.bubble/config.toml`.

## Requirements

- **macOS**: Homebrew, then `brew install colima incus`
- **Linux**: Incus installed natively ([install guide](https://linuxcontainers.org/incus/docs/main/installing/))
- **Editor**: [VSCode](https://code.visualstudio.com/) with Remote SSH extension (or `--shell` for plain SSH)

## Commands

| Command | Description |
|---------|-------------|
| `bubble <target>` | Open (or create) a bubble for a GitHub URL/repo |
| `bubble list` | List all bubbles |
| `bubble pause <name>` | Freeze a bubble |
| `bubble pop <name>` | Pop a bubble (delete permanently) |
| `bubble cleanup` | Pop all clean bubbles (no unsaved work) |
| `bubble images list\|build\|delete` | Manage base images |
| `bubble git update` | Refresh shared git mirrors |
| `bubble network apply\|remove <name>` | Manage network restrictions |
| `bubble automation install\|remove\|status` | Manage periodic jobs |
| `bubble relay enable\|disable\|status` | Manage bubble-in-bubble relay |
| `bubble remote set-default\|clear-default\|status` | Manage remote SSH host |
| `bubble cloud provision\|destroy\|start\|stop\|status` | Manage Hetzner Cloud server |
| `bubble cloud default on\|off` | Set cloud as the default for all bubbles |
| `bubble cloud ssh` | SSH directly to the cloud server |
| `bubble tools list\|set\|status` | Manage tools installed in container images |
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

[remote]
default_host = ""        # e.g. "user@myserver" or "user@host:2222"

[tools]
claude = "auto"     # "yes" | "no" | "auto" (detect from host)
codex = "auto"
```

### Tools

Tools like Claude Code and OpenAI Codex can be installed in container images. Each tool defaults to `"auto"`, which installs it if the corresponding command is found on your host.

```bash
bubble tools list                  # show all tools and their settings
bubble tools set claude yes   # always install
bubble tools set codex no          # never install
bubble tools status                # show what would actually be installed
```

When the resolved tool set changes, a background image rebuild is triggered automatically.

### Claude Code Integration

When a bubble is opened for a GitHub issue with VS Code (the default editor), bubble can set up [Claude Code](https://claude.ai/claude-code) as an autonomous coding agent. It fetches the issue title, body, and comments via the GitHub API, generates a prompt, and injects a VS Code task that launches Claude Code when the workspace opens.

```bash
# Creates a containerized environment; when VS Code opens, Claude starts working on the issue
bubble mathlib4/issues/42
```

Claude is instructed to read the issue, implement a fix on the `issue-<number>` branch, and open a PR. This turns `bubble 42` (for an issue) into an autonomous coding agent workflow.

You can also provide a custom prompt for any bubble via the `BUBBLE_CLAUDE_PROMPT` environment variable:

```bash
BUBBLE_CLAUDE_PROMPT="Refactor the parser module" bubble leanprover/lean4
```

Requirements: Claude Code must be installed in the container (see tool settings above), the `gh` CLI must be available on the host (for fetching issue/PR metadata), and the default VS Code editor must be used. With `--shell` or `--no-interactive`, the prompt is not injected. If `gh` is unavailable or the API call fails, bubble proceeds without injecting a prompt.

## Hetzner Cloud

Run bubbles on auto-provisioned Hetzner Cloud servers. The server shuts down automatically when idle to minimize costs, and restarts on demand when you open a new bubble.

### Setup

1. Create a Hetzner Cloud account at [console.hetzner.cloud](https://console.hetzner.cloud)
2. Go to your project → Security → API Tokens → Generate API Token (read/write)
3. Set the token in your environment:
   ```bash
   export HETZNER_TOKEN="your-token-here"
   ```
4. Install the cloud dependency:
   ```bash
   uv pip install 'dev-bubble[cloud]'
   ```
5. Provision a server:
   ```bash
   bubble cloud provision                    # default: cx43 (8 shared vCPU, 16GB RAM)
   bubble cloud provision --type ccx43       # 16 dedicated vCPU, 64GB RAM
   bubble cloud provision --type cx53 --location hel1  # specific type and datacenter
   ```

Bubble generates its own SSH keypair (`~/.bubble/cloud_key`) — no need to configure SSH keys manually.

### Usage

```bash
# Open a bubble on the cloud server
bubble --cloud leanprover-community/mathlib4/pull/35219

# Set cloud as the default (no --cloud flag needed)
bubble cloud default on
bubble leanprover-community/mathlib4     # goes to cloud automatically
bubble leanprover/lean4 --local          # override: run locally instead
```

Multiple bubbles share one cloud server. If the server is stopped (manually or by idle auto-shutdown), it restarts automatically when you run `bubble --cloud <target>` or `bubble <target>` with cloud as default.

### Server Types and Pricing

```bash
bubble cloud provision --list            # show all types with current pricing
```

Common types:

| Type | Specs | Approximate Cost |
|------|-------|-----------------|
| `cx33` | 4 shared vCPU, 8 GB RAM | ~€0.01/hr |
| `cx43` | 8 shared vCPU, 16 GB RAM (default) | ~€0.02/hr |
| `cx53` | 16 shared vCPU, 32 GB RAM | ~€0.04/hr |
| `ccx33` | 8 dedicated vCPU, 32 GB RAM | ~€0.09/hr |
| `ccx43` | 16 dedicated vCPU, 64 GB RAM | ~€0.17/hr |

Prices are approximate and vary by datacenter. Run `bubble cloud provision --list` for current pricing from the Hetzner API.

Dedicated vCPU types (`ccx*`) may require a limit increase on new Hetzner accounts — the CLI will guide you if so.

Hetzner bills servers hourly while they exist, even when powered off. To stop billing entirely, use `bubble cloud destroy`. The idle auto-shutdown reduces costs by keeping the server off when not in use, but the only way to fully stop charges is to destroy the server.

### Idle Auto-Shutdown

A systemd timer checks every 5 minutes for SSH connections and CPU load. If there are **no SSH connections** and **low CPU** (normalized load < 0.5) for the configured idle timeout (default: 15 minutes), the server shuts down automatically. A 15-minute boot grace period prevents shutdown during initial setup, so a freshly booted idle server won't shut down for roughly 25 minutes.

- Running containers do **not** prevent shutdown — only active SSH sessions and high CPU load do
- Containers survive shutdown: they're still on disk when the server restarts
- The server restarts automatically on your next `bubble --cloud <target>` command

### Lifecycle Commands

```bash
bubble cloud status                      # show server info and current state
bubble cloud stop                        # power off manually
bubble cloud start                       # power on and wait for SSH
bubble cloud destroy                     # delete server and all containers permanently
bubble cloud ssh                         # SSH directly to the cloud server (requires server running)
```

### Configuration

Cloud settings in `~/.bubble/config.toml`:

```toml
[cloud]
server_type = "cx43"         # default server type for provision
location = "fsn1"            # datacenter: fsn1, nbg1, hel1, ash, hil
idle_timeout = 900           # seconds before idle shutdown (default: 900 = 15min)
```

The `HETZNER_TOKEN` environment variable is always required — the token is never stored on disk.

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
- **GitHub auth proxy**: Host token never enters containers. Git push/pull is repo-scoped. GitHub API access (gh CLI, GraphQL) is read-only but **account-wide** — queries can read any data the host token can access. Disable with `bubble security set github-api off`

### Known Limitations

These are inherent consequences of the architecture, not bugs. Understanding them helps you make informed trust decisions.

1. **DNS exfiltration**: The network allowlist restricts IP connectivity, but DNS queries still reach the internet via the container resolver. Data can be encoded in DNS queries to exfiltrate information. This is inherent to any iptables-based approach that allows DNS.

2. **/24 CIDR over-allowance**: Domain allowlisting resolves to /24 CIDR blocks (256 IPs) to handle CDN IP rotation. Other services sharing the same /24 block are also reachable.

3. **iptables defense depth**: Network rules are enforced by iptables inside the container. The `user` account cannot modify them (no sudo), but a kernel exploit or other root escalation within the container could flush the rules. There is no external enforcement layer (e.g., Incus ACLs).

4. **Boot-time network window**: There is a brief window between container launch and iptables rule application during which the container has unrestricted network access. No user code runs during this window with stock images.

5. **Auth proxy token visibility**: The per-container auth proxy token is stored in the user's git config and in `/etc/profile.d/bubble-gh.sh` (mode 644). Any process in the container can read it. The token is scoped to one repository and access level for git and REST API requests, but GraphQL queries (level 3+) are not repo-scoped and can read any data the host token can access.

## License

Apache 2.0 — see [LICENSE](LICENSE).
