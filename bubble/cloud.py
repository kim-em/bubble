"""Hetzner Cloud support for auto-provisioned remote bubble hosts."""

import json
import os
import subprocess
import time

import click

from .config import (
    CLOUD_KEY_FILE,
    CLOUD_KNOWN_HOSTS,
    CLOUD_STATE_FILE,
    DATA_DIR,
    save_config,
)
from .remote import RemoteHost

# ---------------------------------------------------------------------------
# Cloud-init script
# ---------------------------------------------------------------------------

CLOUD_INIT_TEMPLATE = """\
#!/bin/bash
set -euo pipefail

# Ensure curl is available (usually present but not guaranteed)
apt-get update
apt-get install -y curl ca-certificates

# Install Incus via Zabbly repo
mkdir -p /etc/apt/keyrings/
curl -fsSL https://pkgs.zabbly.com/key.asc > /etc/apt/keyrings/zabbly.asc

CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
ARCH=$(dpkg --print-architecture)

cat > /etc/apt/sources.list.d/zabbly-incus-stable.sources <<SOURCES
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: $CODENAME
Components: main
Architectures: $ARCH
Signed-By: /etc/apt/keyrings/zabbly.asc
SOURCES

apt-get update
apt-get install -y incus

# Initialize Incus
incus admin init --auto

# Install idle auto-shutdown
mkdir -p /var/lib/bubble

cat > /etc/bubble-idle.conf <<CONF
IDLE_TIMEOUT={idle_timeout}
CONF

cat > /usr/local/bin/bubble-idle-check <<'IDLESCRIPT'
#!/bin/bash
set -euo pipefail

CONF_FILE="/etc/bubble-idle.conf"
ACTIVITY_FILE="/var/lib/bubble/last-activity"
BOOT_GRACE=900  # 15 minutes after boot

# Read config
IDLE_TIMEOUT=900
if [ -f "$CONF_FILE" ]; then
    source "$CONF_FILE"
fi

NOW=$(date +%s)

# Grace period after boot
BOOT_TIME=$(date -d "$(uptime -s)" +%s 2>/dev/null || echo 0)
if [ $((NOW - BOOT_TIME)) -lt $BOOT_GRACE ]; then
    echo "$NOW" > "$ACTIVITY_FILE"
    exit 0
fi

ACTIVE=false

# Check for established SSH connections from outside
if ss -tnp state established dport = :22 2>/dev/null | grep -q .; then
    ACTIVE=true
fi

# Check normalized CPU load (load1 / nproc > 0.5 means busy)
if [ "$ACTIVE" = false ]; then
    LOAD1=$(awk '{print $1}' /proc/loadavg)
    NPROC=$(nproc)
    if awk "BEGIN{exit !(${LOAD1}/${NPROC} > 0.5)}"; then
        ACTIVE=true
    fi
fi

if [ "$ACTIVE" = true ]; then
    echo "$NOW" > "$ACTIVITY_FILE"
    exit 0
fi

# Check how long we've been idle
if [ -f "$ACTIVITY_FILE" ]; then
    LAST_ACTIVE=$(cat "$ACTIVITY_FILE")
else
    # First check — start the idle timer now
    echo "$NOW" > "$ACTIVITY_FILE"
    exit 0
fi

IDLE_SECONDS=$((NOW - LAST_ACTIVE))
if [ "$IDLE_SECONDS" -ge "$IDLE_TIMEOUT" ]; then
    logger -t bubble-idle "Shutting down after ${IDLE_SECONDS}s idle"
    shutdown -h now
fi
IDLESCRIPT
chmod +x /usr/local/bin/bubble-idle-check

# Systemd timer for idle check
cat > /etc/systemd/system/bubble-idle.service <<'UNIT'
[Unit]
Description=Bubble idle shutdown check

[Service]
Type=oneshot
ExecStart=/usr/local/bin/bubble-idle-check
UNIT

cat > /etc/systemd/system/bubble-idle.timer <<'TIMER'
[Unit]
Description=Bubble idle shutdown check timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now bubble-idle.timer

# Initialize activity timestamp
echo "$(date +%s)" > /var/lib/bubble/last-activity

# Mark readiness (after everything succeeds)
touch /var/run/bubble-cloud-ready
"""


def _get_cloud_init(config: dict) -> str:
    """Generate cloud-init script from config."""
    idle_timeout = config.get("cloud", {}).get("idle_timeout", 900)
    return CLOUD_INIT_TEMPLATE.replace("{idle_timeout}", str(idle_timeout))


# ---------------------------------------------------------------------------
# SSH key management
# ---------------------------------------------------------------------------


def _ensure_ssh_key() -> tuple[str, str]:
    """Ensure bubble cloud SSH keypair exists. Returns (private_path, public_key_content)."""
    priv = CLOUD_KEY_FILE
    pub = CLOUD_KEY_FILE.with_suffix(".pub")

    if not priv.exists() or not pub.exists() or not pub.read_text().strip():
        # Regenerate if either file is missing or empty
        priv.unlink(missing_ok=True)
        pub.unlink(missing_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(priv),
             "-N", "", "-C", "bubble-cloud"],
            check=True,
            capture_output=True,
        )
        priv.chmod(0o600)

    return str(priv), pub.read_text().strip()


def _ssh_cmd_base() -> list[str]:
    """Base SSH command with bubble cloud key and known_hosts."""
    return [
        "ssh",
        "-i", str(CLOUD_KEY_FILE),
        "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={CLOUD_KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=accept-new",
    ]


def _ssh_run(
    host: str, command: str, timeout: int = 30, check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command on the cloud server via SSH."""
    cmd = _ssh_cmd_base() + [f"root@{host}", command]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def _load_state() -> dict | None:
    """Load cloud state from disk. Returns None if no state."""
    if not CLOUD_STATE_FILE.exists():
        return None
    return json.loads(CLOUD_STATE_FILE.read_text())


def _save_state(state: dict):
    """Save cloud state to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CLOUD_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def _clear_state():
    """Remove cloud state file."""
    CLOUD_STATE_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Hetzner API helpers
# ---------------------------------------------------------------------------


def _get_token() -> str:
    """Get Hetzner API token from environment."""
    token = os.environ.get("HETZNER_TOKEN", "")
    if not token:
        raise click.ClickException(
            "HETZNER_TOKEN environment variable is required.\n"
            "Get one from: https://console.hetzner.cloud/projects → API tokens"
        )
    return token


def _get_client():
    """Get hcloud client instance."""
    try:
        from hcloud import Client
    except ImportError:
        raise click.ClickException(
            "hcloud package not installed. Install with:\n"
            "  uv pip install 'dev-bubble[cloud]'\n"
            "  # or: pip install hcloud"
        )
    return Client(token=_get_token())


# ---------------------------------------------------------------------------
# Server creation error handling
# ---------------------------------------------------------------------------

# Server types to suggest as alternatives, in preference order.
_FALLBACK_TYPES = ["ccx43", "ccx33", "cx53", "cx43", "cx33"]


def _try_create(client, server_type: str, location: str, ssh_key=None) -> bool:
    """Test whether a server type + location combo works (creates and deletes).

    Returns True if creation succeeds, False for availability/limit errors.
    Re-raises auth, rate-limit, and other unexpected errors.
    An ssh_key should be passed to prevent Hetzner from generating a root
    password and emailing it to the account owner.
    """
    import secrets

    from hcloud._exceptions import APIException
    from hcloud.images import Image
    from hcloud.locations import Location
    from hcloud.server_types import ServerType

    suffix = secrets.token_hex(4)
    try:
        create_kwargs = dict(
            name=f"bubble-probe-{server_type}-{suffix}",
            server_type=ServerType(name=server_type),
            image=Image(name="ubuntu-24.04"),
            location=Location(name=location),
            start_after_create=False,
        )
        if ssh_key is not None:
            create_kwargs["ssh_keys"] = [ssh_key]
        resp = client.servers.create(**create_kwargs)
        client.servers.delete(resp.server)
        return True
    except APIException as e:
        code = str(e.code)
        # Auth/permission/rate-limit errors should not be swallowed
        if code in ("unauthorized", "forbidden") or e.code in (401, 403, 429):
            raise
        return False
    except Exception:
        raise


def _spec_str(client, name: str, location: str) -> str:
    """Return a short spec string like '16 vCPU, 64 GB RAM (dedicated), ~€0.17/hr'."""
    try:
        st = client.server_types.get_by_name(name)
        cpu_type = st.data_model.cpu_type
        label = "dedicated" if cpu_type == "dedicated" else "shared"
        price = ""
        for p in st.data_model.prices:
            loc = p["location"] if isinstance(p, dict) else p.location
            if loc == location:
                hourly = p["price_hourly"]["gross"] if isinstance(p, dict) else p.price_hourly.gross
                price = f", ~€{float(hourly):.2f}/hr"
                break
        return f"{st.data_model.cores} vCPU, {st.data_model.memory:.0f} GB RAM ({label}{price})"
    except Exception:
        return name


# Hetzner API error codes that indicate availability/limit issues (worth probing).
_PROBEABLE_CODES = {"resource_unavailable", "limit_exceeded", "placement_error"}

# Maximum number of probe attempts to avoid excessive API calls.
_MAX_PROBES = 10


def _handle_create_error(exc: Exception, client, server_type: str, location: str, ssh_key=None):
    """Turn an opaque Hetzner API error into actionable guidance."""
    from hcloud._exceptions import APIException

    if not isinstance(exc, APIException):
        raise exc

    code = str(exc.code)
    if code not in _PROBEABLE_CODES:
        raise exc

    # Probe alternatives
    click.echo("\nServer creation failed. Checking available alternatives...")
    locations = ["fsn1", "nbg1", "hel1", "ash", "hil"]
    # Put requested location first, then others
    if location in locations:
        locations.remove(location)
    locations.insert(0, location)

    probes_remaining = _MAX_PROBES

    # Check requested type at other locations first
    for loc in locations:
        if loc == location or probes_remaining <= 0:
            continue
        probes_remaining -= 1
        if _try_create(client, server_type, loc, ssh_key=ssh_key):
            raise click.ClickException(
                f"'{server_type}' is not available in {location}, "
                f"but works in {loc}.\n\n"
                f"  bubble cloud provision --type {server_type} --location {loc}"
            )

    # Requested type doesn't work anywhere — find alternatives
    working = []
    for alt in _FALLBACK_TYPES:
        if alt == server_type or probes_remaining <= 0:
            continue
        for loc in locations:
            if probes_remaining <= 0:
                break
            probes_remaining -= 1
            if _try_create(client, alt, loc, ssh_key=ssh_key):
                working.append((alt, loc))
                break  # one working location per type is enough

    lines = []
    lines.append(
        f"'{server_type}' is not available (your account may need a limit increase "
        f"for this server category)."
    )
    if working:
        lines.append("")
        lines.append("Available alternatives:")
        for alt, loc in working:
            spec = _spec_str(client, alt, loc)
            loc_note = "" if loc == location else f" --location {loc}"
            lines.append(f"  bubble cloud provision --type {alt}{loc_note}  # {spec}")

    is_dedicated = server_type.startswith("ccx")
    if is_dedicated and not any(a.startswith("ccx") for a, _ in working):
        lines.append("")
        lines.append(
            "To use dedicated CPU servers, request a limit increase at:\n"
            "  https://console.hetzner.cloud  → Project → Servers → Resource limits"
        )

    raise click.ClickException("\n".join(lines))


# ---------------------------------------------------------------------------
# Server type listing
# ---------------------------------------------------------------------------

# Types to show in --list, grouped by category. Order within group matters.
_LIST_TYPES = [
    # (name, note)
    ("cx23", None),
    ("cx33", None),
    ("cx43", "default"),
    ("cx53", None),
    ("ccx13", None),
    ("ccx23", None),
    ("ccx33", None),
    ("ccx43", None),
    ("ccx53", None),
    ("ccx63", None),
]


def list_server_types(config: dict, location: str | None = None):
    """List available server types with specs and pricing."""
    cloud_cfg = config.get("cloud", {})
    loc = location or cloud_cfg.get("location") or "fsn1"

    client = _get_client()

    click.echo(f"Available server types (location: {loc}):\n")

    prev_category = None
    for name, note in _LIST_TYPES:
        try:
            st = client.server_types.get_by_name(name)
        except Exception:
            continue
        if st.data_model.deprecation is not None:
            continue

        cpu_type = st.data_model.cpu_type
        category = "dedicated" if cpu_type == "dedicated" else "shared"
        if category != prev_category:
            if prev_category is not None:
                click.echo()
            label = "Shared vCPU" if category == "shared" else "Dedicated vCPU"
            click.echo(f"  {label}:")
            prev_category = category

        # Find price for this location
        price_str = ""
        for p in st.data_model.prices:
            p_loc = p["location"] if isinstance(p, dict) else p.location
            if p_loc == loc:
                hourly = p["price_hourly"]["gross"] if isinstance(p, dict) else p.price_hourly.gross
                price_str = f"€{float(hourly):.2f}/hr"
                break

        note_str = f"  ({note})" if note else ""
        click.echo(
            f"    {name:8s} {st.data_model.cores:2d} vCPU, "
            f"{st.data_model.memory:5.0f} GB RAM, "
            f"{st.data_model.disk:4d} GB disk  "
            f"{price_str}{note_str}"
        )

    click.echo(f"\nTo provision:  bubble cloud provision --type <name>")
    click.echo(f"Other locations: --location nbg1|hel1|ash|hil")


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def provision_server(config: dict, server_type: str | None = None, location: str | None = None):
    """Provision a new Hetzner Cloud server for bubble.

    Creates the server with cloud-init that installs Incus and sets up
    idle auto-shutdown. Registers an SSH key and waits for readiness.
    """
    state = _load_state()
    if state:
        raise click.ClickException(
            f"Cloud server already exists: {state.get('server_name', 'unknown')} "
            f"({state.get('ipv4', '?')})\n"
            f"Use 'bubble cloud destroy' first, or 'bubble cloud status' to check."
        )

    cloud_cfg = config.get("cloud", {})

    # Resolve server_type: flag > config > default
    st = server_type or cloud_cfg.get("server_type") or "cx43"

    loc = location or cloud_cfg.get("location", "fsn1")
    server_name = cloud_cfg.get("server_name", "bubble-cloud")

    # Save resolved values back to config for next time
    if server_type:
        config.setdefault("cloud", {})["server_type"] = server_type
    if location:
        config.setdefault("cloud", {})["location"] = location
    save_config(config)

    client = _get_client()

    # Ensure SSH key
    priv_path, pub_content = _ensure_ssh_key()
    click.echo(f"SSH key: {priv_path}")

    # Register SSH key with Hetzner

    key_name = f"bubble-{server_name}"
    click.echo(f"Registering SSH key '{key_name}'...")
    try:
        ssh_key = client.ssh_keys.create(name=key_name, public_key=pub_content)
    except Exception as e:
        if "uniqueness_error" in str(e).lower() or "already" in str(e).lower():
            # Key already exists, find it and verify it matches
            for k in client.ssh_keys.get_all():
                if k.data_model.name == key_name:
                    ssh_key = k
                    break
            else:
                raise click.ClickException(
                    f"SSH key '{key_name}' exists but not found: {e}"
                )
            # Verify the remote key matches our local key
            remote_pub = ssh_key.data_model.public_key.strip()
            if remote_pub != pub_content:
                click.echo("  Existing key doesn't match local key, replacing...")
                client.ssh_keys.delete(ssh_key)
                ssh_key = client.ssh_keys.create(
                    name=key_name, public_key=pub_content,
                )
        else:
            raise

    # Create server
    from hcloud.images import Image
    from hcloud.locations import Location
    from hcloud.server_types import ServerType

    click.echo(f"Creating server '{server_name}' ({st} in {loc})...")
    cloud_init = _get_cloud_init(config)

    try:
        response = client.servers.create(
            name=server_name,
            server_type=ServerType(name=st),
            image=Image(name="ubuntu-24.04"),
            location=Location(name=loc),
            ssh_keys=[ssh_key],
            user_data=cloud_init,
        )
    except Exception as e:
        _handle_create_error(e, client, st, loc, ssh_key=ssh_key)
        raise  # unreachable, _handle_create_error always raises
    server = response.server

    ipv4 = server.data_model.public_net.ipv4.ip if server.data_model.public_net.ipv4 else None
    if not ipv4:
        # IP not assigned yet — wait briefly and re-fetch
        click.echo("Waiting for IP assignment...", nl=False)
        for _ in range(12):
            time.sleep(5)
            server = client.servers.get_by_id(server.data_model.id)
            if server and server.data_model.public_net.ipv4:
                ipv4 = server.data_model.public_net.ipv4.ip
                if ipv4:
                    break
            click.echo(".", nl=False)
        click.echo()
        if not ipv4:
            raise click.ClickException("Server created but no IPv4 assigned.")
    click.echo(f"Server created: {server_name} (ID: {server.data_model.id}, IP: {ipv4})")

    # Save state
    state = {
        "provider": "hetzner",
        "server_id": server.data_model.id,
        "server_name": server_name,
        "ipv4": ipv4,
        "server_type": st,
        "location": loc,
        "ssh_key_id": ssh_key.data_model.id,
    }
    _save_state(state)

    # Wait for readiness
    _wait_for_ready(ipv4)

    click.echo(f"Server '{server_name}' is ready.")
    return state


def _wait_for_ready(ipv4: str, timeout: int = 300):
    """Wait for the cloud server to be SSH-reachable and cloud-init complete."""
    click.echo("Waiting for server to be ready...", nl=False)
    start = time.monotonic()
    interval = 5

    while time.monotonic() - start < timeout:
        try:
            result = _ssh_run(
                ipv4,
                "test -f /var/run/bubble-cloud-ready && echo ready",
                timeout=10,
                check=False,
            )
            if result.returncode == 0 and "ready" in result.stdout:
                click.echo(" ready!")
                return
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass

        click.echo(".", nl=False)
        time.sleep(interval)

    click.echo(" timeout!")
    raise click.ClickException(
        f"Server at {ipv4} did not become ready within {timeout}s.\n"
        f"Check cloud-init logs: ssh root@{ipv4}"
        " 'cat /var/log/cloud-init-output.log'"
    )


def destroy_server(force: bool = False):
    """Destroy the cloud server and clean up."""
    state = _load_state()
    if not state:
        raise click.ClickException("No cloud server to destroy.")

    server_name = state.get("server_name", "unknown")
    if not force:
        click.confirm(
            f"Permanently destroy cloud server '{server_name}' ({state.get('ipv4', '?')})?\n"
            "All containers on this server will be lost.",
            abort=True,
        )

    client = _get_client()

    # Delete server
    server_id = state.get("server_id")
    if server_id:
        click.echo(f"Deleting server {server_name} (ID: {server_id})...")
        try:
            server = client.servers.get_by_id(server_id)
            if server:
                client.servers.delete(server)
        except Exception as e:
            click.echo(f"Warning: could not delete server: {e}")
            click.echo(
                "State preserved for retry. "
                "Use 'bubble cloud destroy -f' to force cleanup."
            )
            return

    # Delete SSH key from Hetzner
    ssh_key_id = state.get("ssh_key_id")
    if ssh_key_id:
        try:
            key = client.ssh_keys.get_by_id(ssh_key_id)
            if key:
                client.ssh_keys.delete(key)
                click.echo("Removed SSH key from Hetzner.")
        except Exception:
            pass

    # Clean up known_hosts entries for this IP
    ipv4 = state.get("ipv4", "")
    if ipv4 and CLOUD_KNOWN_HOSTS.exists():
        try:
            subprocess.run(
                ["ssh-keygen", "-R", ipv4, "-f", str(CLOUD_KNOWN_HOSTS)],
                capture_output=True, check=False,
            )
        except FileNotFoundError:
            pass

    _clear_state()
    click.echo(f"Cloud server '{server_name}' destroyed.")


def stop_server():
    """Power off the cloud server (stops hourly billing)."""
    state = _load_state()
    if not state:
        raise click.ClickException("No cloud server configured.")

    client = _get_client()
    server = client.servers.get_by_id(state["server_id"])
    if not server:
        raise click.ClickException(f"Server ID {state['server_id']} not found on Hetzner.")

    status = server.data_model.status
    if status == "off":
        click.echo("Server is already off.")
        return

    click.echo(f"Powering off '{state['server_name']}'...")
    client.servers.power_off(server)
    click.echo("Server powered off. Hourly billing stopped.")


def start_server():
    """Power on the cloud server and wait for SSH."""
    state = _load_state()
    if not state:
        raise click.ClickException("No cloud server configured.")

    client = _get_client()
    server = client.servers.get_by_id(state["server_id"])
    if not server:
        raise click.ClickException(f"Server ID {state['server_id']} not found on Hetzner.")

    status = server.data_model.status
    if status == "running":
        click.echo("Server is already running.")
        # Re-fetch IP (might have changed)
        _update_ip(client, state)
        return

    click.echo(f"Starting '{state['server_name']}'...")
    client.servers.power_on(server)

    # Wait for the server to get its IP
    time.sleep(3)
    _update_ip(client, state)

    # Wait for SSH
    _wait_for_ssh(state["ipv4"])
    click.echo("Server is running.")


def _update_ip(client, state: dict):
    """Re-fetch the server's IPv4 and update state."""
    server = client.servers.get_by_id(state["server_id"])
    if server and server.data_model.public_net.ipv4:
        new_ip = server.data_model.public_net.ipv4.ip
        if new_ip and new_ip != state.get("ipv4"):
            old_ip = state.get("ipv4", "")
            state["ipv4"] = new_ip
            _save_state(state)
            click.echo(f"IP updated: {old_ip} -> {new_ip}")
            # Clean old IP from known_hosts
            if old_ip and CLOUD_KNOWN_HOSTS.exists():
                subprocess.run(
                    ["ssh-keygen", "-R", old_ip, "-f", str(CLOUD_KNOWN_HOSTS)],
                    capture_output=True, check=False,
                )


def _wait_for_ssh(ipv4: str, timeout: int = 120):
    """Wait for SSH to become reachable."""
    click.echo("Waiting for SSH...", nl=False)
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        try:
            result = _ssh_run(ipv4, "echo ok", timeout=10, check=False)
            if result.returncode == 0:
                click.echo(" connected!")
                return
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass

        click.echo(".", nl=False)
        time.sleep(5)

    click.echo(" timeout!")
    raise click.ClickException(f"Cannot reach {ipv4} via SSH after {timeout}s.")


def get_server_status() -> dict | None:
    """Get cloud server status. Returns enriched state dict or None."""
    state = _load_state()
    if not state:
        return None

    try:
        client = _get_client()
        server = client.servers.get_by_id(state["server_id"])
        if server:
            state["status"] = server.data_model.status
            st = server.data_model.server_type
            state["server_type_description"] = st.description if st else ""
            # Prices are per-location, just show the type name
        else:
            state["status"] = "not_found"
    except Exception as e:
        state["status"] = f"api_error: {e}"

    return state


def get_cloud_remote_host(config: dict) -> RemoteHost:
    """Get the cloud server as a RemoteHost, auto-starting if needed.

    This is the main integration point called from cli.py when --cloud is used.
    """
    state = _load_state()
    if not state:
        raise click.ClickException(
            "No cloud server provisioned.\n"
            "Set one up with: bubble cloud provision"
        )

    client = _get_client()
    server = client.servers.get_by_id(state["server_id"])
    if not server:
        raise click.ClickException(
            f"Cloud server ID {state['server_id']} not found on Hetzner.\n"
            "It may have been deleted externally. Run: bubble cloud destroy"
        )

    status = server.data_model.status
    if status == "off":
        click.echo("Cloud server is off, starting...")
        client.servers.power_on(server)
        time.sleep(3)
        _update_ip(client, state)
        _wait_for_ssh(state["ipv4"])
    elif status != "running":
        raise click.ClickException(
            f"Cloud server is in unexpected state: {status}\n"
            "Check with: bubble cloud status"
        )
    else:
        # Running — refresh IP just in case
        _update_ip(client, state)

    return RemoteHost(
        hostname=state["ipv4"],
        user="root",
        port=22,
        ssh_options=[
            "-i", str(CLOUD_KEY_FILE),
            "-o", "IdentitiesOnly=yes",
            "-o", f"UserKnownHostsFile={CLOUD_KNOWN_HOSTS}",
            "-o", "StrictHostKeyChecking=accept-new",
        ],
    )


def cloud_ssh(args: list[str] | None = None):
    """SSH directly to the cloud server."""
    state = _load_state()
    if not state:
        raise click.ClickException("No cloud server configured.")

    ipv4 = state.get("ipv4", "")
    if not ipv4:
        raise click.ClickException("No IP address in cloud state.")

    cmd = _ssh_cmd_base() + [f"root@{ipv4}"]
    if args:
        cmd += args
    os.execvp(cmd[0], cmd)
