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
| `node_observability_fluentd_forward_port` | `9432` | fluentd forward source. Node-local (docker's log driver dials it on this host), so deliberately **not** in `cs_ports`. |
| `node_observability_fluentd_restart_sec` | `30` | systemd backoff. |
| `node_observability_loki_username` | `loguser` | Basic-auth user on the loki vhost. |
| `node_observability_loki_endpoint` | derived | See above. |
| `node_observability_preload_images` | four pinned images | Preloads. |

Consumed, not owned: `cadvisor_image`, `fluentd_loki_image`, `borg_image`,
`bastion_image`, `xtrabackup24_image`, `xtrabackup80_image` (versions.yml),
`cs_ports.cadvisor`, `cs_ports.metrics_loki`, `cs_metrics_domain`,
`loki_basic_auth_password`.

The cadvisor container is passed `-port={{ cs_ports.cadvisor }}` so the port the
metrics host scrapes comes from the ports contract rather than from cadvisor's
compiled-in default (v1 relied on the default matching).
