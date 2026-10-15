# firewall

Host firewall for every ComputeStacks host: one role-owned nftables table,
`inet cs_static`, rendered from the ports contract and inventory, plus an
additive append path for existing shared hosts in attach mode.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Host firewalls" | `all` |
| `add-region.yml` | "New node firewall" | `nodes` (the new node) |
| `add-region.yml` (`tasks_from: append`) | "Existing metrics host — prometheus file_sd fragments only" | `metrics` |
| `add-region.yml` (`tasks_from: append`) | "Existing controller — firewall appends only" | `controller` |
| `add-region.yml` (`tasks_from: append`) | "Existing backup server — account, repository path, firewall appends" | `backup` |
| `firewall.yml` | "Host firewalls" | every host not `existing_env`; on an attach inventory, `nodes` only |
| `firewall.yml` (`tasks_from: append`) | "Existing shared hosts — new node address appends only" | `controller`, `metrics`, `backup` that are `existing_env` |
| `maintain-nodes.yml` (via `node_upgrade` `tasks_from: maintain`) | "Maintain the nodes, one at a time" | `maintain_node_targets` (live nodes, inside a maintenance hold) |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** the nftables table is never rendered on an `existing_env`
host; `tasks_from: append` adds the new node's addresses to whichever firewall
the host already runs (see [attach mode](#attach-mode-existing_env-true)).
`playbooks/firewall.yml` re-runs exactly these two paths on their own, for an
inventory change only this role reads (README.md, "Changing the firewall").
**Requires:** `community.general` (`modprobe`). Tailnet membership is read
from inventory, so it does not depend on `tailscale` having run.

Owns exactly one nftables table,
`inet cs_static`, rendered from the ports contract
(`playbooks/group_vars/all/ports.yml`) and inventory group membership, and
applied by a role-owned oneshot unit. Also owns
`/etc/modules-load.d/cs-firewall.conf` (docs/contracts.md §Ownership —
`node_kernel` owns `cs-node.conf`; the two files are never shared).

## What it installs

| Path | Purpose |
|---|---|
| `/etc/nftables.d/cs-static.nft` | The table. Rendered from `templates/cs-static.nft.j2`, `nft --check`-validated before install. |
| `/etc/systemd/system/cs-firewall.service` | Oneshot `nft -f <file>`, `RemainAfterExit`, `WantedBy=sysinit.target`, ordered `Before=network-pre.target`. No `ExecStop`. |
| `/etc/modules-load.d/cs-firewall.conf` | `nf_tables`, `nf_conntrack`, `xt_physdev` (also `modprobe`d immediately). |
| `/etc/nftables.d/cs-peers.nft` | Attach mode on a v2-built host only: additive peer rules, loaded by the same unit after the table (a missing file is a no-op). |

The distro `nftables.service` is **stopped and disabled** (see below).

## The three rules that must never be broken

1. **Never `flush ruleset`.** Docker uses the `iptables-nft` shim, so its rules
   live in nftables tables; cs-agent renders its own `table ip cs_agent`
   (published-port DNAT) over netlink. A `flush ruleset` — which is the first
   line of the distro `nftables.service`'s `/etc/nftables.conf` — destroys
   both: container networking stays dead until `dockerd` restarts, and every
   published tenant port closes until the agent's next reconcile. This role
   therefore ships its own unit and its own file, and disables the distro
   service so a reboot cannot do it either.
2. **Never a forward-hook chain.** Inbound traffic to a published tenant port
   is DNAT'd by `cs_agent`'s prerouting chain and then traverses **forward**,
   not input. A `policy drop` forward chain here would drop it — every tenant
   service on the node goes dark. Cross-project isolation is the agent's job
   (`DOCKER-USER`, which it re-asserts along with the `FORWARD -> DOCKER-USER`
   jump on every reconcile); this role must not create v1's `expose-ports` /
   `container-inbound` chains either.
3. **Apply idiom.** The file is

   ```
   table inet cs_static          # add: no-op if it already exists
   delete table inet cs_static   # so the delete can never fail
   table inet cs_static { … }    # the real definition
   ```

   one transaction, only our table touched. Verified: applying the rendered
   file twice in a row leaves a single `cs_static` table and leaves a
   pre-existing `ip cs_agent` / `ip filter` table byte-for-byte intact.

## Port matrix

Every port below is `cs_ports.<name>` from the ports contract; every address is
an inventory var (`primary_ip`, `public_ip`) — no gathered facts, so a
`--limit` run still renders the full picture (docs/contracts.md rule 1).
"controller / metrics / node addresses" always means *every* host in that
group, iterated (never `[0]`), and **every** source-restricted accept in the
tables below also gets an `iifname "tailscale0"` accept when both ends route
control-plane traffic over the tailnet (members, neither with
`tailscale_access_only: true`) — see [pairwise accepts](#pairwise-accepts-address-path-and-tailnet-path).
It also means every host in that group across **every site**: this role does
not site-scope, deliberately — see [sites](#sites-are-deliberately-ignored).

### Every host

| Port / match | Proto | Source | Control-plane row |
|---|---|---|---|
| — | any | `iifname lo` | — |
| — | any | `ct state established,related` (and `invalid` dropped) | — |
| icmp / icmpv6 sane types | icmp | anywhere | — |
| 546 | udp | `fe80::/64` destination (DHCPv6/SLAAC client) | — |
| `ssh` (22, or the host's `ssh_port`) | tcp | anywhere, or `firewall_allow_ssh_from` | 1, 4, 11, 19 |
| `node_exporter` (9100) | tcp | metrics addresses **+ `iifname tailscale0`** when both this host and the metrics host route over the tailnet | 9 |
| 41641 | udp | anywhere — whenever this host is on the tailnet, access-only or not | — (peer WireGuard) |
| `100.64.0.0/10`, `fd7a:115c:a1e0::/48` | any | **dropped** unless it arrives on `tailscale0` — only when this host is on the tailnet | — (anti-spoof) |

### nodes

| Port | Proto | Source | Row |
|---|---|---|---|
| `tenant_http` (80), `tenant_https` (443) | tcp | anywhere | 13 |
| `tenant_port_begin`–`tenant_port_end` (10000–50000) | tcp + udp | anywhere | 13 |
| `docker_tls` (2376) | tcp | controller addresses | 2 |
| `agent_http` (8500) | tcp | see [the 8500 rule](#the-8500-rule) | 3, 8 |
| `cadvisor` (8080), `haproxy_stats` (81) | tcp | metrics addresses **+ `iifname tailscale0`** when both this node and the metrics host route over the tailnet | 9 |

### controller

| Port | Proto | Source | Row |
|---|---|---|---|
| `controller_http` (80), `controller_https` (443) | tcp | anywhere | 5, 6 |
| `controller_acme_backend` (3000) | tcp | **every** node address | 7 |

### metrics

| Port | Proto | Source | Row |
|---|---|---|---|
| `controller_http` (80) | tcp | anywhere — ACME HTTP-01 webroot (`acme_web` runs here; no 443, its TLS vhosts are 3101/3102) | — |
| `metrics_prometheus` (3101) | tcp | controller addresses | 10 |
| `metrics_loki` (3102) | tcp | controller addresses + every node address | 10, 14 |
| `node_exporter` (9100) | tcp | metrics addresses (itself) **and** its own container bridges — prometheus scrapes the host exporter from a container on the `ops` network | 9 |

### registry

| Port | Proto | Source | Row |
|---|---|---|---|
| `controller_http` (80) | tcp | anywhere — ACME HTTP-01 webroot (`acme_web` runs here) | — |
| `tenant_https` (443) | tcp | anywhere | 18 |
| `tenant_port_begin`–`tenant_port_end` | tcp | anywhere | 18 |
| `ssh` (22, or the host's `ssh_port`) | tcp | covered by the global ssh accept (controller `DockerSSH`) | 19 |

### nameservers

| Port | Proto | Source | Row |
|---|---|---|---|
| `dns` (53) | tcp + udp, v4 + v6 | anywhere | 15 |
| `pdns_api` (8081) | tcp | controller addresses | 16 |
| `postgres` (5432) | tcp | the *other* nameserver hosts | 17 |

### backup

Nothing beyond the global ssh accept — nodes reach borg over ssh only (row 11).

## Pairwise accepts: address path and tailnet path

A source-restricted accept keyed only on a peer's `primary_ip` / `public_ip`
**drops that peer's traffic the moment the peer is tailnet-joined**: prometheus
on a tailnet-joined metrics host dials the node's *tailnet* address, so the
packet arrives on `tailscale0` with a `100.64.0.0/10` source that no
address-keyed rule matches (and the anti-spoof rule above sends anything
claiming that source on another interface straight to `drop`). Every
source-restricted rule in this role is therefore emitted through one helper,
`accept_from()`, which renders both halves:

* one `ip saddr <peer address> … accept` per peer address — the LAN /
  `primary_ip` / public path; and
* one `iifname "tailscale0" … accept` when **this host and that peer both
  route control-plane traffic over the tailnet** — the tailnet path.

Both stay in place at once, because a mixed fleet has both paths at once (a
same-L2 region with `tailscale_enabled: false` next to a remote tailnet
region). Membership on both sides is the pure-inventory predicate described
under [tailnet membership](#tailnet-membership-is-inventory-derived-not-a-fact),
so it holds under `--limit`.

This covers the exporter scrapes (`node_exporter` 9100 on every host,
`cadvisor` 8080 and `haproxy_stats` 81 on nodes) exactly as it covers
`docker_tls`, `controller_acme_backend`, the metrics vhosts, `pdns_api` and the
nameserver `postgres` replication. `agent_http` is the one deliberate exception
— it is *exclusively* tailnet when both ends are joined, never both paths (see
below), because the admin Bearer it carries is cleartext.

Scenario check for a node's exporter ports (verified by rendering each case and
running `nft --check`):

| metrics host | this node | Rules emitted for 9100 / 8080 / 81 | Scrape path |
|---|---|---|---|
| tailnet | tailnet | metrics addresses **and** `iifname tailscale0` | tailnet |
| tailnet | opted out / no key / access-only | metrics addresses only | prometheus dials `primary_ip`, source is the metrics host's own address |
| not tailnet or access-only | tailnet | metrics addresses only | address path |
| not tailnet | not tailnet | metrics addresses only | address path |

In all four the public interface stays closed for 8080 and 81: the only accepts
that exist for those ports are the metrics host's addresses and, where both ends
are on the tailnet, `tailscale0`. That matters because
`node_observability` runs cadvisor on the **host network bound to
`0.0.0.0`** (a `primary_ip` bind would refuse the tailnet scrape), so this
accept set is the only thing keeping 8080 off the public interface. The one way
to undo that is `firewall_extra_allowed_ipv4` / `_ipv6`, which are blanket
`accept`s for an address and therefore open every port on this host to it —
they are a v1-parity escape hatch, not a per-port knob.

## Sites are deliberately ignored

`site` (docs/contracts.md §Site scoping) partitions the fleet: one metrics host
and at most one backup server per physical facility. This role ignores it, and
that is a decision rather than an oversight.

The tempting change is to narrow the `node_exporter` (9100) and
`cadvisor` / `haproxy_stats` accepts from `groups['metrics']` to the asking
host's own site — `cs_site_metrics_hosts[cs_site]`. It breaks the fleet. The
9100 accept is emitted on *every* host, and the controller, nameservers,
registry and backup servers have no `site` at all: for them the narrowed
expression resolves to nothing, no accept is emitted, and every infrastructure
scrape target stops answering — on single-site installs as well, where `site`
is unset everywhere by design.

Accepting scrapes from every metrics host is therefore over-permissive by
exactly one already-trusted control-plane host per extra site, on ports that
serve nothing but metrics. That is the correct trade, and the comment at the
9100 accept in `templates/cs-static.nft.j2` says so at the point where somebody
would otherwise "fix" it.

Attach mode is the one place the fleet partition does show up, and it is
handled by `existing_env` rather than by `site`: `tasks/append.yml` appends
addresses only for nodes this run provisions, skipping hosts already flagged
`existing_env` (docs/contracts.md rule 9).

## The 8500 rule

`agent_http` carries the controller's admin Bearer **in cleartext**, so it is
never opened publicly. Two states, derived pairwise exactly as
docs/contracts.md requires:

* **This node and the controller both route over the tailnet** — the controller
  dials the node's tailnet address (`node.agent_host` in the manifest), so the
  only accept is `iifname "tailscale0" tcp dport 8500`. The anti-spoof rule
  above it drops any packet claiming a tailnet source that arrives on another
  interface.
* **Otherwise** — accept from the controller's `primary_ip` / `public_ip` only.

In both states the node also accepts 8500 **from its own container bridges**
(`br-*`, `docker0`). Tenant containers reach the agent through
`metadata.internal`, which resolves to the node's `primary_ip` (control-plane
row 8), so those packets enter the input hook with `iifname` = the project
bridge. Matching on the interface rather than on `container_network` is
deliberate: project bridge subnets are allocated by the controller at runtime,
and a strict source-CIDR match would silently break the metadata endpoint for
any network created outside the declared pool. This is narrower than v1, which
accepted **all** TCP from `container_network`; adjust with
`firewall_container_ports` if a future container needs another host port.

### Tailnet membership is inventory-derived, not a fact

The role needs to know *whether* a host is on the tailnet, never *which*
address it has — tailnet traffic is matched by interface. Membership is
therefore computed from inventory alone:

```
(hostvars[h].tailscale_enabled | default(false) | bool)
```

the membership predicate of docs/contracts.md §Tailnet membership, which
`site.yml`'s gate also uses (`firewall_tailnet_hosts`). An auth key is not
membership, and neither is tailscale installed or logged in on a host: a
host with `tailscale_enabled` false or unset gets no tailnet rules at all,
even one someone joined by hand. It decides the
anti-spoof drops and the UDP 41641 accept. The `tailscale0` accepts follow the
narrower routing predicate (`firewall_tailnet_routed_hosts`), which adds
`and not (hostvars[h].tailscale_access_only | default(false) | bool)` and
mirrors the `cs_agent` role's `cs_agent_tailnet_routed`
(docs/contracts.md §Tailscale access-only mode): an access-only host joins the
tailnet for operator access, but no control-plane leg to or from it is
accepted on `tailscale0`. Both stay correct under `--limit`, where un-targeted
hosts have no facts at all — a fact-derived version would silently downgrade a
tailnet-only 8500 rule into a public-address one.

The ssh accept is not tailnet-aware. Tailscale SSH (`--ssh` in
`tailscale_extra_args`) is answered by `tailscaled` itself and needs no rule
here; plain `ssh` to a host's tailnet address reaches sshd only when
`firewall_allow_ssh_from` is empty or lists `100.64.0.0/10`.

Consequence to know: `iifname "tailscale0" … accept` trusts *any* tailnet peer
for that port, not just the specific peer host. The tailnet is the control
plane and only ComputeStacks hosts are on it, so this is the intended trust
boundary; pinning the peer address would require reading another host's
tailnet IP (a fact) and would break under `--limit`.

## Attach mode (`existing_env: true`)

`tasks/main.yml` hard-guards the two paths: the nftables path never runs on a
flagged host (it would replace a working policy with a table built from a
partial inventory view — docs/contracts.md rule 9). Instead it includes
`tasks/append.yml`, which is also the entry point the add-region plays call
directly on the existing metrics, controller and backup hosts:

```yaml
- ansible.builtin.include_role:
    name: firewall
    tasks_from: append
```

`append.yml` asserts the host is flagged and detects which firewall it runs —
never assumes:

* **v1-built host** (`/usr/local/bin/cs-recover_iptables` present) →
  `tasks/v1_append.yml`, below.
* **v2-built host** (no v1 script) → `tasks/v2_append.yml`: re-templates the
  role-owned unit (it carries no inventory data) and adds one
  `add rule inet cs_static input … saddr <addr> accept` line per new node
  address to `/etc/nftables.d/cs-peers.nft`, which the unit loads after the
  table. `cs-static.nft` itself is never re-rendered from the partial
  inventory, and the peers file is only ever appended to. A host with neither
  the v1 script nor the v2 table fails loudly.

`tasks/v1_append.yml`:

1. appends `iptables -A INPUT -p all -s <addr> -j ACCEPT` for every new node
   address to `/usr/local/bin/cs-recover_iptables` with v1's own `lineinfile`
   idiom (`roles/iptables/tasks/update.yml` in the v1 tree) — the blanket
   accept is how v1 grants a peer its whole inbound set;
2. makes the same rules live with a `-C` check followed by `-A` only where
   missing, instead of re-running the script (v1's own handler re-runs it,
   appending duplicate rules on every converge);
3. fails loudly if the script is absent — a flagged host that is not a v1 host
   is a stop-and-report situation, not something to guess at.

Only IPv4 node addresses are appended: v1's `ip6tables` chain ends in a REJECT,
so a v6 append would need a different insertion point. Appending at EOF is safe
because v1 drops by **policy** (`iptables -P INPUT DROP`), not by a terminal
rule, so a later `-A INPUT … ACCEPT` still takes effect.

The `regexp` is anchored on the **whole rule**, not on the address. v1 passed
the raw address and v2 originally passed `item | regex_escape`, which escapes
the dots but does not anchor — and `lineinfile` *replaces* the last line its
regexp matches. Appending `10.100.1.1` to a script that already accepted
`10.100.1.10` therefore matched inside that address and deleted an existing
peer's blanket accept. Nothing broke at the time (the `-C` / `-A` tasks only
add to the live chain), so the damage surfaced at that host's next reboot or
`systemctl restart cs-iptables`, as a production node that had lost docker
mTLS, cs-agent and loki ingest.

The anchored form matches exactly the rule this role writes, modulo whitespace,
and requires the address to be followed by whitespace so a CIDR accept
(`-p all -s 10.100.0.0/21 -j ACCEPT`, the shape v1's own upgrade notes use for
the container network) can never be matched and narrowed to a single host. The
deliberate consequence: a hand-edited variant — different flag order, an
absolute `/sbin/iptables`, a trailing `-m comment` — does not match and gets a
second, additive line. A duplicate `-A … ACCEPT` is cosmetic and self-limiting;
rewriting a line on a production boot script we did not write is not.

## What an input chain can and cannot enforce

An input chain only sees traffic terminating on the **host**. A port published
by a container (`-p 8080:8080`) is DNAT'd in `nat` PREROUTING by docker and then
traverses **forward**, so an input accept for such a port is documented intent,
not enforcement — and closing it would require a forward-hook chain, which
rule 7 forbids. The fix belongs in the owning role: run the listener on the host
network (or bind it to a host address), never publish it.

Current status of every port in the matrix that is served by a container:

| Port | Owning role | Status |
|---|---|---|
| `cadvisor` 8080 (nodes) | `node_observability` | **Enforced.** Runs on the host network with no `-p`, bound `0.0.0.0`; this role's accept set is its only protection. |
| fluentd forward (nodes) | `node_observability` | **N/A.** Host network bound `127.0.0.1` — only docker's local log driver dials it, so there is no matrix entry at all. |
| `haproxy_stats` 81 (nodes) | `haproxy` | **Enforced.** haproxy runs on the host. |
| `metrics_prometheus` 3101 / `metrics_loki` 3102 (metrics) | `acme_web` | **Enforced.** The nginx TLS terminator runs with `--network=host` and no `-p`, in front of loopback-only prometheus and loki — see [`roles/acme_web/README.md`](../acme_web/README.md). |

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `firewall_table` | `cs_static` | Table name. Never `cs_agent` (the agent owns that). |
| `firewall_config_file` | `/etc/nftables.d/cs-static.nft` | The rendered table. |
| `firewall_peers_file` | `/etc/nftables.d/cs-peers.nft` | Attach-mode additive rules on a v2-built host; appended to, never rewritten. |
| `firewall_service` | `cs-firewall.service` | Role-owned unit. |
| `firewall_validate_config` | `true` | `nft --check --file` before install. |
| `firewall_disable_distro_service` | `true` | Stop/disable `nftables.service`. |
| `firewall_modules` | `nf_tables`, `nf_conntrack`, `xt_physdev` | Boot + immediate module loads. |
| `firewall_tailscale_interface` | `tailscale0` | Interface the tailnet rules match. |
| `firewall_tailscale_udp_port` | `41641` | WireGuard peer port; see note below. |
| `firewall_container_interfaces` | `["br-*", "docker0"]` | This host's own container bridges. |
| `firewall_container_ports` | `agent_http` on nodes, `node_exporter` on metrics | Ports accepted from those bridges. |
| `firewall_allow_ssh_from` | `[]` (anywhere) | Restrict ssh to these sources. |
| `firewall_extra_allowed_ipv4` / `_ipv6` | `[]` | v1 parity (`extra_allowed_ipv4_addresses`). |
| `firewall_extra_input_rules` | `[]` | Raw nft statements appended to the input chain. |
| `firewall_log_dropped` | `false` | Rate-limited log before the drop policy. |
| `firewall_v1_script` | `/usr/local/bin/cs-recover_iptables` | Attach-mode target. |

Consumed, not owned: `cs_ports`, `primary_ip`, `public_ip`, `existing_env`,
`tailscale_enabled`, `tailscale_access_only`, `group_names`, `groups`.

`firewall_tailscale_udp_port` is deliberately **not** in `cs_ports`: that file
is the control-plane graph (who dials whom on the ComputeStacks control plane),
and 41641 is peer-to-peer WireGuard, not a control-plane hop. Opening it lets
peers form direct connections instead of relaying through DERP.
