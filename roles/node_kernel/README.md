# node_kernel

Kernel tuning for nodes: boot-time modules, conntrack sizing, container-density
sysctls with BBR, and keeping IPv6 router advertisements accepted on the
uplink once docker turns forwarding on.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Node base stack" | `nodes` |
| `add-region.yml` | "New node base stack" | `nodes` (the new node) |
| `maintain-nodes.yml` (via `node_upgrade` `tasks_from: maintain`) | "Maintain the nodes, one at a time" | `maintain_node_targets` (live nodes, inside a maintenance hold) |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** new node only. **Requires:** —

Does **not** own docker/firewall concerns — see `docs/contracts.md`
§Ownership: this role owns `/etc/modules-load.d/cs-node.conf`; the `firewall`
role owns its own, separate `/etc/modules-load.d/cs-firewall.conf`. Image
preloads belong to `node_observability` and docker configuration to
`docker_config`, not this role.

## What it does

- Loads `nf_conntrack` and `br_netfilter` on boot via
  `/etc/modules-load.d/cs-node.conf` (this role's own file).
- Sizes the conntrack hash table via `/etc/modprobe.d/nf_conntrack.conf`
  (module option — needs a module reload/reboot to take effect).
- Writes one consolidated sysctl file, `/etc/sysctl.d/60-cs-node.conf`,
  covering container-density tuning (conntrack max, inotify limits,
  open-file limits, backlog/buffer sizing) plus BBR
  (`net.core.default_qdisc=fq`, `net.ipv4.tcp_congestion_control=bbr`).
  Every sysctl lives in a file under `/etc/sysctl.d/`; nothing is written to
  `/etc/sysctl.conf`.
- When the node has a global IPv6 address (checked via this host's own
  facts — acceptable per docs/contracts.md, since this is a per-host file,
  not a shared template) **and** an `uplink_interface` inventory var is set,
  keeps router advertisements accepted on that interface (below). If
  `uplink_interface` is not set on a node that has a global IPv6 address,
  the role emits a `debug` note (not a failure) rather than guessing the
  interface name from facts. If it names an interface the host does not
  have, the role fails the host.
- Module loading, the sysctl reload and the networkd reload are
  handler-driven: they fire only when the owned file actually changes
  (docs/contracts.md rule 3).

### IPv6 router advertisements on the uplink

Docker sets `net.ipv6.conf.all.forwarding=1` when it creates an IPv6-enabled
network. A forwarding host ignores router advertisements unless told
otherwise, so the RA-learned default route expires about 30 minutes later.
Which fix applies depends on who handles RAs on the uplink, and the role asks
`networkctl --json=short status <uplink_interface>`:

- **systemd-networkd manages the link** (netplan's default renderer on
  Ubuntu server). networkd forces the kernel's `accept_ra` to 0 on every link
  it manages and processes RAs itself, in userspace, so an `accept_ra`
  sysctl is undone at the next boot or link reconfigure. The role writes
  `/etc/systemd/network/<network file>.d/60-cs-node-accept-ra.conf` with
  `[Network] IPv6AcceptRA=yes`, where `<network file>` is the `.network`
  file networkd reports for the link (for netplan,
  `10-netplan-<netplan id>.network`, generated into `/run/systemd/network`).
  The setting is explicit because networkd turns RA off by default whenever
  it considers IPv6 forwarding enabled (`systemd.network(5)`,
  `IPv6AcceptRA=`); explicitly enabled, it keeps accepting RAs regardless.
  The drop-in lives in `/etc` and is keyed by the file's name, so
  `netplan generate` / `netplan apply` regenerating `/run/systemd/network`
  leaves it in force, and the operator's netplan YAML is never edited. (A
  netplan `accept-ra: true` override was rejected: it has to repeat the
  operator's netplan id to merge, and applying it means `netplan apply`.)
  A change runs `networkctl reload`, which reconfigures the uplink (only
  links whose configuration changed). The link is not set down, but on a
  live node the reconfiguration can briefly drop and re-learn the
  RA-derived IPv6 addresses and routes on it; IPv4 on another interface is
  unaffected. With `node_kernel_uplink_ra_reload: false` the drop-in is
  written without the reload and takes effect at the next reboot or
  networkd restart. The role also removes
  `/etc/sysctl.d/61-cs-node-ipv6.conf` there (written by earlier versions of
  this role), so a later `sysctl --system` cannot set `accept_ra=2` and have
  the kernel process RAs alongside networkd.
- **Anything else** (ifupdown, NetworkManager, a link networkd leaves
  unmanaged, no networkd at all). The kernel handles RAs, so the role writes
  `/etc/sysctl.d/61-cs-node-ipv6.conf` with
  `net.ipv6.conf.<uplink_interface>.accept_ra = 2` (accept RAs even while
  forwarding) and reloads sysctl.

The drop-in sets `IPv6AcceptRA=yes` even where the operator's netplan says
`accept-ra: false` (for example a statically addressed IPv6 uplink), since a
drop-in overrides the generated `.network` file. Where RAs must stay off, set
`node_kernel_uplink_accept_ra: false`: the role then writes neither the
drop-in nor the sysctl and removes either one it wrote before (a removed
sysctl file leaves the kernel's `accept_ra` as is until the next boot). The
RA-learned default route is then the operator's concern; a static IPv6
default route is unaffected by forwarding.

networkd never touches the forwarding sysctls docker sets: netplan renders
no `IPv6Forwarding=`, and with it unset in both the `.network` file and
`networkd.conf` networkd leaves the sysctl alone. If the netplan id of the
uplink changes later, the old drop-in is left behind, inert; the next
converge writes the new one. The decision is
`filter_plugins/node_kernel.py`, tested by `tests/node_kernel_uplink_ra.yml`.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `node_kernel_conntrack_hashsize` | `"250000"` | nf_conntrack module hashsize (module option, not sysctl). |
| `node_kernel_sysctls` | see `defaults/main.yml` | Full sysctl key/value map written to `60-cs-node.conf`. |
| `node_kernel_accept_ra_dropin` | `60-cs-node-accept-ra.conf` | Name of the networkd drop-in that sets `IPv6AcceptRA=yes` on the uplink. |
| `node_kernel_uplink_accept_ra` | `true` | `false` leaves router advertisements on the uplink to the host's own network config: no drop-in, no sysctl, and any this role wrote before are removed. |
| `node_kernel_uplink_ra_reload` | `true` | `false` writes the networkd drop-in without running `networkctl reload`; it takes effect at the next reboot or networkd restart. |

Consumed, not owned: `uplink_interface` (host var, no default — when unset,
the router advertisement fix is skipped with a documented note, never guessed from
facts).
