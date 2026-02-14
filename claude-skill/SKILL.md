---
name: lean-bubbles
description: Create and manage containerized Lean development environments using the `bubble` CLI.
triggers:
  - bubble
  - lean container
  - lean sandbox
  - containerized lean
  - isolated environment
  - lean environment
---

# lean-bubbles Skill

Use the `bubble` CLI to create and manage containerized Lean development environments.

## When to Use

- User wants to work on a Lean/Mathlib PR in an isolated environment
- User is reviewing an untrusted PR and wants sandboxing
- User wants to start a new Lean project without cluttering their host
- User asks for a "bubble", "container", "sandbox", or "isolated environment" for Lean work
- **Proactively suggest** bubbling when the user is about to checkout an unfamiliar PR
- User wants to move their current local work into a container

## Commands

### Create a bubble
```bash
# From a PR number
bubble new mathlib4 --pr 12345

# From a branch
bubble new batteries --branch fix-grind

# Fresh from main
bubble new lean4

# Any org/repo works
bubble new leanprover-community/quote4 --branch my-feature

# Don't open VSCode (e.g., when running in terminal)
bubble new mathlib4 --pr 12345 --no-attach
```

### Move local work into a bubble
```bash
# Move current directory into a bubble (stashes local changes)
bubble wrap .

# Copy instead (leave local dir unchanged)
bubble wrap . --copy

# Associate with a PR
bubble wrap . --pr 12345
```

### Work with bubbles
```bash
# Open VSCode connected to a bubble
bubble attach mathlib4-pr-12345

# Shell access
bubble shell mathlib4-pr-12345

# List all bubbles
bubble list
bubble list --archived       # Include archived bubbles
bubble list --json           # JSON output
```

### Lifecycle
```bash
# Pause (freeze) a bubble
bubble pause mathlib4-pr-12345

# Archive (save state, destroy container)
bubble archive mathlib4-pr-12345

# Resume from local archive
bubble resume mathlib4-pr-12345

# Destroy permanently
bubble destroy mathlib4-pr-12345
```

### Base images
```bash
# Build derived images (pre-cached for faster creation)
bubble images build lean-mathlib
bubble images build lean-batteries
bubble images build lean-lean4
bubble images list
```

### Network
```bash
# Apply/remove network restrictions
bubble network apply mathlib4-pr-12345
bubble network remove mathlib4-pr-12345
```

### Automation
```bash
# Install periodic jobs (hourly git update, weekly image refresh)
bubble automation install

# Check automation status
bubble automation status

# Remove automation jobs
bubble automation remove
```

### Maintenance
```bash
# First-time setup
bubble init

# Refresh shared git mirrors
bubble git update
```

## Supported Short Names

| Name | Repository |
|------|-----------|
| `mathlib4` | leanprover-community/mathlib4 |
| `lean4` | leanprover/lean4 |
| `batteries` | leanprover-community/batteries |
| `aesop` | leanprover-community/aesop |
| `proofwidgets4` | leanprover-community/ProofWidgets4 |

## Tips

- Bubbles use shared git objects, so creating a new one is fast (~seconds) even for large repos
- Each bubble has SSH access: `ssh bubble-<name>`
- VSCode connects via Remote SSH automatically
- Run `bubble init` if you haven't set up lean-bubbles yet
- Network allowlisting is applied by default — containers can only reach allowed domains
- Use `bubble archive` when done — it saves state and frees disk
- Use `bubble resume` to pick up where you left off
