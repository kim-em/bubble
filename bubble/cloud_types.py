"""Server type probing, error handling, and listing for Hetzner Cloud."""

import click

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


def handle_create_error(exc: Exception, client, server_type: str, location: str, ssh_key=None):
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
    from .cloud import _get_client

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

    click.echo("\nTo provision:  bubble cloud provision --type <name>")
    click.echo("Other locations: --location nbg1|hel1|ash|hil")
