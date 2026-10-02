"""Native Incus: map the operator's uid/gid onto the container's `user` (ids read from the stopped
container's /etc/passwd, not assumed), only when /etc/subuid and /etc/subgid allow it; print a
hint and start unmapped otherwise; start unmapped when Incus refuses; never on a configured
remote or a non-local default remote; keep a profile's raw.idmap; and mount plainly (no shift)
once a mapping is in force, which is read per container, not remembered per runtime.

Live reference (Incus 7.4, kernel 7.1.5, dir pool on ext4, host uid 1005, container `user`
1001): under `raw.idmap uid 1005 1001`, a PLAIN mount of a 1005-owned 0600 file is `user`'s inside
the container and readable/writable; the same directory mounted `shift=true` shows 1005 and is
denied. Hence: map, then never shift."""

import json

import pytest

from bubble.runtime import incus as I

PASSWD = (
    "root:x:0:0:root:/root:/bin/bash\n"
    "ubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n"
    "user:x:1001:1001::/home/user:/bin/bash\n"
)
LOCAL = {"local": {"Addrs": ["unix://"], "Protocol": "incus"}}
NONLOCAL = {"local": {"Addrs": ["https://10.0.0.5:8443"], "Protocol": "incus"}}


class FakeRuntime(I.IncusRuntime):
    """The real IncusRuntime with the subprocess boundary replaced: every incus argv is recorded
    and answered as scripted."""

    def __init__(
        self,
        remote="",
        *,
        refuse_idmap=False,
        remotes=None,
        default_remote="local",
        existing_idmap="",
        instance_idmap=None,
        disk_errors=(),
    ):
        super().__init__(remote=remote)
        self.calls = []
        self.refuse_idmap = refuse_idmap
        self.remotes = LOCAL if remotes is None else remotes
        self.default_remote = default_remote
        self.existing_idmap = existing_idmap  # what `config get -e` reports (profile + instance)
        self.instance_idmap = instance_idmap  # what `config get` reports before we set anything
        self.disk_errors = list(disk_errors)  # IncusErrors to raise from successive device adds

    def _run(self, args, check=True, capture=True):
        self.calls.append(list(args))
        if args[:2] == ["file", "pull"]:
            return PASSWD
        if args[:2] == ["remote", "get-default"]:
            return self.default_remote + "\n"
        if args[:2] == ["remote", "list"]:
            return json.dumps(self.remotes)
        if args[:3] == ["config", "get", "-e"]:
            return self.existing_idmap
        if args[:2] == ["config", "get"]:
            set_by_us = [c for c in self.calls if c[:2] == ["config", "set"] and c[2] == args[2]]
            if set_by_us and not self.refuse_idmap:
                return set_by_us[-1][3].split("=", 1)[1]
            return self.instance_idmap or ""
        if args[:2] == ["config", "set"] and self.refuse_idmap:
            raise I.IncusError(
                1, ["incus", *args], "", "Error: Invalid idmap: uid not allowed by subuid"
            )
        if args[:3] == ["config", "device", "add"] and self.disk_errors:
            raise self.disk_errors.pop(0)
        return ""

    def _get_info(self, name):
        return name


def _subid(monkeypatch, tmp_path, uid_ok, gid_ok, uid=1005, gid=1005):
    su = tmp_path / "subuid"
    sg = tmp_path / "subgid"
    su.write_text("root:1000000:1000000000\n" + (f"root:{uid}:1\n" if uid_ok else ""))
    sg.write_text("root:1000000:1000000000\n" + (f"root:{gid}:1\n" if gid_ok else ""))
    real_allows = I.IncusRuntime._subid_allows

    def allows(path, wanted):
        return real_allows(str(su) if path.endswith("subuid") else str(sg), wanted)

    monkeypatch.setattr(I.IncusRuntime, "_subid_allows", staticmethod(allows))
    monkeypatch.setattr(I.os, "getuid", lambda: uid)
    monkeypatch.setattr(I.os, "getgid", lambda: gid)


def _kinds(rt):
    out = []
    for c in rt.calls:
        if c[0] == "remote":
            continue  # locality lookups, not container operations
        out.append(c[0] if c[0] != "config" else "config-" + c[1] + ("-e" if c[2] == "-e" else ""))
    return out


def _idmap_set(rt):
    return [c[3][len("raw.idmap=") :] for c in rt.calls if c[:2] == ["config", "set"]]


def _disk_adds(rt):
    return [c for c in rt.calls if c[:3] == ["config", "device", "add"]]


# ---- mapping policy


def test_maps_onto_the_real_container_user_when_allowed(monkeypatch, tmp_path, capsys):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime()
    rt.launch("c", "img")
    assert _kinds(rt) == ["init", "file", "config-get-e", "config-set", "start"]
    assert _idmap_set(rt) == ["uid 1005 1001\ngid 1005 1001"]
    assert "bubble:" not in capsys.readouterr().out


def test_only_the_differing_id_is_mapped(monkeypatch, tmp_path):
    _subid(
        monkeypatch, tmp_path, True, False, uid=1005, gid=1001
    )  # gid matches; subgid would refuse
    rt = FakeRuntime()
    rt.launch("c", "img")
    assert _idmap_set(rt) == ["uid 1005 1001"]  # no gid line, and the subgid refusal is irrelevant
    _subid(monkeypatch, tmp_path, False, True, uid=1001, gid=1005)
    rt = FakeRuntime()
    rt.launch("c", "img")
    assert _idmap_set(rt) == ["gid 1005 1001"]


def test_no_mapping_when_operator_is_the_container_user(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True, uid=1001, gid=1001)
    rt = FakeRuntime()
    rt.launch("c", "img")
    assert _kinds(rt) == ["init", "file", "start"]
    rt.add_disk("c", "d", "/host/x", "/opt/x")
    assert _disk_adds(rt)[-1][-1] == "shift=true"  # unmapped: shifted so host ids stay visible


def test_hint_and_unmapped_when_not_allowed(monkeypatch, tmp_path, capsys):
    _subid(monkeypatch, tmp_path, False, True)
    rt = FakeRuntime()
    rt.launch("c", "img")
    assert _kinds(rt) == ["init", "file", "start"]
    out = capsys.readouterr().out
    assert "root:1005:1" in out and "/etc/subuid" in out
    rt.add_disk("c", "d", "/host/x", "/opt/x")
    assert _disk_adds(rt)[-1][-1] == "shift=true"


def test_starts_unmapped_when_incus_refuses(monkeypatch, tmp_path, capsys):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime(refuse_idmap=True)
    rt.launch("c", "img")
    assert _kinds(rt) == ["init", "file", "config-get-e", "config-set", "start"]
    assert "restart incus" in capsys.readouterr().out
    rt.add_disk("c", "d", "/host/x", "/opt/x")
    assert _disk_adds(rt)[-1][-1] == "shift=true"  # the mapping is NOT in force


def test_unrelated_config_error_propagates_and_nothing_is_deleted(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)

    class Failing(FakeRuntime):
        def _run(self, args, check=True, capture=True):
            if args[:2] == ["config", "set"]:
                self.calls.append(list(args))
                raise I.IncusError(1, ["incus", *args], "", "Error: Instance is busy")
            return super()._run(args, check, capture)

    rt = Failing()
    with pytest.raises(I.IncusError, match="busy"):
        rt.launch(
            "existing-idmap-container", "img"
        )  # a name containing "idmap" is not a diagnostic
    assert not any(c[0] == "delete" for c in rt.calls)


# ---- existing configuration


def test_profile_idmap_is_preserved_and_extended(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime(existing_idmap="both 4000 4000\nuid 1005 1001")  # a profile maps uid already
    rt.launch("c", "img")
    assert _idmap_set(rt) == ["both 4000 4000\nuid 1005 1001\ngid 1005 1001"]  # kept, gid appended


def test_fully_covering_profile_idmap_is_left_alone(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime(existing_idmap="uid 1005 1001\ngid 1005 1001")
    rt.launch("c", "img")
    assert _idmap_set(rt) == []  # nothing rewritten
    rt.add_disk("c", "d", "/host/x", "/opt/x")
    assert _disk_adds(rt)[-1][-1] == "path=/opt/x"  # and the mapping counts as in force: plain


def test_merge_idmap_keeps_unrelated_lines():
    m = I.IncusRuntime._merge_idmap
    assert m("", "uid 1 2\ngid 1 2") == "uid 1 2\ngid 1 2"
    assert m("both 9 9\n", "uid 1 2") == "both 9 9\nuid 1 2"
    assert m("uid 1 2", "uid 1 2\ngid 1 2") == "uid 1 2\ngid 1 2"  # already ours: kept once
    assert m("both 4000 4000\nuid 1 2", "uid 1 2") == "both 4000 4000\nuid 1 2"


def test_merge_idmap_replaces_a_line_mapping_our_host_id_elsewhere():
    """incus-user's restricted project maps its user onto the same id inside (`uid 1007 1007`).
    Kept, it would leave bubble's mounts owned by 1007 in a container whose `user` is 1001, and
    git would call every Lake mirror "dubious" while bubble counted the container as mapped."""
    m = I.IncusRuntime._merge_idmap
    assert (
        m("uid 1007 1007\ngid 1007 1007", "uid 1007 1001\ngid 1007 1001")
        == "uid 1007 1001\ngid 1007 1001"
    )
    assert m("uid 1 7\nboth 9 9", "uid 1 2") == "both 9 9\nuid 1 2"
    # A `both` line for our host id keeps the half we do not map.
    assert m("both 1007 1007", "uid 1007 1001") == "gid 1007 1007\nuid 1007 1001"
    assert m("both 1007 1001", "uid 1007 1001\ngid 1007 1001") == "both 1007 1001"


def test_incus_user_profile_idmap_is_overridden_on_the_instance(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True, uid=1007, gid=1007)
    rt = FakeRuntime(existing_idmap="uid 1007 1007\ngid 1007 1007")  # incus-user's default profile
    rt.launch("c", "img")
    assert _idmap_set(rt) == ["uid 1007 1001\ngid 1007 1001"]
    rt.add_disk("c", "d", "/host/x", "/opt/x")
    assert _disk_adds(rt)[-1][-1] == "path=/opt/x"  # mapped, so mounted plainly


# ---- server scope


def test_no_mapping_on_a_configured_remote(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime(remote="bubble-colima")
    rt.launch("c", "img")
    assert rt.calls == [["launch", "bubble-colima:img", "bubble-colima:c"]]
    rt.add_disk("c", "d", "/host/x", "/opt/x", readonly=True)
    assert rt.calls[-1] == [
        "config",
        "device",
        "add",
        "bubble-colima:c",
        "d",
        "disk",
        "source=/host/x",
        "path=/opt/x",
        "readonly=true",
    ]  # Colima's plain mount, exactly as before: no shift, no idmap


def test_no_mapping_when_the_default_remote_is_not_local(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime(remotes=NONLOCAL)
    rt.launch("c", "img")
    assert _kinds(rt) == ["launch"]
    rt.add_disk("c", "d", "/host/x", "/opt/x")
    assert _disk_adds(rt)[-1][-1] == "path=/opt/x"
    assert not any(a.startswith("raw.idmap=") for c in rt.calls for a in c)


def test_named_default_remote_that_is_local_maps(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime(remotes={"mine": LOCAL["local"]}, default_remote="mine")
    rt.launch("c", "img")
    assert _idmap_set(rt) == ["uid 1005 1001\ngid 1005 1001"]


# ---- disk arguments and the mapping actually in force


def test_mapped_container_mounts_plainly_ro_and_rw(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime()
    rt.launch("c", "img")
    rt.add_disk("c", "ro", "/host/x", "/opt/x", readonly=True)
    rt.add_disk("c", "rw", "/host/y", "/opt/y")
    assert _disk_adds(rt) == [
        [
            "config",
            "device",
            "add",
            "c",
            "ro",
            "disk",
            "source=/host/x",
            "path=/opt/x",
            "readonly=true",
        ],
        ["config", "device", "add", "c", "rw", "disk", "source=/host/y", "path=/opt/y"],
    ]


def test_mapping_is_read_per_container_not_remembered_per_runtime(monkeypatch, tmp_path):
    """A container this runtime did not launch (a refresh after restart) is asked, not assumed."""
    rt = FakeRuntime(instance_idmap="uid 1005 1001")
    rt.add_disk("old", "d", "/host/x", "/opt/x")
    assert ["config", "get", "old", "raw.idmap"] in rt.calls
    assert _disk_adds(rt)[-1][-1] == "path=/opt/x"
    rt2 = FakeRuntime(instance_idmap="")
    rt2.add_disk("other", "d", "/host/x", "/opt/x")
    assert _disk_adds(rt2)[-1][-1] == "shift=true"


def test_two_containers_keep_separate_mapping_state(monkeypatch, tmp_path):
    _subid(monkeypatch, tmp_path, True, True)
    rt = FakeRuntime()
    rt.launch("mapped", "img")
    rt.refuse_idmap = True
    rt.launch("unmapped", "img")
    rt.add_disk("mapped", "d", "/host/x", "/opt/x")
    rt.add_disk("unmapped", "d", "/host/x", "/opt/x")
    adds = _disk_adds(rt)
    assert adds[0][3] == "mapped" and adds[0][-1] == "path=/opt/x"
    assert adds[1][3] == "unmapped" and adds[1][-1] == "shift=true"


# ---- shifting unsupported, and unrelated disk errors


def _err(text, *, stdout=False):
    return I.IncusError(
        1,
        ["incus", "config", "device", "add", "c", "shared-shift-cache", "disk", "shift=true"],
        text if stdout else "",
        "" if stdout else text,
    )


@pytest.mark.parametrize("stdout", [False, True])
def test_unsupported_shift_falls_back_once_with_a_warning(monkeypatch, tmp_path, capsys, stdout):
    _subid(monkeypatch, tmp_path, True, True, uid=1001, gid=1001)  # unmapped: shifting is tried
    rt = FakeRuntime(
        disk_errors=[
            _err(
                "Error: Failed to start device: idmapped mounts not supported on this filesystem",
                stdout=stdout,
            )
        ]
    )
    rt.add_disk("c", "d", "/host/x", "/opt/x", readonly=True)
    adds = _disk_adds(rt)
    assert adds[-2][-1] == "shift=true" and adds[-1][-1] == "readonly=true"  # one retry, same args
    assert adds[-1][6:8] == ["source=/host/x", "path=/opt/x"]
    out = capsys.readouterr().out
    assert "warning" in out and "/host/x -> /opt/x" in out and "plainly" in out


def test_unrelated_disk_errors_propagate_even_when_names_mention_shift_or_idmap(
    monkeypatch, tmp_path
):
    _subid(monkeypatch, tmp_path, True, True, uid=1001, gid=1001)
    for text in (
        'Error: Failed to start device "shared-shift-cache": stat /srv/idmap-store: no such file',
        "Error: Failed to start device: permission denied",
    ):
        rt = FakeRuntime(disk_errors=[_err(text)])
        with pytest.raises(I.IncusError):
            rt.add_disk("c", "shared-shift-cache", "/srv/idmap-store", "/opt/x")
        assert len(_disk_adds(rt)) == 1  # no retry


# ---- subordinate-id parsing


def test_subid_parser(tmp_path):
    f = tmp_path / "subuid"
    f.write_text(
        "# comment\n\nroot:100000:65536\nalice:200000:65536\n0:1005:1\n"
        "malformed line\nroot:x:y\nroot:300000\n"
    )
    allows = I.IncusRuntime._subid_allows
    assert allows(str(f), 1005) is True  # numeric root name
    assert allows(str(f), 100000) is True and allows(str(f), 165535) is True  # range boundaries
    assert allows(str(f), 165536) is False and allows(str(f), 99999) is False
    assert allows(str(f), 250000) is False  # another user's range does not count for root
    assert allows(str(tmp_path / "missing"), 1) is None
    g = tmp_path / "subgid"
    g.write_text("root:1005:1\n")
    assert allows(str(g), 1005) is True  # uid and gid files are read independently
