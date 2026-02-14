---
name: bubble
description: Create and manage containerized development environments using the `bubble` CLI.
triggers:
  - bubble
  - container
  - sandbox
  - isolated environment
  - development environment
---

# bubble Skill

Use the `bubble` CLI to create and manage containerized development environments.

## When to Use

- User wants to work on a PR in an isolated environment
- User is reviewing an untrusted PR and wants sandboxing
- User wants to start a project without cluttering their host
- User asks for a "bubble", "container", "sandbox", or "isolated environment"
- **Proactively suggest** bubbling when the user is about to checkout an unfamiliar PR
- Language hooks auto-detect Lean 4 projects and provide pre-configured toolchains

## Commands

### Open a bubble (the primary command)
```bash
# Full GitHub URL — just paste it
bubble https://github.com/leanprover-community/mathlib4/pull/35219

# Shorter forms
bubble leanprover-community/mathlib4/pull/35219
bubble mathlib4/pull/35219    # short names are learned after first use
bubble mathlib4               # default branch

# Branch or commit
bubble leanprover/lean4/tree/some-branch
bubble leanprover/lean4/commit/abc123

# From a local git repo
bubble .
bubble ./path/to/repo
bubble --path mydir

# PR number shorthand (when in a cloned repo)
bubble 123

# Options
bubble mathlib4 --ssh            # SSH instead of VSCode
bubble mathlib4 --no-interactive # Just create, don't attach
bubble mathlib4 --no-network     # Skip network allowlisting
bubble mathlib4 --name my-custom-name
```

### Manage bubbles
```bash
# List all bubbles
bubble list
bubble list --json

# Pause (freeze) a bubble
bubble pause mathlib4-pr-35219

# Destroy permanently
bubble destroy mathlib4-pr-35219
bubble destroy mathlib4-pr-35219 --force
```

### Images
```bash
bubble images list
bubble images build base
bubble images build lean
```

### Git store
```bash
# Refresh shared bare repo mirrors
bubble git update
```

### Network
```bash
bubble network apply mathlib4-pr-35219
bubble network remove mathlib4-pr-35219
```

### Automation
```bash
bubble automation install    # hourly git update, weekly image refresh
bubble automation status
bubble automation remove
```

### Bubble-in-bubble relay
```bash
bubble relay enable    # allow containers to open bubbles on the host
bubble relay disable
bubble relay status
```

## Tips

- Bubbles use shared git objects — creating a new one is fast (~seconds) even for large repos
- Each bubble has SSH access: `ssh bubble-<name>`
- VSCode connects via Remote SSH automatically
- If a bubble for the same target already exists, `bubble` re-attaches to it
- Network allowlisting is applied by default — containers can only reach allowed domains
- Language hooks auto-detect project type (Lean 4 via `lean-toolchain` file)
- Config lives at `~/.bubble/config.toml`; data at `~/.bubble/`
