# loki

Runs the Loki log store on the metrics host, loopback-only, for the
controller's log reads and the nodes' fluentd shipping.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Metrics host" | `metrics` |
| `add-region.yml`, `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** not run — an existing metrics host's loki is left as it is.
**Requires:** `metrics` earlier in the same play (it owns the `ops` network);
`community.docker`.

## Purpose

Runs the `loki-logs` container (unit name `loki.service`, matching v1) on
the metrics host, bound to `127.0.0.1:{{ loki_internal_port }}` (3100). It
joins the `ops` docker network **owned by the `metrics` role** — this role
never creates that network, and must run after `metrics` in the same play
(`playbooks/site.yml`'s "Metrics host" play is already ordered `acme_web,
metrics, loki`).

## What it does (`tasks/main.yml`)

1. Asserts `loki_image` is an exact pin (no `:latest`).
2. Creates `/etc/loki` and `/var/lib/loki`.
3. Installs `loki-config.yml` — **static, and the same configuration v1
   ships** (`roles/loki/files/loki-config.yml`; only YAML indentation
   differs): boltdb/filesystem storage, 336h
   (14-day) retention via `table_manager`, in-memory ring, single replica.
   No template variables — copied, not rendered, deliberately (nothing in
   it needs to vary per-install; see "Variables" for the one place a change
   would need to be kept in sync).
4. Creates the `loki-data` docker volume.
5. Pulls `loki_image`, renders the systemd unit, ensures the service is
   enabled/running.

Restart-on-change is handler-driven (`Restart loki`), notified by the
config copy, the image pull, and the unit template — full restart, no
reload, same reasoning as the `metrics` role (no ExecReload wired up).

## Exposure via acme_web

Loki binds loopback-only (3100); `acme_web`'s nginx serves it on
`cs_ports.metrics_loki` (3102) with TLS and basic auth, and this role writes
neither the vhost nor the htpasswd file — see
[`roles/acme_web/README.md`](../acme_web/README.md).

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `loki_ops_network` | `ops` | Consumed, not owned — see Purpose. |
| `loki_config_dir` / `loki_data_dir` | `/etc/loki` / `/var/lib/loki` | v1 paths. |
| `loki_internal_port` | `3100` | Loopback-only (see "Exposure via acme_web"); not in `cs_ports` for the same reason as `vault_listen_port` / `metrics_prometheus_internal_port`. **Coupled to `files/loki-config.yml`'s `server.http_listen_port: 3100`** — that file is copied verbatim (not templated), so changing this default alone does NOT change what loki listens on inside the container; the static file would need editing too. Left this way deliberately (v1 parity, and nothing in this repo has a reason to change it). |

Consumed, not owned: `loki_image` (`versions.yml`, pinned `2.9.10` —
**fleet parity; do NOT bump** without also checking every v1-built metrics
host, since the controller's Loki API v1 client and any existing chunk/index
data format are version-coupled).
