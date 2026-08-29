# The control-plane graph

Who dials whom, on what address, with what credential. This graph is what the
play order, the firewall rules, the manifest fields and the tailscale decision
are all derived from, and it is the source
[`playbooks/group_vars/all/ports.yml`](../playbooks/group_vars/all/ports.yml)
is derived from — **change both together or neither**.

A two-region install is assumed throughout: the controller in region A, a
second node in a remote region B. Addresses are named by their inventory or
manifest variable, never by value.

| # | From → To | Address | Port / proto | Credential | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | operator → all hosts | `ansible_host` | `ssh` | operator key | The ansible transport itself. |
| 2 | controller → node dockerd | `primary_ip` | `docker_tls` TLS | vault-issued PKI client cert | Heartbeat and every container operation. |
| 3 | controller → cs-agent | `agent_host` if set, else `primary_ip` | `agent_http` HTTP | per-node admin Bearer | Admin API. **Cleartext** — see below. |
| 4 | controller → node, root ssh | `primary_ip` | `ssh` | controller app + root keys | Volumes, haproxy certificate deploy, LB reload. |
| 5 | node → controller (enrolment) | portal domain | `controller_https` | `NODE_ENROLLMENT_TOKEN` | v2 does not use this leg — see below. |
| 6 | node → controller (LB config fetch) | `https://<Setting.hostname>` | `controller_https` | none (signed URL path) | The node curls this after the controller triggers it over #4. |
| 7 | node haproxy → controller (ACME backend) | `regions[].acme_server` | `controller_acme_backend` HTTP | none | Tenant HTTP-01 challenges proxied back to the portal. |
| 8 | container → cs-agent | `metadata.internal` = `primary_ip` | `agent_http` HTTP | tenant Bearer | The agent must be reachable on `primary_ip`. |
| 9 | prometheus (metrics host) → node exporters | node address, pairwise-derived | `node_exporter` / `cadvisor` / `haproxy_stats` | none | **Cleartext, unauthenticated.** `haproxy_stats` must equal the port in `load_balancer.stats_bind`. |
| 10 | controller → prometheus / loki (read) | metrics host | `metrics_prometheus` / `metrics_loki` TLS | basic auth | The `MetricClient` / `LogClient` endpoints — nginx vhosts, **not** 443. |
| 11 | node → backup server | backup host | `ssh` | per-node borg key, `cstacks` user | `borg serve` plus the mkdir/rm shell. |
| 12 | node / agent → package and image repos | public | 443 | none | Package and image pulls. |
| 13 | internet → node haproxy (tenant ingress) | node public | `tenant_http` / `tenant_https` + `tenant_port_begin`–`tenant_port_end` tcp/udp | — | Published container ports. |
| 14 | node fluentd → loki (write) | metrics host | `metrics_loki` TLS | basic auth | Container log shipping. |
| 15 | internet → nameservers | ns hosts | `dns` tcp/udp, v4 + v6 | — | Authoritative DNS. |
| 16 | controller → pdns API | primary nameserver | `pdns_api` HTTP | pdns API key | Zone management. |
| 17 | ns follower ↔ primary | ns hosts | `postgres` | pg replication | powerdns database replication. |
| 18 | internet → registry | registry public | 443 + `tenant_port_begin`–`tenant_port_end` | registry auth | Tenant registries. |
| 19 | controller → registry | registry host | `ssh` | controller app key | `DockerSSH` container management. |

Every port named above resolves through `cs_ports` in `ports.yml`. No role or
template writes a literal port number (`docs/contracts.md` rule 5), and the
firewall opens exactly the rows that apply to a host's groups.

## What the graph decides

### Rows 3 and 9 are cleartext without a private path

Firewalling narrows *who* can connect; it does not encrypt. Whenever a region
has no private path to the controller and the metrics host, the per-node admin
Bearer (#3) and every scrape (#9) cross the public internet in the clear.

The answer is tailscale, enabled fleet-wide when `tailscale_authkey` is set
(controller, metrics host and every node that has not opted out with
`tailscale_enabled: false`). Where a region shares an L2 with the controller,
opting out is reasonable and the two legs ride the LAN. A remote region with
tailscale disabled sends the admin Bearer and scrapes in cleartext: an
explicit operator choice, documented rather than prevented.

### The agent's listen address follows from rows 3 and 8

`metadata.listen_addr` cannot be the tailscale address (containers reach the
agent through `metadata.internal`, which resolves to `primary_ip` — row 8) and
cannot be `primary_ip` only (with tailscale on, the controller dials the
tailnet address — row 3). So a tailnet-joined node listens on `:8500`, all
interfaces, and the **firewall** is the enforcement: `agent_http` is accepted
from `tailscale0` and the local/docker paths, never from the public interface.
That is why the firewall and tailscale roles ship together.

### Tailscale address derivation is pairwise, never per-node

* `nodes[].agent_host` = the node's tailnet address **only if the controller**
  is tailnet-joined; otherwise omitted, and the controller dials `primary_ip`.
* The prometheus scrape address for a node = its tailnet address **only if the
  metrics host** is tailnet-joined; otherwise `primary_ip`.

A tailnet-only `agent_host` on a controller that is not itself on the tailnet
is unreachable, and the failure is silent — orders are rejected while every
dashboard stays green.

### Row 7: `acme_server` is the controller as reached *from that az*

`regions[].acme_server` is not "the controller's address"; it is the address
**that az's node** can reach the controller's ACME backend on: the controller's
tailnet address when both ends are paired, otherwise an address routable from
that region. The controller's firewall must accept `controller_acme_backend`
from every node address, and a remote region with no tailnet needs
`controller_acme_address` set as a host var — its `primary_ip` fallback routes
on the controller's own network and nowhere else. `roles/validate`'s
`acme_backend` check probes this leg from each node so the mistake fails the
install instead of the first tenant certificate.

v1 solved the same problem by rewriting `/etc/hosts` on every node
(`local_dns`). v2 does not: the portal domain resolves publicly, and where a
private path exists, `acme_server` carries it.

### Row 5 is not how v2 enrols

The HTTP enrolment endpoint needs `NODE_ENROLLMENT_TOKEN` in the controller
environment (absent on v1 controllers) and matches on source IP, which is
fragile behind a CDN or a tailnet. v2 instead reads the hash controller-side —
`cstacks runner` against `Node.find_by(hostname: …).agent_token_hash`,
delegated to the controller host — and writes it into the node's `agent.yml`.
Uniform in both modes, no environment change, no restart, no source-IP
dependency. Greenfield still sets `NODE_ENROLLMENT_TOKEN` so upstream's manual
flow keeps working.

### Rows 4 and 19 are one role

`ssh_trust` installs the controller's app and root pubkeys into the root
`authorized_keys` of **every host the controller dials**: the nodes *and* the
registry host. The controller manages tenant registries with `DockerSSH` over
`ssh://<Setting.registry_node>`, so a nodes-only version breaks every
private-registry order with nothing but a SystemEvent to show for it. The
controller's own `/root/.ssh/config` is rendered whole in greenfield only.

Related: the `haproxy` role pre-creates `/etc/haproxy/certs` and
`/etc/haproxy/errors/*`, because the controller deploys certificates there over
row 4. The agent does **not** write haproxy configuration — the controller
does, over row 4 plus the node's fetch in row 6.

### The graph fixes the play order

`agent:datachannel_backfill` calls every node's agent, and agents answer 401
until they are enrolled. So `site.yml` runs: controller up → `bootstrap:apply`
(creates the Node rows and mints the tokens) → the nodes play (`cs_agent`
installs, enrols, restarts) → a **second** controller play
(`controller_post_enroll`: datachannel backfill, metadata agent backfill,
`test_connection:all`) → `validate`. `controller_seed` and
`controller_post_enroll` are separate roles for exactly this reason.
