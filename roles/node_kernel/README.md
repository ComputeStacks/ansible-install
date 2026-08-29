# node_kernel

**Owner:** Wave 1A

Node-only kernel tuning (`hosts: nodes`). Does **not** own docker/firewall
concerns — see `docs/contracts.md` ownership map: this role owns
`/etc/modules-load.d/cs-node.conf`; the `firewall` role (Wave 2F) owns its
own, separate `/etc/modules-load.d/cs-firewall.conf`. Image preloads and
docker configuration are owned by later waves (2D/1B), not this role.

## What it does

- Loads `nf_conntrack` and `br_netfilter` on boot via
  `/etc/modules-load.d/cs-node.conf` (this role's own file).
- Sizes the conntrack hash table via `/etc/modprobe.d/nf_conntrack.conf`
  (module option — needs a module reload/reboot to take effect, matching
  v1's documented caveat).
- Writes one consolidated sysctl file, `/etc/sysctl.d/60-cs-node.conf`,
  covering container-density tuning (conntrack max, inotify limits,
  open-file limits, backlog/buffer sizing) plus BBR
  (`net.core.default_qdisc=fq`, `net.ipv4.tcp_congestion_control=bbr` — new
  in v2, not present in v1).
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

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `node_kernel_conntrack_hashsize` | `"250000"` | nf_conntrack module hashsize (module option, not sysctl). |
| `node_kernel_sysctls` | see `defaults/main.yml` | Full sysctl key/value map written to `60-cs-node.conf`. |

Consumed, not owned: `uplink_interface` (host var, no default — when unset,
the accept_ra fix is skipped with a documented note, never guessed from
facts).

## Deviations from v1 (`roles/node`)

- **Two conflicting inotify overrides in v1, resolved to one.** v1's
  `kernel_params.yml` set `fs.inotify.max_user_instances`/
  `max_user_watches` to `8388608`/`16777216` in its main "high container
  density" block, then later overrode the *same* keys to the much smaller
  `1024`/`1048576` in a second block explicitly commented "for high
  container density" — the opposite direction the comment claims. This
  looks like a leftover/duplicate edit bug in v1, not an intentional
  tightening. **Ported only the larger, comment-intentional values**
  (`8388608`/`16777216`); the smaller conflicting override was dropped.
- Everything else in v1 that didn't specify a `sysctl_file` was written to
  `/etc/sysctl.conf` directly. Per Wave 1A instructions, **all sysctls are
  now consolidated into files under `/etc/sysctl.d/`** instead
  (`60-cs-node.conf`, plus `61-cs-node-ipv6.conf` when applicable).
- **BBR is new in v2** — not present in v1.
- **IPv6 handling changed.** v1 shipped a static
  `/etc/sysctl.d/50-ipv6.conf` that *disabled* IPv6 entirely
  (`net.ipv6.conf.{all,default}.disable_ipv6 = 1`) — not ported (out of
  scope for this role; IPv6 enablement/disablement is a network/product
  decision made elsewhere). Instead, this role adds the new `accept_ra=2`
  uplink fix described in the v2 plan, gated on the node actually having a
  global IPv6 address and an explicit `uplink_interface` var.
- Does not port v1's `preload_images.yml` (container image preloads —
  owned by Wave 2D) or any docker-related tuning.
- Module loading and sysctl reload are handler-driven (fire only when the
  owned file actually changes) rather than v1's always-run
  `modprobe`/immediate-apply tasks — consistent with docs/contracts.md's
  convergent-role / handlers-restart-on-change rule.
