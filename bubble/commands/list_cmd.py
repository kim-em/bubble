"""The 'list' command and its helper functions."""

import json
import shutil
import unicodedata
from pathlib import Path

import click

from ..clean import CleanStatus, check_clean, check_native_clean
from ..config import load_config
from ..lifecycle import load_registry
from ..setup import get_runtime


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def _format_age(dt: "datetime | None") -> str:  # noqa: F821
    """Format a datetime as a human-readable age string."""
    if dt is None:
        return "-"
    from datetime import datetime, timezone

    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


def _char_width(ch: str) -> int:
    """Return the display width of a single character."""
    if unicodedata.combining(ch) or ch in ("\u200b", "\u200c", "\u200d", "\ufe0f", "\ufe0e"):
        return 0
    cat = unicodedata.east_asian_width(ch)
    return 2 if cat in ("W", "F") else 1


def _display_width(s: str) -> int:
    """Return the display width of *s*, counting wide Unicode chars as 2."""
    return sum(_char_width(ch) for ch in s)


def _truncate(s: str, max_width: int) -> str:
    """Truncate *s* to *max_width* display columns, adding ``...`` if needed."""
    if _display_width(s) <= max_width:
        return s
    if max_width <= 3:
        return "." * max_width
    result: list[str] = []
    w = 0
    for ch in s:
        cw = _char_width(ch)
        if w + cw + 3 > max_width:  # leave room for "..."
            break
        result.append(ch)
        w += cw
    return "".join(result) + "..."


def _pad(s: str, width: int) -> str:
    """Left-align *s* in a field of *width* display columns."""
    return s + " " * max(0, width - _display_width(s))


def _parse_iso(s: str | None):
    """Parse an ISO datetime string, returning None on failure."""
    if not s:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_cloud_host(hostname: str) -> bool:
    """Check if a hostname matches the cloud server IP."""
    from ..cloud import _load_state

    state = _load_state()
    return bool(state and state.get("ipv4") == hostname)


def _remote_entries_from_registry() -> list[dict]:
    """Build list entries for remote bubbles from the registry.

    Returns a list of dicts with keys: name, state, location, created_at,
    last_used_at, remote_host_spec.  State defaults to ``"unknown"``.
    """
    registry = load_registry()
    entries = []
    for name, info in registry.get("bubbles", {}).items():
        host_spec = info.get("remote_host", "")
        if not host_spec:
            continue
        from ..remote import RemoteHost

        try:
            host = RemoteHost.parse(host_spec)
        except ValueError:
            continue
        location = "cloud" if _is_cloud_host(host.hostname) else host_spec
        entries.append(
            {
                "name": name,
                "state": "unknown",
                "location": location,
                "created_at": _parse_iso(info.get("created_at")),
                "last_used_at": None,
                "remote_host_spec": host_spec,
            }
        )
    return entries


def _native_entries_from_registry(show_clean: bool = False) -> list[dict]:
    """Build list entries for native workspaces from the registry."""
    registry = load_registry()
    entries = []
    for name, info in registry.get("bubbles", {}).items():
        if not info.get("native"):
            continue
        native_path = info.get("native_path", "")
        state = "exists" if native_path and Path(native_path).is_dir() else "missing"
        entry = {
            "name": name,
            "state": state,
            "location": "native",
            "created_at": _parse_iso(info.get("created_at")),
            "last_used_at": None,
            "native_path": native_path,
        }
        if show_clean and state == "exists":
            entry["clean_status"] = check_native_clean(native_path, name)
        entries.append(entry)
    return entries


def _query_remote_list(
    host_spec: str, is_cloud: bool, verbose: bool = False, timeout: int = 15
) -> list[dict] | None:
    """Query a remote host for its bubble list via SSH.

    For cloud hosts, checks the Hetzner API first and skips SSH when the
    server is off.  Returns parsed JSON list or *None* on failure.
    """
    if is_cloud:
        try:
            from ..cloud import get_server_status

            status = get_server_status()
            if not status or status.get("status") != "running":
                return None
        except Exception:
            return None

    from ..remote import RemoteHost, apply_cloud_ssh_options, remote_bubble

    try:
        host = RemoteHost.parse(host_spec)
        if is_cloud:
            apply_cloud_ssh_options(host)
        args = ["list", "--json"]
        if verbose:
            args.append("-v")
        result = remote_bubble(host, args, timeout=timeout)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def register_list_command(main):
    """Register the 'list' command on the main CLI group."""

    @main.command("list")
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON")
    @click.option("-v", "--verbose", is_flag=True, help="Include disk usage and IPv4 (slower)")
    @click.option(
        "-c", "--clean", "show_clean", is_flag=True, help="Check cleanness status (slower)"
    )
    @click.option("--cloud", "query_cloud", is_flag=True, help="Query cloud server for live status")
    @click.option("--ssh", "ssh_host", default=None, help="Query SSH host for live status")
    @click.option("--local", "local_only", is_flag=True, help="Show only local bubbles")
    def list_bubbles(as_json, verbose, show_clean, query_cloud, ssh_host, local_only):
        """List all bubbles."""
        from ..notices import maybe_print_welcome

        config = load_config()
        if not as_json:
            maybe_print_welcome()
        runtime = get_runtime(config, ensure_ready=False)

        # --- Local containers ---
        from ..images.builder import is_builder_container

        containers = [
            c for c in runtime.list_containers(fast=not verbose) if not is_builder_container(c.name)
        ]

        clean_statuses = {}
        if show_clean:
            for c in containers:
                if c.state == "running":
                    clean_statuses[c.name] = check_clean(runtime, c.name)
                else:
                    clean_statuses[c.name] = CleanStatus(clean=False, error="not running")

        local_names = {c.name for c in containers}
        entries = []
        for c in containers:
            entry = {
                "name": c.name,
                "state": c.state,
                "location": "local",
                "created_at": c.created_at,
                "last_used_at": c.last_used_at,
            }
            if verbose:
                entry["ipv4"] = c.ipv4
                entry["disk_usage"] = c.disk_usage
            if show_clean:
                entry["clean_status"] = clean_statuses.get(c.name)
            entries.append(entry)

        # --- Remote bubbles from registry ---
        has_remote = False
        if not local_only:
            remote_entries = _remote_entries_from_registry()
            # Skip any that are also local (shouldn't happen, but be safe)
            remote_entries = [e for e in remote_entries if e["name"] not in local_names]
            has_remote = bool(remote_entries)

            # Live queries: group remote entries by (host_spec, is_cloud)
            queried_hosts: dict[str, list[dict] | None] = {}
            if query_cloud or ssh_host:
                # Determine which host specs to query
                specs_to_query: set[str] = set()
                for e in remote_entries:
                    is_cloud = e["location"] == "cloud"
                    if is_cloud and query_cloud:
                        specs_to_query.add(e["remote_host_spec"])
                    elif not is_cloud and ssh_host:
                        from ..remote import RemoteHost

                        try:
                            query_host = RemoteHost.parse(ssh_host)
                            entry_host = RemoteHost.parse(e["remote_host_spec"])
                            if query_host.hostname == entry_host.hostname:
                                specs_to_query.add(e["remote_host_spec"])
                        except ValueError:
                            pass

                if ssh_host and not specs_to_query:
                    click.echo(
                        f"Warning: no remote bubbles found for host '{ssh_host}'.",
                        err=True,
                    )

                for spec in specs_to_query:
                    is_cloud = any(
                        e["remote_host_spec"] == spec and e["location"] == "cloud"
                        for e in remote_entries
                    )
                    queried_hosts[spec] = _query_remote_list(
                        spec,
                        is_cloud,
                        verbose=verbose,
                    )

            # Update remote entries with live data where available
            for e in remote_entries:
                spec = e["remote_host_spec"]
                live_data = queried_hosts.get(spec)
                if live_data is not None:
                    # Find matching container in live data
                    for rc in live_data:
                        if rc["name"] == e["name"]:
                            e["state"] = rc.get("state", "unknown")
                            e["created_at"] = _parse_iso(rc.get("created_at")) or e["created_at"]
                            e["last_used_at"] = _parse_iso(rc.get("last_used_at"))
                            if verbose:
                                e["ipv4"] = rc.get("ipv4")
                                e["disk_usage"] = rc.get("disk_usage")
                            break
                    else:
                        # Registered locally but not found on remote
                        e["state"] = "not found"
                elif spec in queried_hosts:
                    # Query was attempted but failed
                    is_cloud = e["location"] == "cloud"
                    if is_cloud:
                        try:
                            from ..cloud import get_server_status

                            status = get_server_status()
                            if status and status.get("status") == "off":
                                e["state"] = "server off"
                            else:
                                e["state"] = "unreachable"
                        except Exception:
                            e["state"] = "unreachable"
                    else:
                        e["state"] = "unreachable"

            entries.extend(remote_entries)

        # --- Native workspaces from registry (always local) ---
        native_entries = _native_entries_from_registry(show_clean=show_clean)
        native_entries = [e for e in native_entries if e["name"] not in local_names]
        entries.extend(native_entries)
        if native_entries:
            has_remote = True  # Force showing location column

        # --- Output ---
        if as_json:
            data = []
            for e in entries:
                d = {
                    "name": e["name"],
                    "state": e["state"],
                    "location": e["location"],
                    "created_at": (
                        e["created_at"].isoformat()
                        if hasattr(e.get("created_at"), "isoformat")
                        else e.get("created_at")
                    ),
                    "last_used_at": (
                        e["last_used_at"].isoformat()
                        if hasattr(e.get("last_used_at"), "isoformat")
                        else e.get("last_used_at")
                    ),
                }
                if verbose:
                    d["ipv4"] = e.get("ipv4")
                    d["disk_usage"] = e.get("disk_usage")
                if show_clean:
                    cs = e.get("clean_status")
                    if cs and cs.error:
                        d["clean"] = None
                    elif cs:
                        d["clean"] = {"status": cs.clean, "reasons": cs.reasons}
                data.append(d)
            click.echo(json.dumps(data, indent=2))
            return

        if not entries:
            click.echo("No bubbles. Create one with: bubble owner/repo")
            return

        # Build header and rows based on flags
        show_location = has_remote or local_only

        # --- Compute dynamic column widths ---
        MAX_NAME = 50
        MAX_LOCATION = 30
        name_w = max(len("NAME"), *(min(_display_width(e["name"]), MAX_NAME) for e in entries))
        if show_location:
            loc_w = max(
                len("LOCATION"),
                *(min(_display_width(e["location"]), MAX_LOCATION) for e in entries),
            )
        else:
            loc_w = 0

        # Fixed-width columns
        STATE_W = 12
        TIME_W = 12
        DISK_W = 10
        IPV4_W = 16

        # Clamp dynamic columns if total exceeds terminal width
        term_w = shutil.get_terminal_size((80, 24)).columns
        fixed = STATE_W + 2 * TIME_W + 3  # separating spaces
        if show_location:
            fixed += 1  # space before LOCATION (loc_w added separately)
        if verbose:
            fixed += DISK_W + IPV4_W + 2
        if show_clean:
            fixed += 8  # rough allowance for STATUS
        avail = term_w - fixed
        if name_w + loc_w > avail:
            # Shrink name first, then location if needed
            name_w = max(len("NAME"), avail - loc_w)
            if name_w + loc_w > avail:
                loc_w = max(len("LOCATION") if show_location else 0, avail - name_w)

        # Truncate values to their column widths
        names = [_truncate(e["name"], name_w) for e in entries]
        locations = [_truncate(e["location"], loc_w) for e in entries] if show_location else []

        header = _pad("NAME", name_w) + " " + _pad("STATE", STATE_W)
        if show_location:
            header += " " + _pad("LOCATION", loc_w)
        header += " " + _pad("CREATED", TIME_W) + " " + _pad("LAST USED", TIME_W)
        if verbose:
            header += " " + _pad("DISK", DISK_W) + " " + _pad("IPv4", IPV4_W)
        if show_clean:
            header += " STATUS"
        click.echo(header)
        click.echo("-" * _display_width(header))
        for i, e in enumerate(entries):
            created = _format_age(e.get("created_at"))
            used = _format_age(e.get("last_used_at"))
            line = _pad(names[i], name_w) + " " + _pad(e["state"], STATE_W)
            if show_location:
                line += " " + _pad(locations[i], loc_w)
            line += " " + _pad(created, TIME_W) + " " + _pad(used, TIME_W)
            if verbose:
                disk = _format_bytes(e["disk_usage"]) if e.get("disk_usage") else "-"
                ipv4 = e.get("ipv4") or "-"
                line += " " + _pad(disk, DISK_W) + " " + _pad(ipv4, IPV4_W)
            if show_clean:
                cs = e.get("clean_status")
                line += " " + cs.summary if cs else ""
            click.echo(line)

        # Help text hints
        hints = []
        if not verbose:
            hints.append("-v for disk usage")
        if not show_clean:
            hints.append("-c for cleanness")
        if has_remote and not query_cloud:
            # Check if any remote entries are cloud
            if any(e["location"] == "cloud" for e in entries):
                hints.append("--cloud for live cloud status")
        if has_remote and not ssh_host:
            ssh_hosts = {
                e["remote_host_spec"]
                for e in entries
                if e.get("remote_host_spec") and e["location"] != "cloud"
            }
            if ssh_hosts:
                hints.append("--ssh HOST for live remote status")
        if hints:
            click.echo(f"\nUse {', '.join(hints)}.")
