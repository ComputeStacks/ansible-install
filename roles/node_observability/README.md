# node_observability

**Owner:** Wave 2D. **Hosts:** `nodes`.

The node-side observability containers — `cadvisor` (container metrics, scraped
by the metrics host) and `fluentd` (tenant logs, shipped to loki) — plus the
container image preloads the controller expects to find on a node.

`node-exporter` is **not** here: it is applied fleet-wide by a community role in
the all-hosts play.

## Container names are a contract

The containers are named exactly `cadvisor` and `fluentd`. The controller's
prometheus alerting carries an ignore-list matched on these names (v1
`container_ignore_list`), so a renamed system container starts firing tenant
alerts on every node. Do not rename them.

## Convergence

Both units bake their pinned image tag into `ExecStart`, so bumping
`cadvisor_image` / `fluentd_loki_image` in `versions.yml` re-renders the unit
and the handler restarts the container — and *only* then. v1 restarted cadvisor
on every converge and fluentd on every converge after the first, dropping
buffered log lines each run.

`fluentd` gets `RestartSec=30`: every tenant container on the node logs through
it, and the whole fleet restarts at once, so a crash loop must back off rather
than hammer docker.

## Host network and bind addresses (firewall-enforceable)

Both containers run with `--network=host` and **no `-p`/`--publish` flags**.
This is not cosmetic: a published container port is DNAT'd by docker in `nat`
PREROUTING and then traverses **forward**, which the `firewall` role's input
chain never sees — and contracts.md rule 7 forbids a forward-hook drop chain
(it would black-hole every published tenant port). A listener on the host
network terminates on the host, so the firewall's matrix entry for
`cs_ports.cadvisor` really is enforcement instead of documented intent. It also
matches how the existing fleet runs these containers.

Binds:

| Listener | Bind | Why |
|---|---|---|
| cadvisor | `0.0.0.0` (`node_observability_cadvisor_listen_ip`) | See below. |
| fluentd forward | `127.0.0.1` (`node_observability_fluentd_forward_bind`) | Only docker's log driver on this same host dials it; the bind, not a firewall rule, is what keeps it off-box. |

**Why cadvisor binds all interfaces rather than `primary_ip`.** The pairwise
address rule in docs/contracts.md makes the scrape address a property of the
*metrics host*, not of the node: prometheus dials a node's **tailscale**
address whenever the metrics host is itself tailnet-joined, and its `primary_ip`
otherwise. A cross-region node reached over the tailnet therefore receives a
connection addressed to its tailnet IP, and a socket bound to `primary_ip`
would refuse it — every cross-region node would report no container metrics
while looking healthy. Binding `primary_ip` would only be safe if the scrape
address were always `primary_ip`, which the contract explicitly says it is not.
Since moving to the host network is what made the port filterable in the first
place, the access control belongs in the firewall's input chain (accept 8080
from metrics addresses) rather than in the bind, and the bind stays permissive
so both scrape paths work. Set `node_observability_cadvisor_listen_ip` to
`{{ primary_ip }}` in an environment that never uses tailscale.

**Cross-wave note for the firewall role:** with the tailnet scrape path in use,
the node's accept for `cs_ports.cadvisor` (and `cs_ports.haproxy_stats`) needs
the same treatment as the 8500 rule — an `iifname "tailscale0"` accept when the
metrics host is tailnet-joined. An accept keyed only on the metrics host's
`primary_ip`/`public_ip` drops a scrape that arrives over `tailscale0`.

## Loki endpoint

`https://<metrics host>:{{ cs_ports.metrics_loki }}`, with basic auth
(`loki_basic_auth_password`). The host is `cs_metrics_domain` when set — that is
what the metrics host's certificate is issued for — and otherwise the metrics
host's inventory `primary_ip` (an inventory var, never a gathered fact, per
contracts.md rule 1). Credentials are written to
`/etc/fluentd/loki.env` (0600) and passed with `docker --env-file`, so the loki
password never appears in the world-readable unit file.

v2 assumes **one** shared metrics host. If `groups['metrics']` holds more than
one the role logs a warning and uses the first; splitting or replicating log
delivery across several metrics hosts is an open design question, not something
this role should decide silently.

## Image preloads

`borg_image`, `bastion_image`, `xtrabackup24_image`, `xtrabackup80_image` — all
pinned in `versions.yml`, pulled with `pull: not_present` so a converge only
moves an image when the pin moves.

v1's `preload_images.yml` had the two xtrabackup task names swapped against
their tags ("Pull image xtrabackup 8" pulled `2.4`, and vice versa). Both were
pulled, so nothing broke, but the intent was unreadable. v2 pulls by the pinned
variable, so the tag and the name cannot drift apart.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `node_observability_cadvisor_container` | `cadvisor` | Container name (contract — see above). |
| `node_observability_fluentd_container` | `fluentd` | Container name (contract). |
| `node_observability_cadvisor_listen_ip` | `0.0.0.0` | cadvisor bind address (see above). |
| `node_observability_fluentd_forward_bind` | `127.0.0.1` | fluentd forward bind — loopback only. |
| `node_observability_fluentd_forward_port` | `9432` | fluentd forward source. Node-local (docker's log driver dials it on this host), so deliberately **not** in `cs_ports`. |
| `node_observability_fluentd_restart_sec` | `30` | systemd backoff. |
| `node_observability_loki_username` | `loguser` | Basic-auth user on the loki vhost. |
| `node_observability_loki_endpoint` | derived | See above. |
| `node_observability_preload_images` | four pinned images | Preloads. |

Consumed, not owned: `cadvisor_image`, `fluentd_loki_image`, `borg_image`,
`bastion_image`, `xtrabackup24_image`, `xtrabackup80_image` (versions.yml),
`cs_ports.cadvisor`, `cs_ports.metrics_loki`, `cs_metrics_domain`,
`loki_basic_auth_password`.

The cadvisor container is passed `-listen_ip` and `-port={{ cs_ports.cadvisor }}`
so the address and port the metrics host scrapes come from the role variable and
the ports contract rather than from cadvisor's compiled-in defaults (v1 relied on
the defaults matching).
