# loki

**Owner: Wave 2E.** `hosts: metrics`. Straight port of v1's `roles/loki`.

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
3. Installs `loki-config.yml` — **static, ported byte-for-byte from v1**
   (`roles/loki/files/loki-config.yml`): boltdb/filesystem storage, 336h
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

## Interface for Wave 3G (`acme_web`)

Binds loopback-only at `127.0.0.1:{{ loki_internal_port }}` (3100). Wave 3G
fronts it with an nginx vhost at `cs_ports.metrics_loki` (3102, TLS + basic
auth) for both the controller's LogClient reads and node fluentd log
shipping (write). This role writes neither the vhost nor the htpasswd file.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `loki_ops_network` | `ops` | Consumed, not owned — see Purpose. |
| `loki_config_dir` / `loki_data_dir` | `/etc/loki` / `/var/lib/loki` | v1 paths. |
| `loki_internal_port` | `3100` | Loopback-only (see "Interface for Wave 3G"); not in `cs_ports` for the same reason as `vault_listen_port` / `metrics_prometheus_internal_port`. **Coupled to `files/loki-config.yml`'s `server.http_listen_port: 3100`** — that file is copied verbatim (not templated), so changing this default alone does NOT change what loki listens on inside the container; the static file would need editing too. Left this way deliberately (v1 parity, and nothing in this repo has a reason to change it). |

Consumed, not owned: `loki_image` (`versions.yml`, pinned `2.9.10` —
**fleet parity; do NOT bump** without also checking every v1-built metrics
host, since the controller's Loki API v1 client and any existing chunk/index
data format are version-coupled).

## Requirements

`community.docker` (pinned in `requirements.yml`). Runs after the `metrics`
role on the same host.

## Deviations from v1 (`roles/loki`)

* v1's per-tag task gating (`tags: ['never', 'bootstrap', 'addnode']`,
  `when: ansible_facts.services['loki.service'] is not defined`) is gone —
  convergent by default (docs/contracts.md rule 3): re-running reconciles
  config/image/unit state, handlers fire only on actual change.
* v1 created the `ops` network itself (`community.general.docker_network` in
  `roles/loki/tasks/install.yml`) — one of the two places v1 created it
  redundantly (the other being `roles/nginx/tasks/preflight.yml`). This role
  does not; `metrics` is now the sole owner (docs/contracts.md ownership
  map, Wave 0).
* `loki_image` is a single `repo:tag` pin, replacing v1's split
  `loki_image` / `loki_image_tag` vars — value unchanged (`2.9.10`).
* Uses `community.docker.docker_image_pull` / `docker_volume` (pinned
  collection) in place of v1's `community.general.docker_image` /
  `docker_volume`.
