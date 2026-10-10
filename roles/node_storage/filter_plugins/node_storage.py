"""Pure decision logic for roles/node_storage.

Everything in here is a function of its arguments: no command runs, nothing
is read off a host. tasks/probe.yml collects what a node reports about
`docker_data_device` (`realpath`, `blkid -p -o export`, `lsblk -J`,
`findmnt -J --mountpoint`, the type of the data mount and bind target paths
and whether they hold anything) into one fact, tasks/classify.yml hands that fact to
node_storage_classify, and tests/node_storage_classify.yml feeds it canned
copies of the same output to prove the rules without a disk.

The classification decides whether this role may run `mkfs.xfs` on a device,
so it is the rule the role's safety rests on. It lives here, spelled once with
its reasons, rather than spread over a chain of Jinja `when:` lines.
"""

import json
import os
import re

# findmnt prints a bind mount's SOURCE as the device with the bound
# directory appended in brackets: `/dev/xvdb[/docker]`.
_SOURCE_RE = re.compile(r"^(?P<dev>.*?)\[(?P<sub>.*)\]$")

# How the kernel reports an ENFORCED project quota in an XFS mount's options
# (fs/xfs/xfs_super.c, xfs_fs_show_options): `prjquota`. `pquota` is the
# mount-time alias some tools echo back, accepted for robustness.
# `pqnoenforce` (accounting only) is deliberately NOT accepted: the limits this
# filesystem exists for would never be enforced.
PRJQUOTA_OPTIONS = ("prjquota", "pquota")

# blkid -o export keys that describe the device node, not a signature on it.
_NON_SIGNATURE_KEYS = ("DEVNAME",)

NOTHING_CHANGED = "Nothing was changed."

# lsblk TYPE values a docker_data_device must NOT be, in words.
_TYPE_WORDS = {
    "part": "a partition",
    "lvm": "an LVM logical volume",
    "crypt": "an encrypted (dm-crypt) volume",
    "dm": "a device-mapper device",
    "loop": "a loop device",
    "rom": "an optical drive",
    "md": "a software RAID array",
    "mpath": "a multipath device",
}


def _kname(path):
    """A device path reduced to its kernel name: /dev/xvdb -> xvdb."""
    return os.path.basename((path or "").strip().rstrip("/"))


def _split_source(source):
    """findmnt SOURCE -> (device, subdir); a plain mount's subdir is '/'."""
    source = (source or "").strip()
    m = _SOURCE_RE.match(source)
    if m:
        return m.group("dev"), (m.group("sub") or "/")
    return source, "/"


def _beneath(path, base):
    """True when `path` is `base` or anything below it (on a path boundary)."""
    base = base.rstrip("/") or "/"
    return path == base or path.startswith(base + "/")


def _mountpoints(node):
    """lsblk's mountpoints for one device node, nulls dropped.

    util-linux >= 2.37 prints MOUNTPOINTS as a list with null for "not
    mounted"; older releases print a single `mountpoint` string. Both read.
    """
    points = node.get("mountpoints")
    if points is None and node.get("mountpoint") is not None:
        points = [node.get("mountpoint")]
    return [p for p in (points or []) if p]


def _walk(node):
    """The node and every device stacked below it (partitions, dm, ...)."""
    yield node
    for child in node.get("children") or []:
        for n in _walk(child):
            yield n


def _has_prjquota(options):
    return any(o.strip() in PRJQUOTA_OPTIONS for o in (options or "").split(","))


def _ran(result):
    """True when a registered command result actually ran (not skipped)."""
    return isinstance(result, dict) and "rc" in result and not result.get("skipped")


def _doc(stdout):
    """A command's JSON stdout -> a dict ({} for no output).

    A registered stdout is always a string; an already-parsed document is
    accepted too, so the test fixtures can carry lsblk/findmnt output as
    readable JSON instead of a string-escaped copy of it.
    """
    if isinstance(stdout, dict):
        return stdout
    text = (stdout or "").strip()
    return json.loads(text) if text else {}


def parse_blkid(stdout):
    """`blkid -p -o export <dev>` stdout -> {KEY: VALUE}.

    One KEY=VALUE per line. In this format blkid escapes unsafe characters in
    a value with a backslash; the backslashes are dropped.
    """
    out = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = re.sub(r"\\(.)", r"\1", value)
    return out


def _blkid(result):
    """{} when blkid found nothing (rc 2, silent) or did not run; the parsed
    export on rc 0; {BLKID_RC: rc} on anything else (e.g. 8, ambivalent
    probe), which the classifier refuses as an unrecognized signature.

    rc 2 is ALSO what blkid returns when it could not open or read the device
    at all, with a warning on stderr; that case is not "no signature" and is
    reported separately by _blkid_error.
    """
    if not _ran(result) or result["rc"] == 2:
        return {}
    if result["rc"] == 0:
        return parse_blkid(result.get("stdout"))
    return {"BLKID_RC": str(result["rc"])}


def _blkid_error(result):
    """blkid's stderr when it exited 2 and complained (could not read the
    device), else ''. rc 2 with an empty stderr is a genuinely blank device."""
    if _ran(result) and result["rc"] == 2:
        return (result.get("stderr") or "").strip()
    return ""


def _lsblk(result):
    """blockdevices[0] of `lsblk -J` (children included), or None."""
    if not _ran(result) or result["rc"] != 0:
        return None
    devices = _doc(result.get("stdout")).get("blockdevices") or []
    return devices[0] if devices else None


def _findmnt(result):
    """One `findmnt -J --mountpoint <p>` result -> the visible filesystem, or None.

    Nothing mounted there: findmnt exits 1 and prints nothing. When several
    filesystems are stacked on one mountpoint findmnt lists them in mount
    order, and the LAST one is what a process sees, so that one is returned.
    """
    if not _ran(result) or result["rc"] != 0:
        return None
    filesystems = _doc(result.get("stdout")).get("filesystems") or []
    return filesystems[-1] if filesystems else None


def _path_type(result):
    """One ansible.builtin.stat result (follow: false) -> the path's type:
    absent | directory | symlink | file | other, or unknown when the stat did
    not run or failed. A symlink is reported as a symlink, never as what it
    points at."""
    if not isinstance(result, dict) or result.get("skipped") or result.get("failed"):
        return "unknown"
    st = result.get("stat")
    if not isinstance(st, dict) or "exists" not in st:
        return "unknown"
    if not st.get("exists"):
        return "absent"
    if st.get("islnk"):
        return "symlink"
    if st.get("isdir"):
        return "directory"
    if st.get("isreg"):
        return "file"
    return "other"


def _item_path(item):
    """A loop item -> the path it is about: the item itself, or a bind entry's
    `target`."""
    if isinstance(item, dict):
        return item.get("target")
    return item


def node_storage_build_probe(device_input, realpath, stat, blkid, lsblk, findmnt, find,
                             path_stat=None):
    """tasks/probe.yml's registered results -> the node_storage_probe fact.

    device_input  docker_data_device as configured
    realpath      `realpath -e -- <device_input>`        {rc, stdout}
    stat          ansible.builtin.stat of the resolved path (or skipped)
    blkid         `blkid -p -o export <device>`         {rc, stdout, stderr} (or skipped)
    lsblk         `lsblk -J -o ... <device>`            {rc, stdout} (or skipped)
    findmnt       loop results, item = mountpoint path   [{item, rc, stdout}]
    find          loop results, item = a path            [{item, rc, stdout, stderr}]
                  (`find <path> -mindepth 1 -maxdepth 1 -print -quit`)
    path_stat     loop results, item = a path            [{item, stat}]
                  (ansible.builtin.stat, follow: false)

    For each path (the data mount and every bind target):

    path_type     absent | directory | symlink | file | other | unknown
    dir_nonempty  true only when the path is a directory with an entry AND
                  nothing is mounted there: a mounted path shows the mounted
                  filesystem's contents, which say nothing about the root disk
                  underneath.
    find_error    set (to find's stderr, or a description) when the path is a
                  directory but find did not list it cleanly. An unreadable
                  directory is never taken for an empty one. find is not
                  consulted for a path that is absent (nothing to hide) or not
                  a directory (refused on its type: find does not follow a
                  symlinked start point, so a symlink always reads as empty).
    """
    device = (realpath.get("stdout") or "").strip() if _ran(realpath) and realpath["rc"] == 0 else ""
    st = (stat or {}).get("stat") or {}
    is_block = bool(device) and bool(st.get("exists")) and bool(st.get("isblk"))

    mounts = {}
    for r in findmnt or []:
        mounts[r.get("item")] = _findmnt(r)

    types = {}
    for r in path_stat or []:
        path = _item_path(r.get("item"))
        if path:
            types[path] = _path_type(r)

    nonempty = {}
    errors = {}
    for r in find or []:
        path = _item_path(r.get("item"))
        if not path:
            continue
        if types.get(path) != "directory":
            nonempty[path] = False
            continue
        if not _ran(r) or r["rc"] != 0:
            errors[path] = ((r.get("stderr") or "").strip()
                            or "find exited %s" % (r.get("rc") if _ran(r) else "without running"))
            nonempty[path] = False
            continue
        found = bool((r.get("stdout") or "").strip())
        nonempty[path] = found and not mounts.get(path)

    return {
        "device_input": device_input or "",
        "device": device,
        "is_block": is_block,
        "blkid": _blkid(blkid) if is_block else {},
        "blkid_error": _blkid_error(blkid) if is_block else "",
        "lsblk": _lsblk(lsblk) if is_block else None,
        "mounts": mounts,
        "path_type": types,
        "dir_nonempty": nonempty,
        "find_error": errors,
    }


def _refuse(text):
    return {"state": "refuse", "reason": "%s %s" % (text, NOTHING_CHANGED)}


def node_storage_classify(probe, mount, label, binds):
    """Classify `docker_data_device` from what tasks/probe.yml collected.

    Returns {state, reason}, state one of:

    * blank  -- a whole disk with no signature of any kind (and blkid read it
                cleanly), mounted nowhere, with nothing mounted at the data
                mount or the bind targets, and each of those three paths
                absent or an EMPTY plain directory (not a symlink, not a file,
                not unreadable). The ONLY state apply.yml formats.
    * ours   -- our XFS (label `label`), no partition table, no children,
                mounted nowhere outside the allowed set, but at least one of
                the three mounts is still missing; each unmounted path absent
                or an empty plain directory. apply.yml mounts.
    * ready  -- ours, mounted at `mount` from THIS device with project quotas
                on, and every bind target mounted from `<mount>/<subdir>` on
                THIS device. apply.yml only makes sure fstab has the three
                entries; on a node it built, that changes nothing.
    * refuse -- anything else, with an operator-facing reason that names the
                device, says what to do, and says nothing was changed.

    The allowed mountpoints for the device are `mount`, every bind target, and
    anything BENEATH any of them (a volume a container bind-mounts out of
    /var/lib/docker shows up as another mountpoint of the same device).

    Every comparison uses the RESOLVED device (probe.device, the realpath of
    the configured value), so a /dev/disk/by-id/... path classifies exactly
    like the kernel name it points at.
    """
    probe = probe or {}
    binds = binds or []
    given = probe.get("device_input") or ""
    device = probe.get("device") or ""
    mounts = probe.get("mounts") or {}
    nonempty = probe.get("dir_nonempty") or {}
    targets = [b["target"] for b in binds]
    allowed = [mount] + targets

    if device and given and device != given:
        dev = "docker_data_device %s (%s)" % (given, device)
    else:
        dev = "docker_data_device %s" % (given or device or "(unset)")

    # --- the device itself -------------------------------------------------
    if not device:
        return _refuse(
            "%s does not exist on this host. Attach the data disk, or set "
            "docker_data_device to the path the disk actually has "
            "(`lsblk` lists them)." % dev)
    if not probe.get("is_block"):
        return _refuse(
            "%s is not a block device. Point docker_data_device at the whole "
            "data disk (e.g. /dev/vdb or /dev/disk/by-id/...)." % dev)

    lsblk = probe.get("lsblk")
    if not lsblk:
        return _refuse(
            "%s could not be read by lsblk. Check the device on the node "
            "with `lsblk %s`." % (dev, device))

    names = set([_kname(device), _kname(lsblk.get("path")), _kname(lsblk.get("name"))])
    names.discard("")

    for node in _walk(lsblk):
        if "/" in _mountpoints(node):
            where = "is" if node is lsblk else "holds (%s)" % (node.get("path") or node.get("name"))
            return _refuse(
                "%s %s the root filesystem. docker_data_device must be a "
                "SEPARATE, empty disk; point it at the data disk instead." % (dev, where))

    dtype = lsblk.get("type")
    if dtype != "disk":
        return _refuse(
            "%s is %s (lsblk TYPE %s), not a whole disk. Point "
            "docker_data_device at a whole, unpartitioned data disk."
            % (dev, _TYPE_WORDS.get(dtype, "not a disk"), dtype or "unknown"))

    children = lsblk.get("children") or []
    if children:
        held = ", ".join(sorted((c.get("path") or c.get("name") or "?") for c in children))
        return _refuse(
            "%s has partitions or other devices on it (%s). This disk is laid "
            "out for something else; the role only uses a whole disk with no "
            "partition table. If its contents are disposable, wipe it "
            "(`wipefs -a` on each of those, then on %s) and re-run." % (dev, held, device))

    # blkid -p exits 2 both for "no signature" and for "could not open or read
    # the device" -- only the second one prints anything. A device blkid could
    # not read is never taken for a blank one.
    if probe.get("blkid_error"):
        return _refuse(
            "%s could not be read by blkid (%s), so whether it already holds "
            "data cannot be told. Check the device on the node with "
            "`blkid -p %s`." % (dev, probe["blkid_error"], device))

    blkid = dict(probe.get("blkid") or {})
    if blkid.get("PTTYPE"):
        return _refuse(
            "%s carries a %s partition table. The role only uses a whole disk "
            "with no partition table; if its contents are disposable, wipe it "
            "(`wipefs -a %s`) and re-run." % (dev, blkid["PTTYPE"], device))

    signature = dict((k, v) for k, v in blkid.items() if k not in _NON_SIGNATURE_KEYS)
    fstype = signature.get("TYPE")
    if signature:
        if fstype != "xfs":
            what = ("an existing %s signature" % fstype) if fstype else (
                "an unrecognized signature (blkid: %s)"
                % ", ".join("%s=%s" % kv for kv in sorted(signature.items())))
            return _refuse(
                "%s already holds %s. The role never formats over existing "
                "data; if the contents are disposable, wipe it (`wipefs -a %s`) "
                "and re-run." % (dev, what, device))
        if signature.get("LABEL") != label:
            what = ("label '%s'" % signature["LABEL"]) if signature.get("LABEL") else "no label"
            return _refuse(
                "%s holds an XFS filesystem with %s, not the '%s' this role "
                "creates, so it is not ours. If the contents are disposable, "
                "wipe it (`wipefs -a %s`) and re-run; if it is ours, label it "
                "(`xfs_admin -L %s %s`, unmounted)." % (dev, what, label, device, label, device))
        ours = True
    else:
        if lsblk.get("fstype"):
            return _refuse(
                "%s shows no signature to blkid but lsblk reports %s. Inspect "
                "the device by hand." % (dev, lsblk["fstype"]))
        ours = False

    # --- where the device is mounted ---------------------------------------
    points = _mountpoints(lsblk)
    if points and not ours:
        return _refuse(
            "%s has no filesystem signature yet is mounted at %s. Inspect the "
            "device by hand." % (dev, ", ".join(points)))
    outside = [p for p in points if not any(_beneath(p, a) for a in allowed)]
    if outside:
        return _refuse(
            "%s is mounted at %s, outside %s and the paths beneath them. "
            "Unmount it there (and remove the fstab entry) before re-running."
            % (dev, ", ".join(outside), ", ".join(allowed)))

    # --- what is mounted at the three paths (R2) ---------------------------
    # Whatever sits at the data mount or a bind target must come from THIS
    # device. A different source means docker_data_device changed on a built
    # node, or another disk already holds the path; either way formatting or
    # mounting over it would hide data.
    mounted = {}
    for path in allowed:
        m = mounts.get(path)
        if not m:
            continue
        src, sub = _split_source(m.get("source"))
        if _kname(src) not in names:
            return _refuse(
                "%s is mounted from %s, not from %s. If docker_data_device "
                "changed on a built node, put it back; otherwise unmount %s "
                "(and remove its fstab entry) first." % (path, src or "an unknown source", dev, path))
        if path == mount:
            if sub != "/":
                return _refuse(
                    "%s is mounted from %s[%s], a subdirectory of %s rather "
                    "than its root. Unmount it and re-run." % (path, src, sub, dev))
            if not _has_prjquota(m.get("options")):
                return _refuse(
                    "%s is mounted at %s WITHOUT project quotas (options: %s). "
                    "XFS can only turn prjquota on at mount time, never on a "
                    "remount: schedule a window, stop docker and containerd, "
                    "unmount %s (and the bind mounts beneath it) and re-run, "
                    "which mounts it with %s." % (dev, path, m.get("options") or "none", path, "prjquota"))
        else:
            want = "/" + next(b["src_subdir"] for b in binds if b["target"] == path)
            if sub != want:
                return _refuse(
                    "%s is mounted from %s[%s]; it must be the bind mount of "
                    "%s%s on %s. Unmount %s and re-run." % (path, src, sub, mount, want, dev, path))
        mounted[path] = True

    # --- what the three paths are on the root disk ----------------------------
    # Each path that is NOT a mountpoint must be absent or a plain directory.
    # A symlink (say /var/lib/docker -> /srv/docker) would keep docker's state
    # at its target while reading as empty here -- find does not follow a
    # symlinked start point -- and the mount would then go over the link. A
    # path that could not be checked is never taken for an empty one.
    types = probe.get("path_type") or {}
    errors = probe.get("find_error") or {}
    for path in allowed:
        if mounted.get(path):
            continue
        ptype = types.get(path) or "unknown"
        if ptype == "unknown":
            return _refuse(
                "%s cannot be put in place: %s could not be checked on the "
                "node (stat failed or did not run). Check it by hand with "
                "`ls -ld %s`." % (dev, path, path))
        if ptype not in ("absent", "directory"):
            what = {"symlink": "a symlink", "file": "a regular file"}.get(ptype, "not a directory")
            return _refuse(
                "%s cannot be put in place: %s is %s, not a directory. "
                "Mounting over it would hide whatever it holds or points at "
                "(`ls -ld %s`). Move that state onto the data disk by hand or "
                "rebuild the node with the data disk attached." % (dev, path, what, path))
    for path in allowed:
        if errors.get(path):
            return _refuse(
                "%s cannot be put in place: the contents of %s could not be "
                "listed (%s), so whether it already holds data cannot be told. "
                "Check it by hand with `ls -A %s`." % (dev, path, errors[path], path))
        if not mounted.get(path) and types.get(path) == "directory" and path not in nonempty:
            return _refuse(
                "%s cannot be put in place: the contents of %s were not "
                "checked. Check it by hand with `ls -A %s`." % (dev, path, path))

    # --- docker or containerd already written to the root disk --------------
    # A bind target that is not mounted but already has contents means docker
    # or containerd ran before the data disk was in place. Binding over it
    # would hide that state, and formatting would orphan it.
    written = [t for t in targets if not mounted.get(t) and nonempty.get(t)]
    if written:
        return _refuse(
            "%s cannot be put in place: %s already exist%s and %s not empty, "
            "so docker or containerd has already written to the root disk. "
            "This is a node that was built without a data disk; rebuild it "
            "with the data disk attached." % (
                dev, " and ".join(written), "" if len(written) > 1 else "s",
                "are" if len(written) > 1 else "is"))

    # The same for the data mount itself: files sitting in an unmounted
    # /var/lib/docker-data on the root disk would vanish under the mount.
    if not mounted.get(mount) and nonempty.get(mount):
        return _refuse(
            "%s cannot be put in place: %s is not mounted but already has "
            "contents on the root disk, which mounting the data disk would "
            "hide. Find out what wrote there (`ls -A %s`), move it away or "
            "remove it, and re-run." % (dev, mount, mount))

    if not ours:
        return {"state": "blank", "reason": "%s is blank; it will be formatted XFS "
                "(label %s) and mounted at %s." % (dev, label, mount)}

    missing = [p for p in allowed if not mounted.get(p)]
    if missing:
        return {"state": "ours", "reason": "%s holds our XFS filesystem; %s will be "
                "mounted." % (dev, ", ".join(missing))}
    return {"state": "ready", "reason": ""}


class FilterModule(object):
    def filters(self):
        return {
            "node_storage_classify": node_storage_classify,
            "node_storage_build_probe": node_storage_build_probe,
        }
