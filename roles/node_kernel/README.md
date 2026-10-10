# node_kernel

Kernel tuning for nodes: boot-time modules, conntrack sizing, container-density
sysctls with BBR, and the IPv6 `accept_ra` fix on the uplink.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Node base stack" | `nodes` |
| `add-region.yml` | "New node base stack" | `nodes` (the new node) |
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
  writes `/etc/sysctl.d/61-cs-node-ipv6.conf` forcing
  `net.ipv6.conf.<uplink_interface>.accept_ra = 2` — otherwise Docker
  setting `net.ipv6.conf.all.forwarding=1` on IPv6 network creation silently
  withdraws the RA-learned default route ~30 minutes later. If
  `uplink_interface` is not set on a node that has a global IPv6 address,
  the role emits a `debug` note (not a failure) rather than guessing the
  interface name from facts.
- Module loading and the sysctl reload are handler-driven: they fire only
  when the owned file actually changes (docs/contracts.md rule 3).

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `node_kernel_conntrack_hashsize` | `"250000"` | nf_conntrack module hashsize (module option, not sysctl). |
| `node_kernel_sysctls` | see `defaults/main.yml` | Full sysctl key/value map written to `60-cs-node.conf`. |

Consumed, not owned: `uplink_interface` (host var, no default — when unset,
the accept_ra fix is skipped with a documented note, never guessed from
facts).
