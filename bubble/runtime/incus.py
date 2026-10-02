"""Incus container runtime implementation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone

from .base import ContainerInfo, ContainerRuntime

# Number of attempts and base backoff for transient incusd errors. Under
# heavy host load the incusd REST socket intermittently drops requests
# ("EOF", "connection refused"); these are not real failures, just the
# daemon being momentarily overwhelmed. Retrying with backoff keeps bubble
# working on busy machines (e.g. a shared box running many builds).
_TRANSIENT_RETRIES = 5
_TRANSIENT_BACKOFF_BASE = 0.5  # seconds: 0.5, 1, 2, 4, 8


def _is_transient_incus_error(text: str) -> bool:
    """True if *text* looks like a transient incusd connection hiccup.

    These are socket-level failures talking to incusd, distinct from real
    errors like "Instance not found" or "Device doesn't exist" which must
    surface immediately.
    """
    if not text:
        return False
    low = text.lower()
    # Examples seen under load:
    #   Error: Put "http://unix.socket/1.0/instances/x": EOF
    #   Error: Get "http://unix.socket/1.0/...": EOF
    #   Error: Put "http://unix.socket/...": read ...: connection reset by peer
    if "unix.socket" in low and ("eof" in low or "connection reset" in low):
        return True
    if "connection refused" in low and "unix.socket" in low:
        return True
    return False


class IncusError(subprocess.CalledProcessError, RuntimeError):
    """Error from an incus command.

    Inherits from both CalledProcessError (for returncode/cmd/stderr fields)
    and RuntimeError (so ``except RuntimeError`` catches it, matching the
    ContainerRuntime exception contract).
    """

    def __str__(self):
        detail = (self.stderr or self.stdout or "").strip()
        base = f"incus {' '.join(self.cmd[1:])} failed (exit {self.returncode})"
        if detail:
            return f"{base}: {detail}"
        return base


class IncusRuntime(ContainerRuntime):
    """Container runtime using Incus.

    The optional ``remote`` constructor argument names a non-default
    Incus remote that all resource references will be prefixed with
    (e.g. ``"bubble-colima"`` on macOS).  When empty, container/image
    names are passed through unchanged and the user's current default
    remote applies — bubble does not switch it.
    """

    def __init__(self, remote: str = ""):
        self._remote = remote

    def qualify(self, name: str) -> str:
        """Prefix *name* with our remote if one is configured.

        Names that already contain ``:`` are assumed to be explicitly
        qualified by the caller and pass through unchanged.
        """
        if self._remote and ":" not in name:
            return f"{self._remote}:{name}"
        return name

    def _q(self, name: str) -> str:
        # Internal alias matching the public method, kept short so call sites
        # stay readable.
        return self.qualify(name)

    def _run_subprocess(
        self, cmd: list[str], *, capture: bool = True
    ) -> subprocess.CompletedProcess:
        """Run *cmd*, retrying on transient incusd connection errors.

        Returns the CompletedProcess (does not raise on nonzero exit — the
        caller decides). Retries only when the daemon socket hiccups under
        load; real nonzero exits (bad args, missing instance, etc.) return
        immediately so callers see the actual error.
        """
        last: subprocess.CompletedProcess | None = None
        for attempt in range(_TRANSIENT_RETRIES):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=capture,
                    text=True,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                from ..setup import _ensure_dependencies

                _ensure_dependencies()
                # _ensure_dependencies exits if incus is missing; if we get
                # here it was installed, so retry this attempt once.
                result = subprocess.run(
                    cmd,
                    capture_output=capture,
                    text=True,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
            last = result
            if result.returncode == 0:
                return result
            stderr = result.stderr or ""
            if attempt < _TRANSIENT_RETRIES - 1 and _is_transient_incus_error(stderr):
                time.sleep(_TRANSIENT_BACKOFF_BASE * (2**attempt))
                continue
            return result
        assert last is not None  # loop runs at least once
        return last

    def _run(self, args: list[str], check: bool = True, capture: bool = True) -> str:
        """Run an incus command (with transient-error retry)."""
        cmd = ["incus"] + args
        result = self._run_subprocess(cmd, capture=capture)
        if check and result.returncode != 0:
            raise IncusError(result.returncode, cmd, result.stdout, result.stderr)
        return (result.stdout or "").strip() if capture else ""

    def _run_json(self, args: list[str]) -> dict | list:
        """Run an incus command and parse JSON output."""
        output = self._run(args + ["--format=json"])
        if not output:
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON from incus {' '.join(args)}: {e}") from None

    def is_available(self) -> bool:
        try:
            self._run(["version"])
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    # Per container: whether the operator's ids are mapped onto its `user` (set by launch(), read
    # back from the instance config for containers this runtime did not launch). add_disk mounts
    # a mapped container's disks plainly: a shifted mount presents host ids unchanged and would undo
    # the map (measured on Incus 7.4 / kernel 7.1: under `uid 1005 1001` a plain mount of a
    # 1005-owned 0600 file reads and writes as `user`; the same directory shifted is 1005 inside
    # and denied). Not one flag for the whole runtime: several containers, and other runtime
    # instances (a refresh after restart), must each see the mapping actually in force.
    _mapped: dict[str, bool]
    _daemon_local: bool | None = None

    def _mapping_in_force(self, name: str) -> bool:
        """Is `raw.idmap` set on this instance (by us or by anyone)? Cached per name."""
        cache = self.__dict__.setdefault("_mapped", {})
        if name not in cache:
            try:
                cache[name] = bool(self._run(["config", "get", self._q(name), "raw.idmap"]).strip())
            except IncusError:
                cache[name] = False
        return cache[name]

    def _is_local_daemon(self) -> bool:
        """Whether the daemon our unqualified names reach is the host's own (a unix socket).
        A configured remote is by definition not; an empty remote means the client's default
        remote, which is only local when its address is a unix socket. Host uids, subuid files
        and the operator's identity mean nothing on any other server. Cached."""
        if self._remote:
            return False
        if self._daemon_local is None:
            try:
                default = self._run(["remote", "get-default"]).strip()
                remotes = json.loads(self._run(["remote", "list", "--format", "json"]) or "{}")
                entry = remotes.get(default) or {}
                addrs = entry.get("Addrs") or [entry.get("Addr") or ""]  # Incus 7 lists `Addrs`
                self._daemon_local = any(str(a).startswith("unix:") for a in addrs)
            except (IncusError, ValueError):
                self._daemon_local = False
        return self._daemon_local

    @staticmethod
    def _subid_allows(path: str, wanted: int) -> bool | None:
        """Whether /etc/subuid (or subgid) lets the Incus daemon (root) use `wanted`. None when
        the file cannot be read, which is not a refusal: some hosts do not use the files."""
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError:
            return None
        for line in lines:
            parts = line.strip().split(":")
            if len(parts) != 3 or parts[0] not in ("root", "0"):
                continue
            try:
                start, count = int(parts[1]), int(parts[2])
            except ValueError:
                continue
            if start <= wanted < start + count:
                return True
        return False

    def _container_user_ids(self, name: str) -> tuple[int, int] | None:
        """The uid/gid of the image's `user` account, read from the stopped container's
        /etc/passwd. Not a constant: on an Ubuntu image the stock `ubuntu` account holds 1000 and
        `useradd user` lands on 1001."""
        try:
            passwd = self._run(["file", "pull", f"{self._q(name)}/etc/passwd", "-"])
        except IncusError:
            return None
        for line in passwd.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[0] == "user":
                try:
                    return int(parts[2]), int(parts[3])
                except ValueError:
                    return None
        return None

    def _idmap_for(self, container_uid: int, container_gid: int) -> tuple[str, str]:
        """(raw.idmap lines, hint). Empty value when no mapping is wanted or allowed.

        Native Incus gives the container its own id range, so everything bubble hands it from
        the operator's home (the checkout, credential files, the Lake mirrors, a review store)
        arrives owned by somebody else: writes fail, git calls the mirrors "dubious". Colima maps
        the operator onto the VM's user, which is what makes those mounts just work on macOS.
        raw.idmap is the same mapping on the host, but Incus honours it only for ids listed for
        root in /etc/subuid and /etc/subgid, so check first and tell the operator the lines to
        add when they are missing. The uid and the gid are handled separately: only the id that
        differs is mapped, and only its subordinate file has to allow it (an operator whose primary
        group already matches the container user's must not be refused over an unneeded gid map)."""
        if not self._is_local_daemon():
            return "", ""
        uid, gid = os.getuid(), os.getgid()
        wanted = []
        if uid != container_uid:
            wanted.append(("uid", uid, container_uid, "/etc/subuid"))
        if gid != container_gid:
            wanted.append(("gid", gid, container_gid, "/etc/subgid"))
        if not wanted:
            return "", ""
        adds = " && ".join(
            f"echo 'root:{host}:1' | sudo tee -a {path}" for _, host, _, path in wanted
        )
        hint = (
            "bubble: the container's user cannot own files you mount in "
            f"(host {' and '.join(f'{k} {host}' for k, host, _, _ in wanted)} not mapped). "
            "Allow Incus to map it and restart the daemon:\n"
            f"  {adds} && sudo systemctl restart incus"
        )
        if any(self._subid_allows(path, host) is False for _, host, _, path in wanted):
            return "", hint
        return "\n".join(f"{k} {host} {ctr}" for k, host, ctr, _ in wanted), hint

    @staticmethod
    def _merge_idmap(existing: str, lines: str) -> str:
        """Add our lines to an instance's effective raw.idmap (its own or a profile's) instead of
        replacing it. Every line about other ids survives, and so does a line that already maps one
        of our host ids onto the container id we want.

        A line mapping one of our host ids onto a DIFFERENT container id is replaced, on this
        instance only (the profile is not touched): the container's `user` must own what bubble
        mounts in, and a map elsewhere leaves every mount unusable while the container counts as
        mapped. incus-user's restricted projects carry exactly such a line in their default
        profile, `uid <uid> <uid>`, for the user who owns the project. A `both` line for that host
        id keeps the half we do not map."""
        ours = {}
        for ln in lines.splitlines():
            parts = ln.split()
            if len(parts) >= 3:
                ours[(parts[0], parts[1])] = parts[2]
        kept = []
        for ln in (existing or "").splitlines():
            parts = ln.split()
            if not parts:
                continue
            if len(parts) >= 3 and parts[0] == "both":
                clash = [k for k in ("uid", "gid") if ours.get((k, parts[1]), parts[2]) != parts[2]]
                if clash:
                    kept += [f"{k} {parts[1]} {parts[2]}" for k in ("uid", "gid") if k not in clash]
                    continue
            elif len(parts) >= 3 and ours.get((parts[0], parts[1]), parts[2]) != parts[2]:
                continue  # our host id mapped elsewhere: replaced by our line below
            kept.append(ln.strip())
        covered = set()
        for ln in kept:
            parts = ln.split()
            if len(parts) >= 2:
                covered |= (
                    {("uid", parts[1]), ("gid", parts[1])}
                    if parts[0] == "both"
                    else {(parts[0], parts[1])}
                )
        kept += [f"{k} {h} {c}" for (k, h), c in ours.items() if (k, h) not in covered]
        return "\n".join(kept)

    # What Incus says when it will not apply a raw.idmap: the id is outside root's subordinate
    # ranges, or the map is malformed. Matched against the command's OUTPUT only (never its argv,
    # which itself contains "raw.idmap"), so an instance whose NAME contains "idmap" cannot trip it.
    _IDMAP_REFUSED_RE = re.compile(
        r"invalid idmap|not allowed by sub[ug]id|idmap.*(not allowed|not permitted|out of range)",
        re.IGNORECASE,
    )
    # What Incus says when a disk cannot be shifted on this pool/kernel. Same rule: output only.
    _SHIFT_UNSUPPORTED_RE = re.compile(
        r"(idmapped mounts?|shift(ing)?)[^\n]*(not supported|unsupported|not available)"
        r"|(not supported|unsupported)[^\n]*(shift|idmapped mounts?)",
        re.IGNORECASE,
    )

    def launch(self, name: str, image: str, **kwargs) -> ContainerInfo:
        """Create the container stopped, map the operator onto its `user` when the host allows,
        then start it. A refused mapping is reported on stdout and the container starts unmapped.
        Nothing here deletes an instance: a failure after `init` leaves the stopped container for
        the caller's cleanup, and a failed mapping is reported, never retried destructively."""
        mapped = self.__dict__.setdefault("_mapped", {})
        mapped.pop(name, None)
        if not self._is_local_daemon():
            # A configured remote (Colima on macOS) or a non-local default remote maps the
            # operator onto its own user already, or is not ours to reason about: launch in one
            # step, exactly as before this mapping existed.
            self._run(["launch", self._q(image), self._q(name)])
            mapped[name] = False
            return self._get_info(name)
        self._run(["init", self._q(image), self._q(name)])
        ids = self._container_user_ids(name)
        if ids is not None:
            idmap, hint = self._idmap_for(*ids)
            if idmap:
                try:
                    existing = self._run(["config", "get", "-e", self._q(name), "raw.idmap"])
                except IncusError:
                    existing = ""
                merged = self._merge_idmap(existing, idmap)
                if merged.strip() == (existing or "").strip():
                    mapped[name] = True  # a profile or the operator already maps these ids
                else:
                    try:
                        self._run(["config", "set", self._q(name), f"raw.idmap={merged}"])
                        mapped[name] = True
                    except IncusError as e:
                        detail = f"{e.output or ''}\n{e.stderr or ''}"
                        if not self._IDMAP_REFUSED_RE.search(detail):
                            raise
                        # Incus refused the mapping: the daemon has not been restarted since the
                        # subid files changed, or its allowed ranges differ from them.
                        hint = hint or (
                            "bubble: Incus refused to map your uid into the container (raw.idmap); "
                            "if you just added it to /etc/subuid and /etc/subgid, restart the "
                            "daemon: sudo systemctl restart incus"
                        )
            if hint and not mapped.get(name):
                # stdout, like every other bubble progress line: a caller that captures stderr
                # separately (the TauCeti worker streams only stdout) would otherwise never show it.
                print(hint, flush=True)
        mapped.setdefault(name, False)
        self._run(["start", self._q(name)])
        return self._get_info(name)

    @staticmethod
    def _parse_container(c: dict) -> ContainerInfo:
        """Parse Incus container JSON into ContainerInfo."""
        ipv4 = None
        state = c.get("state") or {}
        network = state.get("network") or {}
        eth0 = network.get("eth0") or {}
        for addr in eth0.get("addresses", []):
            if addr["family"] == "inet":
                ipv4 = addr["address"]
                break
        disk_usage = None
        disk = state.get("disk") or {}
        root = disk.get("root") or {}
        if root.get("usage"):
            disk_usage = root["usage"]

        def _parse_ts(key: str) -> datetime | None:
            raw = c.get(key)
            if not raw or raw.startswith("0001-"):
                return None
            # Incus uses RFC 3339 with nanoseconds; truncate to microseconds
            raw = raw.rstrip("Z")
            if "." in raw:
                base, frac = raw.split(".", 1)
                raw = f"{base}.{frac[:6]}"
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)

        state_map = {"Running": "running", "Stopped": "stopped", "Frozen": "frozen"}
        return ContainerInfo(
            name=c["name"],
            state=state_map.get(c["status"], c["status"].lower()),
            ipv4=ipv4,
            image=(c.get("config") or {}).get("volatile.base_image"),
            disk_usage=disk_usage,
            created_at=_parse_ts("created_at"),
            last_used_at=_parse_ts("last_used_at"),
        )

    def _get_info(self, name: str) -> ContainerInfo:
        """Get info for a single container.

        Passes the remote scope and the name filter as *separate*
        arguments (``incus list <remote>: name=<name>``). Concatenating
        them into one token (``incus list <remote>:<name>``) breaks on
        recent incus versions: a ``list`` argument that doesn't end in
        ``:`` is treated as a name filter on the *default* remote, so it
        matches nothing on a non-default remote (e.g. ``bubble-colima``
        on macOS). The bare ``<remote>:`` token reliably scopes the list
        to the remote, the same way the no-name path in
        ``list_containers`` already relies on.

        ``name=<name>`` is matched as a substring on some incus versions,
        so we still confirm an exact name match before returning.
        """
        scope = [self._q("")] if self._remote else []
        data = self._run_json(["list", *scope, f"name={name}"])
        if isinstance(data, list):
            for c in data:
                if c.get("name") == name:
                    return self._parse_container(c)
        raise RuntimeError(f"Container '{name}' not found")

    def list_containers(self, fast: bool = True) -> list[ContainerInfo]:
        # When a remote is set, pass "remote:" with no name so list scopes
        # to that remote rather than the user's default.
        args = ["list", self._q("")] if self._remote else ["list"]
        if fast:
            args.append("--fast")
        data = self._run_json(args)
        if not isinstance(data, list):
            return []
        return [self._parse_container(c) for c in data]

    def start(self, name: str):
        self._run(["start", self._q(name)])

    def stop(self, name: str):
        self._run(["stop", self._q(name)])

    def freeze(self, name: str):
        self._run(["pause", self._q(name)])

    def unfreeze(self, name: str):
        self._run(["start", self._q(name)])  # unpauses a frozen container

    def delete(self, name: str, force: bool = False):
        args = ["delete", self._q(name)]
        if force:
            args.append("--force")
        self._run(args)

    def exec(self, name: str, command: list[str], *, input: str | None = None, **kwargs) -> str:
        args = ["exec", self._q(name), "--"]
        args.extend(command)
        cmd = ["incus"] + args
        # When *input* is provided we pipe it through stdin so secrets stay
        # out of the container's argv (process list).  Otherwise we close
        # stdin so subprocesses don't inherit our terminal.
        run_kwargs: dict = {"capture_output": True, "text": True}
        if input is None:
            run_kwargs["stdin"] = subprocess.DEVNULL
        else:
            run_kwargs["input"] = input
        result = subprocess.run(cmd, **run_kwargs)
        if result.returncode != 0:
            raise IncusError(result.returncode, cmd, result.stdout, result.stderr)
        return result.stdout.strip()

    def exec_streaming(
        self,
        name: str,
        command: list[str],
        *,
        on_line: Callable[[str], None] | None = None,
    ) -> str:
        """Execute a command with true line-by-line streaming.

        When *on_line* is provided, stdout is streamed line by line and
        each line is passed to the callback as it arrives.  When *on_line*
        is ``None``, falls back to the normal captured :meth:`exec`.
        """
        if on_line is None:
            return self.exec(name, command)
        args = ["exec", self._q(name), "--"] + command
        cmd = ["incus"] + args
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        lines: list[str] = []
        stderr_output = ""
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                on_line(line)
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                stderr_output = proc.stderr.read()
                proc.stderr.close()
            proc.wait()
        output = "\n".join(lines)
        if proc.returncode != 0:
            raise IncusError(proc.returncode, cmd, output, stderr_output)
        return output

    def add_device(self, name: str, device_name: str, device_type: str, **props):
        args = ["config", "device", "add", self._q(name), device_name, device_type]
        for k, v in props.items():
            args.append(f"{k}={v}")
        self._run(args)

    def add_disk(self, name: str, device_name: str, source: str, path: str, readonly: bool = False):
        props = {"source": source, "path": path}
        if readonly:
            props["readonly"] = "true"
        if not self._is_local_daemon():
            # A VM remote (Colima) maps the operator onto its user itself: the plain mount that
            # always worked there. No shifting: it would present the host's ids and undo that map.
            self.add_device(name, device_name, "disk", **props)
            return
        if self._mapping_in_force(name):
            # The operator is mapped onto the container user (see launch), so a plain mount already
            # shows their files as the container user's. A shifted mount would present host ids
            # unchanged and undo that.
            self.add_device(name, device_name, "disk", **props)
            return
        # No mapping (the operator IS the container user, or the host cannot map): `shift=true`
        # asks Incus to idmap the mount so host ids are at least visible as themselves rather than
        # as nobody. One fallback to the plain mount, only when Incus says shifting is unsupported
        # here; anything else (a missing source, a permission fault) is the caller's error.
        try:
            self.add_device(name, device_name, "disk", **props, shift="true")
        except IncusError as e:
            detail = f"{e.output or ''}\n{e.stderr or ''}"
            if not self._SHIFT_UNSUPPORTED_RE.search(detail):
                raise
            print(
                f"bubble: warning: Incus cannot shift the mount {device_name} "
                f"({source} -> {path}) on this host; mounting it plainly. Files owned by you "
                "will belong to nobody inside the "
                "container, so a credential or a writable store on this mount may be unusable.",
                flush=True,
            )
            self.add_device(name, device_name, "disk", **props)

    def publish(self, name: str, alias: str, *, properties: dict[str, str] | None = None):
        # Stop first if running
        try:
            info = self._get_info(name)
            if info.state == "running":
                self.stop(name)
        except RuntimeError:
            pass
        # Delete existing image with same alias
        if self.image_exists(alias):
            self.image_delete(alias)
        # Publish to the *same* remote as the source instance. Without an
        # explicit target remote, incus publishes the alias to the current
        # default remote — which on Colima is a different remote name than
        # ours (even though it's the same socket), and incus then rejects
        # the cross-remote publish ("source and target servers must be
        # different"). It also leaves the image where image_exists/delete
        # (which qualify with our remote) can't see it.
        args = ["publish", self._q(name)]
        if self._remote:
            args.append(self._q(""))  # e.g. "bubble-colima:" — target remote
        args += ["--alias", alias]
        self._run(args)
        if properties:
            assignments = [f"{key}={value}" for key, value in properties.items()]
            self._run(["image", "set-property", self._q(alias), *assignments])

    def image_exists(self, alias: str) -> bool:
        try:
            self._run(["image", "show", self._q(alias)])
            return True
        except subprocess.CalledProcessError:
            return False

    def image_delete(self, alias_or_fingerprint: str):
        self._run(["image", "delete", self._q(alias_or_fingerprint)])

    def image_delete_all(self):
        images = self.list_images()
        for img in images:
            fingerprint = img.get("fingerprint", "")
            if fingerprint:
                self._run(["image", "delete", self._q(fingerprint)])

    def list_images(self) -> list[dict]:
        args = ["image", "list", self._q("")] if self._remote else ["image", "list"]
        data = self._run_json(args)
        if not isinstance(data, list):
            return []
        return data

    def push_file(self, name: str, local_path: str, remote_path: str):
        self._run(["file", "push", local_path, f"{self._q(name)}{remote_path}"])

    # --- Operation introspection (used by `bubble doctor`) -------------

    def list_operations(self) -> list[dict]:
        """List currently running incus operations on our remote."""
        args = ["operation", "list", self._q("")] if self._remote else ["operation", "list"]
        data = self._run_json(args)
        if not isinstance(data, list):
            return []
        return data

    def delete_operation(self, op_id: str):
        """Cancel a running operation by id."""
        self._run(["operation", "delete", self._q(op_id)])

    # --- Network introspection (used by image build IPv4/DNS fixups) --

    def network_get(self, network: str, key: str) -> str:
        """Get a single config value from an incus-managed network."""
        return self._run(["network", "get", self._q(network), key])
