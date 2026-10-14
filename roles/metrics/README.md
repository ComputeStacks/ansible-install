# metrics

Runs prometheus and alertmanager on the metrics host, owns the `ops` docker
network, and renders the per-AZ prometheus file_sd fragments — including, in
attach mode, on an existing metrics host.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Metrics host" | `metrics` |
| `add-region.yml` (`tasks_from: file_sd`) | "Existing metrics host — prometheus file_sd fragments only" | `metrics` |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** `tasks_from: file_sd` only — writes this new region's
fragment files and nothing else (see [attach mode](#attach-mode-existing_env-true)).
**Requires:** `geerlingguy.docker` and `docker_config` on the host (site.yml's
"Docker hosts" play); `community.docker`.

## Purpose

Containerized prometheus + alertmanager on the metrics host. This role is
the **sole owner of the `ops` docker network** (docs/contracts.md ownership
map) — `loki` (same host) joins it but never creates it, and
`playbooks/site.yml`'s "Metrics host" play runs `metrics` before `loki` for
exactly this reason.

It also owns the prometheus **per-AZ file_sd contract**: one rendered
fragment file per node per exporter, in v1's exact paths
(`/etc/prometheus/{node_exporter,cadvisor,haproxy}/<az>.yml`), which is both
how greenfield installs work and the sole mechanism attach mode uses to
extend an **existing** metrics host, v1- or v2-built.

A metrics host serves exactly one **site** (docs/contracts.md §Vocabulary),
and this role renders fragments for that site's nodes and no others — see
"Which nodes get a fragment" below.

## What it does, in order (`tasks/main.yml`)

1. Asserts `prometheus_image` / `alertmanager_image` are exact pins (no
   `:latest`).
2. Creates the `ops` docker network.
3. Creates `/etc/prometheus` and the `prometheus-data`/`alertmanager-data`
   docker volumes.
4. Installs the static v1 alert rule files (`alerts_node.yml`,
   `alerts_prometheus.yml`) and renders the container-resource alert rule
   (`alerts_containers.yml`, templated for the ignore-list/thresholds).
5. Renders `alertmanager.yml`, pulls the alertmanager image, renders its
   systemd unit, ensures the service is enabled/running.
6. Renders `prometheus.yml` (the main config — see "Scrape config" below).
7. Renders this host's own per-AZ file_sd fragments (`include_tasks:
   file_sd.yml` — see "Attach mode" below).
8. Pulls the prometheus image, renders its systemd unit, ensures the service
   is enabled/running.

Both services are plain `docker run --rm` foreground processes wrapped in a
systemd unit (see "Containerization style" below), restarted by a handler whenever the rendered unit file,
config, or a fresh image pull actually changes anything. `tasks/file_sd.yml`
is the one exception: it **never notifies a restart** (see next section).

## The per-AZ file_sd contract, and why nothing restarts for it

Prometheus's `file_sd_configs` mechanism watches the files under
`/etc/prometheus/{node_exporter,cadvisor,haproxy}/*.yml` and reloads targets
from them on its own, within one scrape interval, with **no SIGHUP, reload
endpoint, or restart involved**. That is what makes the per-AZ fragment
files usable both here and in attach mode:

* Exactly one node per az (preflight-enforced), so each fragment holds
  exactly one target.
* **Label contract (docs/contracts.md §Prometheus contract):** `region: <az>`,
  `node: <hostname>`, job names `node-exporter` / `cadvisor` / `haproxy`. The
  controller's placement queries match on these labels exactly
  (`node="<hostname>",region="<az>",job=~"node-exporter"`); wrong values
  here silently zero out cpu/mem for that node and reject every order
  against it.
* Ports come from `cs_ports` (`node_exporter` 9100, `cadvisor` 8080,
  `haproxy_stats` 81 — the latter MUST equal `load_balancer.stats_bind`'s
  port, per ports.yml's own note).

### Which nodes get a fragment

`metrics_file_sd_nodes` — **not** `groups['nodes']`, and **not** whatever
`--limit` happens to leave in the play:

```
cs_new_nodes | map('extract', hostvars)
             | selectattr('cs_site', 'equalto', cs_site)
             | map(attribute='inventory_hostname') | sort | list
```

Two filters:

* **This metrics host's own site.** A site has exactly one metrics host
  (preflight-enforced) and a `region` never spans sites, so a node's
  fragments belong on exactly one Prometheus. The fragment carries the
  frozen `region: <az>` label the controller's placement queries match on;
  two Prometheis both answering for the same az is not a duplicate, it is a
  coin flip decided by whichever endpoint that Region row happens to name.
* **Not `cs_existing_hosts`.** Attach mode's inventory carries the v1
  estate's nodes so the seed and the firewall appends can see them
  (contracts rule 9), but it is a *partial* view of them and each fragment
  is a whole-file write. Rendering one would replace a target v1 has been
  scraping for years with whatever the attach inventory says about a host it
  was never written to describe.

**`--limit` does neither job.** It removes hosts from *plays*; it does not
remove them from `groups[]`, and this loop reads `groups`. (`--limit` is still
load bearing for a different reason: see the fact-cache guard below.)

Single-site installs set `site` on nothing, land every host in the site
called `default`, and outside attach mode this resolves to exactly
`groups['nodes']`.

`hostvars[h].cs_site` is the one cross-host-safe read in the site contract
(docs/contracts.md hard rule 10): `cs_site` references no `hostvars` of its
own. The maps around it — `cs_site_metrics_hosts`, `cs_site_metrics_domains`
— must only ever be read on the executing host.

### Tailscale pairwise scrape-address derivation

Per docs/contracts.md's "Tailscale address derivation" (PAIRWISE, never
per-node): the scrape address for a node is its **tailscale IP only if BOTH
this metrics host and that node route control-plane traffic over the
tailnet** (members, neither with `tailscale_access_only: true`); otherwise
`primary_ip`. `tasks/file_sd.yml` computes this per target as:

```
metrics_scrape_address = (hostvars[node].tailscale_ip
                          | default(hostvars[node].ansible_local.computestacks.tailscale_ip
                                    | default(hostvars[node].primary_ip)))
                          if (((tailscale_authkey | default('')) | length > 0)
                              and (tailscale_enabled | default(true))          # this host
                              and not (tailscale_access_only | default(false) | bool)
                              and (hostvars[node].tailscale_enabled | default(true))  # target node
                              and not (hostvars[node].tailscale_access_only | default(false) | bool))
                          else hostvars[node].primary_ip
```

The address VALUE uses the blessed facts expression from docs/contracts.md
("Facts exception: tailscale_ip"), with `primary_ip` as the final fallback
instead of `''`. Both defaults are load bearing: `hostvars[node].tailscale_ip`
is a `set_fact` the `tailscale` role sets only on hosts targeted in the
current run, so under `--limit metrics` — the normal way to re-render
file_sd — it is undefined for every node. Without the middle term, every
tailnet scrape target would silently rewrite to an unroutable `primary_ip`
and the whole fleet's metrics would go stale. The middle term reads the
value the `tailscale` role persisted into
`/etc/ansible/facts.d/computestacks.fact`.

That value reaches a `--limit`ed run only through the repo's persistent fact
cache (`ansible.cfg`: `fact_caching = ansible.builtin.jsonfile`,
`.ansible_facts_cache/`, never expiring). A host outside the `--limit` is not
contacted, so its `ansible_local` comes from the cache or from nowhere.
**Guard: the first-ever run from a fresh operator clone must be un-limited.**
The cache is gitignored, so a fresh clone starts empty; a `--limit metrics`
render before any full converge sees no `ansible_local` for any node and
rewrites every tailnet scrape target to `primary_ip`. One un-limited
`site.yml` populates the cache, and every `--limit` after that is safe.

Membership stays a pure-inventory predicate — only the address value uses
the facts exception. A remote region with tailscale disabled is scraped over
its public/LAN route in cleartext — that's the operator's explicit,
documented choice, not a bug.

### Attach mode (`existing_env: true`)

`tasks/file_sd.yml` is self-contained for exactly this reason:

```yaml
- name: Render this new region's prometheus file_sd fragments
  ansible.builtin.include_role:
    name: metrics
    tasks_from: file_sd
```

`add-region.yml` runs this against an **existing** metrics host (v1- or
v2-built — both read the same paths). Each
fragment is a whole-file write keyed by az — the per-AZ-fragment exception
docs/contracts.md's attach-mode rule names explicitly — so the set of azs the
loop visits *is* the write set, and the role bounds it itself
("Which nodes get a fragment"): this metrics host's own site, minus every
host flagged `existing_env`. An operator `--limit` narrows the play, never
the loop, and is not what protects the v1 estate's existing fragments.

Nothing else in this role runs on an attach-mode metrics host: no `ops`
network re-creation, no `prometheus.yml` re-render, no service management.
Fragment content is intentionally plain (no exotic relabeling) so it is
byte-compatible with what v1's own `roles/prometheus` already writes and
reads.

## Scrape config (`prometheus.yml`)

No target is `[0]`-indexed (docs/contracts.md rule 2); the jobs are:

* `job_name: node-exporter` — file_sd for this site's nodes (per-AZ
  fragments) **plus** a `static_configs` entry generated by iterating
  `metrics_infra_node_exporter_groups` (`controller`, `registry`, `metrics`,
  `backup`, `nameservers` — every group that runs the distro node_exporter
  package but isn't a per-AZ node), one target/labels block per host,
  labeled `region: "<group name>"` / `node: "<hostname>"` so these never
  collide with a real node's `region="<az>"` label. `backup` is included
  to match v1's node-exporter scrape set
  (`roles/prometheus/templates/node_exporter.yml` looped
  `groups['backup_server']`).

  **This static list is deliberately NOT site-scoped** — the one thing in
  the role that isn't. The controller, the nameservers and the registry have
  no `site` and nowhere sensible to put one; they serve the whole estate.
  Scoping them the way the per-AZ loop is scoped would put them in the site
  called `default`, which on a two-site estate is a site no metrics host
  belongs to, and they would be scraped by **nobody** — series just stop,
  with nothing failing. The alternative, two Prometheis each scraping the
  controller, is harmless: duplicate series in two separate TSDBs that
  nothing joins across. Recorded as a decision, not an oversight.
* `job_name: cadvisor` / `job_name: haproxy` — file_sd only. Neither runs on
  `controller`/`registry`/`metrics`/`nameservers`/`backup`, so there is
  nothing to iterate for them.

`rule_files` names `alerts_prometheus.yml`, the file actually installed, so
the prometheus-health alert rules (`PrometheusConfigurationReload`, etc.)
are loaded.

## Exposure via acme_web

Prometheus binds loopback-only (9090, like loki's 3100); `acme_web`'s nginx
serves it on `cs_ports.metrics_prometheus` (3101) with TLS and basic auth —
see [`roles/acme_web/README.md`](../acme_web/README.md). This role writes
neither the nginx vhost nor the htpasswd file. The basic-auth credentials
`acme_web` consumes are the
environment-wide `prometheus_basic_auth_password` / `loki_basic_auth_password`
in the sample secrets (`inventories/example/group_vars/all/secrets.yml`),
overridable per site through `metrics_site_credentials` (see
`roles/acme_web/README.md`); this role reads none of them.

The public name this host's vhosts answer on is likewise per-site —
`cs_site_metrics_domains[cs_site]`, i.e. the `metrics_domain` host var on
this metrics host, falling back to `cs_metrics_domain`. `acme_web` puts that
name on the certificate.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `metrics_ops_network` | `ops` | Sole owner. |
| `metrics_config_dir` | `/etc/prometheus` | v1 path; also bind-mounted into alertmanager as `/etc/alertmanager` (v1 shared one host dir for both configs — kept for fidelity). |
| `metrics_prometheus_internal_port` | `9090` | Loopback-only; see "Exposure via acme_web". Not in `cs_ports` (never crosses a host boundary), same rationale as `vault_listen_port`. |
| `metrics_alertmanager_port` | `9093` | Container-network-internal only (prometheus dials `alertmanager:9093` by container DNS name on `ops`); never host-bound, never in `cs_ports`. |
| `metrics_alert_endpoint` | `https://{{ cs_portal_domain }}/api/system/alert_notifications` | v1 parity. |
| `metrics_alertmanager_tls_skip_verify` | `true` | v1 default — accepts the controller's self-signed cert until it carries a CA-signed one. |
| `metrics_infra_node_exporter_groups` | `[controller, registry, metrics, backup, nameservers]` | Iterated for the node-exporter job's static targets — see above. **Platform-wide on purpose**; do not site-scope it. |
| `metrics_file_sd_jobs` | `[{component: node_exporter, port: cs_ports.node_exporter}, {component: cadvisor, port: cs_ports.cadvisor}, {component: haproxy, port: cs_ports.haproxy_stats}]` | Drives both the per-AZ directory creation and the fragment render loop. |
| `metrics_file_sd_nodes` | this site's `cs_new_nodes`, sorted | The nodes this metrics host renders fragments for — its own site, minus `cs_existing_hosts`. See "Which nodes get a fragment". Overriding it is a way to shoot yourself in the foot with a whole-file write; don't. |
| `metrics_container_ignore_list` | `alertmanager\|consul\|fluentd\|haproxy\|loki\|loki-logs\|cadvisor\|portal\|prometheus\|grafana\|vault-bootstrap\|nginx\|haproxy\|` | **Frozen, ported byte-for-byte from v1** (`roles/prometheus/vars/main.yml`), trailing pipe and duplicate `haproxy` entry included, for fleet compatibility. Do not "clean up." |
| `metrics_container_cpu_limit` / `metrics_container_memory_limit` | `95` / `92` | v1 parity (cpu_limit is defined but, as in v1, not currently referenced by any shipped alert expression). |

Consumed, not owned: `cs_ports.*`, `prometheus_image` / `alertmanager_image`
(`versions.yml`), `cs_portal_domain`, `tailscale_authkey` /
`tailscale_enabled` / `tailscale_access_only` (global secret + per-host
opt-out and access-only mode), `hostname` /
`primary_ip` / `az` on every node (inventory, preflight-asserted), and the
site contract `cs_site` / `cs_new_nodes` (`playbooks/group_vars/all/
sites.yml`, spellings frozen — this role re-derives neither).

## Containerization style

Each container is wrapped in a hand-rolled systemd unit (`docker run --rm`
plus `ExecStartPre` / `ExecStop` kill and rm): an image change re-renders the
unit, and the unit change notifies a restart handler. The `vault` role takes
the other route — `community.docker.docker_container` with `restart_policy:
unless-stopped`, convergent through the module's own idempotence checks with
no systemd unit. Both satisfy docs/contracts.md rule 3.

Container and unit names are `prometheus` and `alertmanager` on v1-built and
v2-built hosts alike, so both match the same alert ignore-list.
