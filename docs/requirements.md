# Host requirements

What each machine must look like **before** the first run. The provisioner
installs everything else itself; preflight checks most of this and stops the
run, with nothing changed, when a host does not match.

## Every host

* **Ubuntu 26.04 LTS, amd64**, freshly installed. Hosts of an existing v1
  environment (Debian, `existing_env: true`) are the only exception, and
  attach mode only ever adds to them ([attach-mode.md](attach-mode.md)).
* **A single-word lowercase hostname**, no dots: `node101`, never
  `node101.example.com`. It is the inventory `hostname` and becomes the
  controller's `Node.hostname`.
* **Root SSH from the control machine, by key.**
* **A private address the control plane can use (`primary_ip`) and a public
  one (`public_ip`).** They may be the same address.
* **Outbound HTTPS** to the package and image sources: the Ubuntu archive,
  `download.docker.com`, `ghcr.io` and Docker Hub, `github.com` (acme.sh and
  borg releases), `apt.postgresql.org` (controller and nameservers),
  `repo.computestacks.com` (nodes), your ACME CA, and tailscale's servers
  when the tailnet is enabled.
* **One disk is enough** for every host except a node.

Nothing else: no docker, no python packages, no pre-made users.

## Sizing

The smallest sizes the full install has been run on end to end — a
greenfield `site.yml`, attach mode and a docker upgrade with reboot. They are
a floor for a test environment, not a production recommendation; the last
column says what grows.

| Group | vCPU | RAM | Root disk | Data disk | What grows |
| --- | --- | --- | --- | --- | --- |
| `controller` | 4 | 8 GB | 40 GB | — | postgres, and the `cstacks database-backup` dumps in `/var/lib/computestacks/backups` until you copy them off |
| `metrics` | 2 | 4 GB | 20 GB | — | prometheus TSDB and loki chunks (loki keeps 14 days), with the number of nodes |
| `backup` | 2 | 2 GB | 20 GB | — | borg repositories in `/var/lib/computestacks/backups`: every tenant volume, times retention. Size this one for storage first |
| `nameservers` | 2 | 2 GB | 12 GB | — | very little |
| `nodes` | 2 | 4 GB | 40 GB | **required**, 30 GB tested | everything tenant: images, container layers, volumes — all on the data disk |
| `registry` | — | — | — | — | not covered by the test environment; tenant registries store under `/computestacks-mnt` on the root disk |

A node's root disk holds only the OS, logs and packages once the data disk is
in place, so 40 GB stays enough however large the node grows. Its data disk
sets how much a node can host; size it for the tenants you expect, and grow
it later rather than over-buying (below).

## Node disk layout

Every node needs **two disks**: the root disk the OS is installed on, and a
second, blank data disk that `roles/node_storage` formats and owns. Docker
29 keeps images and container writable layers in containerd's image store
under `/var/lib/containerd`, not `/var/lib/docker`, so a node with one disk,
or with a data disk mounted only at `/var/lib/docker`, fills its root
filesystem.

```
/dev/vda   root disk, any layout the installer likes
  └─ /                       OS, logs, packages

/dev/vdb   data disk, whole device: XFS, label dockerdata, prjquota
  └─ /var/lib/docker-data
       ├─ docker/       bind-mounted on  /var/lib/docker
       └─ containerd/   bind-mounted on  /var/lib/containerd
```

**Hand it over blank.** The data disk must be a whole device with no
partition table, no filesystem and no mount, and nothing in fstab:

* do not partition or format it in the installer or the hypervisor template;
* make sure cloud-init leaves it alone — no `disk_setup`, `fs_setup` or
  `mounts` entry for it, and no image default that formats a second disk
  (some cloud images treat it as ephemeral storage);
* do not let docker run before the first converge: a node where
  `/var/lib/docker` or `/var/lib/containerd` already has contents is refused,
  because mounting over it would hide that state.

**Name it in the inventory** as `docker_data_device`, a path under `/dev/`.
Prefer a stable `/dev/disk/by-id/...` link where the platform offers one;
otherwise the kernel name: `/dev/vdb` on KVM/virtio, `/dev/xvdb` on Xen
(xcp-ng attaches its cloud-init config drive after the data disk, as
`/dev/xvdc`). Set it once as a `nodes` group var when every node looks the
same, per host when they differ.

**What the provisioner does with it**, before docker is installed: formats
it XFS with project quotas enforced (`prjquota` can only be enabled at mount
time, and it is what future per-volume size limits will use), mounts it at
`/var/lib/docker-data` by UUID, bind-mounts the two directories over docker's
and containerd's state, and adds a systemd drop-in so neither daemon can start
without those mounts. It formats **only** a device it can prove is blank;
anything else — the root disk, a config drive, a disk with partitions or any
filesystem signature, a different disk than the one a built node already
uses — stops the whole run in preflight, before any host is changed. The
full list is in [`roles/node_storage/README.md`](../roles/node_storage/README.md).

**The node will not boot without it.** The fstab entries carry no `nofail`,
on purpose: a missing data disk drops the node to emergency mode instead of
letting docker write to the root disk. Keep console access to your nodes.

**Growing it** is online and needs no partition step: enlarge the virtual
disk in the hypervisor, then on the node

```sh
xfs_growfs /var/lib/docker-data
```

XFS can grow but never shrink.

## Network

The host firewall is managed by the provisioner (`roles/firewall`). Any
provider-level firewall or security group in front of the hosts must allow
at least this from the internet:

| Host | Ports |
| --- | --- |
| `controller` | tcp 80, 443 |
| `nodes` | tcp 80, 443; tcp and udp 10000–50000 (published container ports) |
| `registry` | tcp 80, 443; tcp 10000–50000 (tenant registries) |
| `nameservers` | tcp and udp 53, IPv4 and IPv6 |
| `metrics` | tcp 80 (ACME HTTP-01); tcp 3101, 3102 from the controller and the nodes |

plus SSH from the control machine, and the private legs between hosts
(controller to node docker TLS, agent API and SSH; nodes to the
controller's ACME backend on 3000; metrics host to every node's exporters;
nodes to the backup server's SSH). Every leg, its port and
its credential is in [control-plane.md](control-plane.md); the port numbers
come from `playbooks/group_vars/all/ports.yml`. A node's container network
CIDR must not overlap anything else in the environment (RFC 1918, at least a
`/28`; `/22` is usual).

## DNS

The portal, metrics and registry names must resolve to their hosts before
the run, because their certificates are issued during it; with the bundled
nameservers the tenant zone must already be delegated to them. The records
are listed in [install.md §3](install.md#3-point-dns-at-the-hosts).
