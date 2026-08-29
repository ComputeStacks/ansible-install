# firewall

**Owner:** Wave 2F (with `tailscale` and `ubuntu_pro` — they interlock)

Host firewall for every ComputeStacks host. Owns exactly one nftables table,
`inet cs_static`, rendered from the ports contract
(`playbooks/group_vars/all/ports.yml`) and inventory group membership, and
applied by a role-owned oneshot unit. Also owns
`/etc/modules-load.d/cs-firewall.conf` (docs/contracts.md ownership map —
`node_kernel` owns `cs-node.conf`; the two files are never shared).

## What it installs

| Path | Purpose |
|---|---|
| `/etc/nftables.d/cs-static.nft` | The table. Rendered from `templates/cs-static.nft.j2`, `nft --check`-validated before install. |
| `/etc/systemd/system/cs-firewall.service` | Oneshot `nft -f <file>`, `RemainAfterExit`, `WantedBy=sysinit.target`, ordered `Before=network-pre.target`. No `ExecStop`. |
| `/etc/modules-load.d/cs-firewall.conf` | `nf_tables`, `nf_conntrack`, `xt_physdev` (also `modprobe`d immediately). |

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
tables below also gets an `iifname "tailscale0"` accept when both ends are
tailnet members — see [pairwise accepts](#pairwise-accepts-address-path-and-tailnet-path).

### Every host

| Port / match | Proto | Source | Control-plane row |
|---|---|---|---|
| — | any | `iifname lo` | — |
| — | any | `ct state established,related` (and `invalid` dropped) | — |
| icmp / icmpv6 sane types | icmp | anywhere | — |
| 546 | udp | `fe80::/64` destination (DHCPv6/SLAAC client) | — |
| `ssh` (22) | tcp | anywhere, or `firewall_allow_ssh_from` | 1, 4, 11, 19 |
| `node_exporter` (9100) | tcp | metrics addresses **+ `iifname tailscale0`** when both this host and the metrics host are tailnet members | 9 |
| 41641 | udp | anywhere — only when this host is on the tailnet | — (peer WireGuard) |
| `100.64.0.0/10`, `fd7a:115c:a1e0::/48` | any | **dropped** unless it arrives on `tailscale0` — only when this host is on the tailnet | — (anti-spoof) |

### nodes

| Port | Proto | Source | Row |
|---|---|---|---|
| `tenant_http` (80), `tenant_https` (443) | tcp | anywhere | 13 |
| `tenant_port_begin`–`tenant_port_end` (10000–50000) | tcp + udp | anywhere | 13 |
| `docker_tls` (2376) | tcp | controller addresses | 2 |
| `agent_http` (8500) | tcp | see [the 8500 rule](#the-8500-rule) | 3, 8 |
| `cadvisor` (8080), `haproxy_stats` (81) | tcp | metrics addresses **+ `iifname tailscale0`** when both this node and the metrics host are tailnet members | 9 |

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
| `ssh` (22) | tcp | covered by the global ssh accept (controller `DockerSSH`) | 19 |

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
* one `iifname "tailscale0" … accept` when **this host and that peer are both
  tailnet members** — the tailnet path.

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
| tailnet | opted out / no key | metrics addresses only | prometheus dials `primary_ip`, source is the metrics host's own address |
| not tailnet | tailnet | metrics addresses only | address path |
| not tailnet | not tailnet | metrics addresses only | address path |

In all four the public interface stays closed for 8080 and 81: the only accepts
that exist for those ports are the metrics host's addresses and, where both ends
are on the tailnet, `tailscale0`. That matters more than it used to — Wave 2D's
`node_observability` now runs cadvisor on the **host network bound to
`0.0.0.0`** (a `primary_ip` bind would refuse the tailnet scrape), so this
accept set is the only thing keeping 8080 off the public interface. The one way
to undo that is `firewall_extra_allowed_ipv4` / `_ipv6`, which are blanket
`accept`s for an address and therefore open every port on this host to it —
they are a v1-parity escape hatch, not a per-port knob.

## The 8500 rule

`agent_http` carries the controller's admin Bearer **in cleartext**, so it is
never opened publicly. Two states, derived pairwise exactly as
docs/contracts.md requires:

* **This node and the controller are both tailnet members** — the controller
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
tailscale_authkey is set  AND  hostvars[h].tailscale_enabled | default(true)
```

which mirrors `site.yml`'s gate and the `cs_agent` role's
`cs_agent_tailnet_joined`. It stays correct under `--limit`, where un-targeted
hosts have no facts at all — a fact-derived version would silently downgrade a
tailnet-only 8500 rule into a public-address one.

Consequence to know: `iifname "tailscale0" … accept` trusts *any* tailnet peer
for that port, not just the specific peer host. The tailnet is the control
plane and only ComputeStacks hosts are on it, so this is the intended trust
boundary; pinning the peer address would require reading another host's
tailnet IP (a fact) and would break under `--limit`.

## Attach mode (`existing_env: true`)

`tasks/main.yml` hard-guards the two paths: the nftables path never runs on a
flagged host (it would replace a working v1 policy with a table built from a
partial inventory view — docs/contracts.md rule 9). Instead
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
so a v6 append would need a different insertion point. The `regexp` uses
`regex_escape` (v1 passed the raw address, whose dots are regex wildcards).

Wave 4J's `attach_fragments` role calls this entry point directly:

```yaml
- ansible.builtin.include_role:
    name: firewall
    tasks_from: v1_append
```

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
| `cadvisor` 8080 (nodes) | Wave 2D `node_observability` | **Enforced.** Runs on the host network with no `-p`, bound `0.0.0.0`; this role's accept set is its only protection. |
| fluentd forward (nodes) | Wave 2D `node_observability` | **N/A.** Host network bound `127.0.0.1` — only docker's local log driver dials it, so there is no matrix entry at all. |
| `haproxy_stats` 81 (nodes) | Wave 2D `haproxy` | **Enforced.** haproxy runs on the host. |
| `metrics_prometheus` 3101 / `metrics_loki` 3102 (metrics) | Wave 3G `acme_web` | **Pending.** prometheus and loki themselves bind `127.0.0.1` inside their containers (Wave 2E); the nginx TLS terminator that will listen on 3101/3102 is Wave 3G's. It must run on the host network or bind host addresses — if it publishes with `-p`, these accepts become intent rather than enforcement. Flagged for Wave 3G. |

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `firewall_table` | `cs_static` | Table name. Never `cs_agent` (the agent owns that). |
| `firewall_config_file` | `/etc/nftables.d/cs-static.nft` | The rendered table. |
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
`tailscale_authkey`, `tailscale_enabled`, `group_names`, `groups`.

`firewall_tailscale_udp_port` is deliberately **not** in `cs_ports`: that file
is the control-plane graph (who dials whom on the ComputeStacks control plane),
and 41641 is peer-to-peer WireGuard, not a control-plane hop. Opening it lets
peers form direct connections instead of relaying through DERP. Flagged for the
manager in case the contract should absorb it anyway.

## Deviations from v1 (`roles/iptables`)

- nftables-native single table instead of a generated `iptables` shell script
  re-run by a handler (which appended duplicate rules on every converge).
- No `expose-ports` / `container-inbound` chain creation: the agent now renders
  its own native table and no longer needs the provisioner to pre-create them.
- v6 traffic is dropped by policy rather than `REJECT`-with-log; enable
  `firewall_log_dropped` for the log half.
- Container traffic to the host is limited to the agent port (v1 accepted all
  TCP from `container_network`).
- All source addresses come from `primary_ip` / `public_ip`; v1's per-role
  address vars (`consul_listen_ip`, `metrics_ip_address`, `node_ip`, …) are
  gone along with consul.
