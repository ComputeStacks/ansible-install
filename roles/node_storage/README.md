# node_storage

**Owner: container storage on the nodes.** Run on `groups['nodes']` by the
`Node container storage` play in `playbooks/site.yml` and
`playbooks/add-region.yml`, immediately BEFORE the play that installs docker.
Its read-only half (`tasks_from: probe` + `classify`) also runs in preflight,
in `playbooks/upgrade-docker.yml`'s preflight and in validate's `storage`
check.

**Nodes only.** The controller, metrics, registry, backup and nameserver hosts
keep a single disk and docker's default locations.

**Required var: `docker_data_device`** on every node (host var or a `nodes`
group var): the whole data disk, an absolute path under `/dev/`. Prefer a
`/dev/disk/by-id/...` link when the platform offers one -- kernel names such
as `/dev/vdb` or `/dev/xvdb` can change between boots when disks are added.
The role never auto-detects a disk.

## Purpose

Docker 29 defaults to the **containerd image store**: image content and every
container's writable layer live under `/var/lib/containerd`, not
`/var/lib/docker` (`docker info` reports `driver-type:
io.containerd.snapshotter.v1` with no daemon.json at all). Measured on a live
29.x node: `/var/lib/containerd` 46G (35G snapshots + 11G content),
`/var/lib/docker` 14G (mostly volumes).

The pre-29 habit of mounting a data disk at `/var/lib/docker` alone therefore
leaves most of the growth on the root disk. That has happened in production:
a node with a ~1T data disk mounted only at `/var/lib/docker` had its ~125G
of image layers on a ~250G root disk while the data disk held only volumes.
This role moves **both** paths onto one dedicated disk, and makes it
impossible for either daemon to start without it.

The layout used to come from a hypervisor cloud-init script
(`setup-container-storage`); the provisioner owns it now.

## Layout

| What | Value |
| --- | --- |
| Device | `docker_data_device`, a whole disk, **no partition table** |
| Filesystem | XFS, `mkfs.xfs -L dockerdata -n ftype=1` (`node_storage_label`) |
| Data mount | `/var/lib/docker-data` (`node_storage_mount`), fstab `UUID=<uuid> ... xfs defaults,noatime,prjquota 0 0` |
| Bind mounts | `/var/lib/docker-data/docker` -> `/var/lib/docker` (source dir 0710), `/var/lib/docker-data/containerd` -> `/var/lib/containerd` (0711), fstab `none bind 0 0` |
| systemd | `/etc/systemd/system/{docker,containerd}.service.d/10-cs-data-disk.conf`: `[Unit] RequiresMountsFor=/var/lib/docker /var/lib/containerd` |

* **No partition table.** The disk has one purpose; a partition would only add
  a growpart step to every resize.
* **`ftype=1`** is required by overlayfs. It is mkfs.xfs's default, pinned
  because an overlay on `ftype=0` only fails at runtime.
* **By UUID, not LABEL.** A second disk carrying a stray `dockerdata` label
  (a disk moved from another VM) must never be able to win the mount.
* **No `nofail`.** A missing data disk must stop the boot loudly, not let
  docker start writing into the root disk's empty `/var/lib/docker`.
  **Pass 0**: `fsck.xfs` is a no-op by design; XFS replays its log at mount.
* **Bind mounts, not `data-root`** (plus containerd's `root` in its
  config.toml). Every consumer -- docker, containerd, cadvisor's
  `/var/lib/docker` mount, cs-agent, node_upgrade -- keeps the canonical paths,
  so nothing else has to know the disk exists, and one pool means growth is
  one `xfs_growfs` with no guessed split between the two daemons. systemd's
  fstab generator orders each bind mount after its source.
* **`RequiresMountsFor=`** on both units: neither daemon can start before the
  bind mounts are up, so neither can ever write its state underneath them.
* **`prjquota`**, enforced, from the first mount: XFS can only turn project
  quotas on at mount time -- never on a remount -- and they are the mechanism
  for the planned per-volume size limits (directories under the data disk,
  bound into containers). XFS treats group and project quotas as mutually
  exclusive; this is project quotas only. Nothing creates quota projects or a
  volumes directory yet; that is future agent/controller work.

## Flow (`tasks/main.yml`)

| Step | File | What happens |
| --- | --- | --- |
| probe | `probe.yml` | Read-only, `changed_when: false`, runs in `--check`. `realpath -e` of the device (so a by-id link compares as its kernel name), `stat`, `blkid -p -o export`, `lsblk -J -o NAME,PATH,TYPE,FSTYPE,MOUNTPOINTS`, `findmnt -J --mountpoint` for the data mount and both bind targets, and whether each bind target already has contents while nothing is mounted there. Sets `node_storage_probe`. Never fails on a wrong device. |
| classify | `classify.yml` | Pure (no command): the `node_storage_classify` filter -> `node_storage_state` and `node_storage_reason`. Fails on `refuse` unless `node_storage_classify_fail: false`. |
| apply | `apply.yml` | xfsprogs; `mkfs.xfs` (blank only, no `-f`); UUID re-read with `blkid -p`; data mount; bind source dirs (only after the data mount is up); bind mounts; drop-ins and an immediate `daemon-reload` when they changed; then probe + classify again and require `ready`. |

Mounts already in place are written to fstab (`state: present`) but never
passed to `ansible.posix.mount` as `mounted`: that state remounts a mounted
filesystem whenever its fstab line changes. **The role never unmounts and
never remounts.** On a `ready` node no mount task runs at all; only the
drop-ins are (re)written, which on a node this role built changes nothing.

## States

| State | Meaning | apply.yml |
| --- | --- | --- |
| `blank` | Whole disk, no signature of any kind, no partition table, no children, mounted nowhere; nothing mounted at the data mount or either bind target; `/var/lib/docker` and `/var/lib/containerd` absent or empty. | formats, then mounts everything |
| `ours` | XFS labeled `dockerdata`, no partition table, no children, mounted nowhere outside the allowed set, but at least one of the three mounts is missing. | mounts what is missing |
| `ready` | `ours`, mounted at the data mount from THIS device with `prjquota`, and both bind targets mounted from `<mount>/docker` / `<mount>/containerd` on THIS device. | nothing (asserts) |
| `refuse` | Anything else, with a reason. | never reached |

The **allowed mountpoints** of the device are the data mount, the two bind
targets, and anything beneath any of them (a local-driver volume bound out of
`/var/lib/docker/volumes/...` appears as one more mountpoint of the device).

## Refusals

Every refusal names the device, says what to do, and ends "Nothing was
changed." The rule lives in `filter_plugins/node_storage.py`;
`tests/node_storage_classify.yml` proves each one (`make check`).

* the device does not exist (`realpath -e` fails) or is not a block device;
* it **is or holds the root filesystem** (checked through every partition and
  device stacked on it);
* lsblk `TYPE` is not `disk` -- a partition, LVM volume, dm/crypt device,
  loop device, optical drive (a config drive presented as a CD), RAID array;
* it has children (partitions, LVM/dm devices on it);
* it carries a partition table (`PTTYPE`, even an empty one);
* it carries **any signature** other than XFS labeled `dockerdata` -- ext4, a
  config drive's iso9660/vfat, LVM2_member, swap, an unlabeled XFS or one with
  another label -- or blkid could not decide (`BLKID_RC=8`, ambivalent);
* it is mounted anywhere outside the allowed set (`/mnt`, and a prefix
  look-alike such as `/var/lib/docker-data2`, are both outside);
* the data mount or either bind target is **mounted from anything other than
  this device** -- a changed `docker_data_device` on a built node, or another
  disk already holding the path (compared by kernel name, with findmnt's
  `[/subdir]` suffix stripped);
* a bind target is mounted from this device but from the wrong subdirectory
  (findmnt `SOURCE` must be `<dev>[/docker]` / `<dev>[/containerd]`), or the
  data mount is a subdirectory rather than the filesystem root;
* it is ours and mounted at the data mount **without enforced project
  quotas**. Accepted option tokens: `prjquota` (what the kernel prints for
  enforced project quotas) and `pquota`. `pqnoenforce` (accounting only) and
  `noquota` are refused. The fix needs a window: stop docker and containerd,
  unmount the bind mounts and the data mount, re-run;
* `/var/lib/docker` or `/var/lib/containerd` is **not mounted but already has
  contents** -- docker or containerd ran on the root disk before the data
  disk was in place. Mounting over it would hide that state. Such a node is
  rebuilt with the data disk attached.

Refusals are fatal for the whole run before any host changes: preflight runs
probe + classify on every node and fails once, listing each refused node.

## Growing the disk

Enlarge the virtual disk in the hypervisor, then on the node:

```sh
xfs_growfs /var/lib/docker-data
```

XFS grows online and can never shrink. There is no partition to grow first.

## Variables

`defaults/main.yml`: `node_storage_mount`, `node_storage_label`,
`node_storage_mount_opts`, `node_storage_binds` (target, source subdir and
the source directory's mode), `node_storage_units`,
`node_storage_dropin_file`, `node_storage_classify_fail`. The layout values
are a contract with validate and the docs; change them only with both.
