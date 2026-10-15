# node_storage

Puts a node's docker and containerd state on a dedicated XFS data disk, in
the play immediately before docker is installed. Its read-only half
(`tasks_from: probe` + `classify`) is also the one data-disk check preflight,
validate and the docker upgrade use.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Node container storage" | `nodes` |
| `add-region.yml` | "New node container storage" | `nodes` (the new node) |
| `upgrade-docker.yml` (`tasks_from: probe`, `classify`) | "Preflight every targeted node" | `node_upgrade_targets` |
| `maintain-nodes.yml` (`tasks_from: probe`, `classify`) | "Preflight every targeted node" | `maintain_node_targets` |
| `roles/preflight` (`tasks_from: probe`, `classify`) | "Preflight validation" / "Preflight validation and attach-mode inventory checks" | `nodes`, not `existing_env` |
| `roles/validate` (`tasks_from: probe`, `classify`) | `storage` check, wherever validate runs | `nodes`, not `existing_env` |
| `unseal.yml` | not run | — |

**Attach mode:** new node only; `existing_env` nodes are never probed.
**Requires:** `docker_data_device` on every node; `ansible.posix` (mounts).

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
| probe | `probe.yml` | Read-only, `changed_when: false`, runs in `--check`. `realpath -e` of the device (so a by-id link compares as its kernel name), `stat`, `blkid -p -o export`, `lsblk -J -o NAME,PATH,TYPE,FSTYPE,MOUNTPOINTS`, `findmnt -J --mountpoint` for the data mount and both bind targets, `stat` of those three paths WITHOUT following links (absent / directory / symlink / file / other), and whether each already has contents while nothing is mounted there. Sets `node_storage_probe`. Never fails on a wrong device. |
| classify | `classify.yml` | Pure (no command): the `node_storage_classify` filter -> `node_storage_state` and `node_storage_reason`. Fails on `refuse` unless `node_storage_classify_fail: false`. |
| apply | `apply.yml` | xfsprogs; `mkfs.xfs` (blank only, no `-f`); UUID re-read with `blkid -p` (every state); data mount; bind source dirs (only after the data mount is up; not on `ready`); bind mounts; drop-ins and an immediate `daemon-reload` when they changed; then probe + classify again and require `ready`. |

**fstab is ensured on every state, `ready` included.** A node whose three
mounts are live but missing from fstab (or carried by hand-made lines) would
otherwise reboot with docker writing to the root disk. Mounts already in
place are written to fstab (`state: present`, which never mounts or
remounts) but never passed to `ansible.posix.mount` as `mounted`: that state
remounts a mounted filesystem whenever its fstab line changes. **The role
never unmounts and never remounts.** A hand-made line for one of the three
paths (by LABEL, by device name, other options) is rewritten to the role's
own; on a node this role built the lines already match field for field, so
on a `ready` node the fstab and drop-in tasks change nothing.

## States

| State | Meaning | apply.yml |
| --- | --- | --- |
| `blank` | Whole disk, no signature of any kind (and blkid read it cleanly), no partition table, no children, mounted nowhere; nothing mounted at the data mount or either bind target; `/var/lib/docker-data`, `/var/lib/docker` and `/var/lib/containerd` each absent or an empty plain directory. | formats, then mounts everything and writes fstab |
| `ours` | XFS labeled `dockerdata`, no partition table, no children, mounted nowhere outside the allowed set, but at least one of the three mounts is missing; each unmounted path absent or an empty plain directory. | mounts what is missing, ensures fstab |
| `ready` | `ours`, mounted at the data mount from THIS device with `prjquota`, and both bind targets mounted from `<mount>/docker` / `<mount>/containerd` on THIS device. | ensures the three fstab entries (`present` only; a no-op on a node it built) |
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
* **blkid could not read the device**: `blkid -p` exits 2 both for "no
  signature" and for "could not open or read", and only the second prints
  anything; rc 2 with stderr is refused, never read as blank;
* blkid finds no signature but lsblk reports an `FSTYPE`, or the device has
  no signature yet is mounted somewhere;
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
* the data mount or a bind target is **not mounted and is not a plain
  directory** -- a symlink (say `/var/lib/docker -> /srv/docker`, whose
  state `find` would read as empty because it does not follow a symlinked
  start point), a regular file, anything else -- or could not be checked
  (stat failed);
* the data mount or a bind target is a directory whose contents **could not
  be listed** (`find` failed for any reason other than the path not
  existing): never taken for empty;
* `/var/lib/docker` or `/var/lib/containerd` is **not mounted but already has
  contents** -- docker or containerd ran on the root disk before the data
  disk was in place. Mounting over it would hide that state. Such a node is
  rebuilt with the data disk attached;
* `/var/lib/docker-data` is **not mounted but already has contents** on the
  root disk, which the mount would hide.

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
