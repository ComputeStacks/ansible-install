# Coming from v1

How this tree differs from the playbooks it replaced, role by role. It is
for operators who know a v1-built environment and want to know what changed
and why. The v1 tree is tagged `v1-final`.

**This is not a migration path.** A v1 environment is not converged onto
these roles: its hosts keep their v1 configuration, and new regions are added
to it with attach mode ([attach-mode.md](attach-mode.md)), which writes to the
existing hosts only additively. `site.yml` builds new environments.

The headline changes are in the [CHANGELOG](../CHANGELOG.md) entry for v2:
Ubuntu 26.04 instead of Debian, no consul, multi-region inventories, a
versioned controller manifest instead of a generated rake task, pinned
versions everywhere, and convergent roles.

Roles with no v1 counterpart — `controller_post_enroll`, `node_storage`,
`node_upgrade`, `preflight`, `postgres_repo`, `registry`, `tailscale`,
`ubuntu_pro` — are new in v2 and are not listed
below unless there is something to compare.


## acme_web (v1: roles/nginx + roles/acme)

- The two v1 roles are merged. Splitting acme.sh from the nginx that serves
  its challenges bought nothing and let the two drift.
- acme.sh is installed from a checksum-verified release tarball into
  per-version directories; the upstream installer never runs and
  self-upgrade is pinned off. v1 ran `acme.sh --upgrade` from cron, which
  replaced the install with git master and made any version pin decorative.
- v1 had eleven near-identical DNS-01 task files under
  `roles/nginx/tasks/dns/`; v2 has one data structure
  (`acme_web_dns_providers`). The per-provider documentation from v1's
  `ACME_VALIDATIONS.md` is in `docs/acme-providers.md`.
- `nginx_image` is a single `repo:tag` pin instead of v1's split
  `nginx_image` / `nginx_image_tag` floating on `stable`.
- The container drops `--privileged` — nothing it does needs it.
- Config mounts are `:ro`.
- `listen … http2` (deprecated since nginx 1.25.1) is replaced by the
  `http2 on;` directive.
- v1's `grafana.conf` vhost is not carried over — no grafana is deployed.
- v1 passed `--force` on an ungated issuance task, re-issuing a perfectly
  good certificate every time it ran. Now the *gate* lives in ansible (SAN-set
  convergence) and `--force` is passed only once that gate has decided
  issuance must happen — acme.sh's "not due yet" skip must not veto it, and
  without it a failed `--install-cert` after a successful `--issue` wedges
  behind acme.sh's renewal window.
- The registry-host reload hook keeps v1's arrangement (one hook refreshes
  nginx's copy and restarts the registry containers) with two bugs fixed: v1
  passed two `--filter ancestor=` values to a single `docker ps` (same-key
  filters are not reliably OR'd, so it could match nothing), and it
  `rm -rf`'d nginx's certificate directory before copying into it.
- v1's `enable_cloudflare_real_ip` cron hook is gone; `ProxyIpList` in the
  controller owns the CDN address lists now.
- The default NSUPDATE TSIG algorithm moves from `hmac-md5` to `hmac-sha256`
  (documented in `docs/acme-providers.md`; set it explicitly to reuse an
  existing v1 key).
- No `ops` docker network membership and no `-p` publishing: the container
  runs on the host network (see "Host networking is load-bearing" in
  `roles/acme_web/README.md`).


## backup_server (v1: roles/backup)

- Upstream GPG-verified borg release instead of `apt install borgbackup`
  (`state: latest`, which floated the version out from under the client image).
- No NFS export path, no `/etc/exports`, no `exportfs`.
- No root SSH key distribution (v1 pushed every node's **root** key into the
  backup server's `root` `authorized_keys` — v2 nodes only ever authenticate as
  `cstacks`, with a dedicated per-node key).
- No borg compaction cron or logrotate for it: compaction is in-agent now.
- Not behind `tags: ['never', 'bootstrap']` — the role is convergent and runs
  in a normal converge.
- On an existing v1 backup server (attach mode) the role writes only
  `~cstacks/.ssh`; nodes attaching to it must set `backup_host_path` (v1:
  `/mnt`) and `backup_borg_remote_path` (v1: `/usr/bin/borg`). See
  `roles/backup_server/README.md` "Attach mode".


## common (v1: roles/base)

- **The `tmux.conf` download from a personal gist is dropped** — the `tmux`
  package is still installed, but no custom dotfile is fetched.
- **Package list curated**, dropping: `linux-headers-amd64` (Debian-specific
  package name, no Ubuntu equivalent needed here), `apt-transport-https`
  (no-op since APT 1.5+), `gnupg-agent` (functionality merged into `gnupg`),
  `pass` (a personal password-store tool, unrelated to server operations),
  and `nfs-common` (v2 has no NFS backend — SSH/borg only). Renamed
  `gnupg2` -> `gnupg` (the current, non-transitional package name).
- **Service names corrected for Debian/Ubuntu**: v1's handlers used `sshd`
  and `chronyd` (RHEL-style unit names); this role uses the Debian/Ubuntu unit
  names `ssh` and `chrony`.
- MOTD is rendered as an executable `/etc/update-motd.d/50-computestacks`
  script (dynamic MOTD), not a static `/etc/motd` template — v1 wrote
  `/etc/motd` directly. Static `/etc/motd` is now emptied instead.
- v1's `/etc/hosts` localhost line is not carried over. Its DNS checks live
  in `roles/preflight` and `roles/validate` instead, and the admin
  credentials are asserted by `roles/controller_seed`.
- **v1's unconditional post-docker-install reboot is replaced** (v1 rebooted
  every host, unconditionally, right after installing docker) by a targeted,
  convergent check right after package install: reboot only when
  `/var/run/reboot-required` exists, never on `existing_env` hosts and never
  on a live node. See "Reboot right after package install" in
  `roles/common/README.md` for the root cause.


## controller (v1: roles/controller + roles/controller_postgres)

- **`SECRET_KEY_BASE` / `USER_AUTH_SECRET` are never generated.** v1 ran
  `openssl rand -hex 64` when they were blank, which silently produced a
  different key on a re-run against a wiped `/etc/default/computestacks` and
  destroyed every encrypted column. Both are asserted inputs now
  (docs/contracts.md rule 8).
- **The wildcard certificates are generate-once, per domain.** v1
  regenerated its single certificate on every run; under v2's convergent rule
  that would rotate the load balancer shared certificate fleet-wide on every
  converge. See "Per-az wildcard certificates" in
  `roles/controller/README.md`.
- **`bootstrap-app` is gone.** v1's generated `bootstrap.rake` embedded ruby
  in a Jinja template and drifted from the controller's models. Seeding is now
  `cstacks seed` → `rake bootstrap:apply[<manifest>]` against a versioned
  schema (`roles/controller_seed`).
- **The application ssh keypair moved** from `/var/lib/computestacks/sshkeys`
  to `/etc/computestacks/.ssh`, where `roles/ssh_trust` reads it. Before
  installing the v2 `cstacks` script on a v1 controller, attach mode asserts
  that every path the script will bind-mount exists as the script would
  resolve it — so an environment file that omits `CS_SSH_KEYS_PATH` fails the
  run instead of mounting an empty directory over the real keypair.
- **`/var/lib/computestacks/proxy_ips` is new**: `ProxyIpList`'s persistent
  store.
- **No consul mount, no `/root/.consul` directory, no consul token slurp.**
- **The branding CDN downloads are gone.** v1 fetched `application.css`, its
  map and a login image from the v1 CDN on every bootstrap — unpinned
  third-party content pulled into the portal's asset path. The same files are
  copied out of the pinned controller image instead (see "Branding" in
  `roles/controller/README.md`).
- **`/computestacks-mnt` is not created on the controller.** v1 created the
  registry data root there, where nothing uses it; `roles/registry` creates it
  on the registry host, where the registry containers bind-mount it.
- **No `tags: ['never', 'bootstrap', 'addnode']` gating.** The role is
  convergent by default; attach mode is a separate task file rather than a tag.
- **`cstacks` is `set -euo pipefail` and shellcheck-clean**, builds its docker
  arguments once (v1 repeated the whole flag list in seven places and they had
  drifted), and gained `stop`, `seed`, `runner` and `migrate`. `console` and
  `test` no longer force `-it` when there is no terminal.
- The cloudflare real-IP cron script is not carried over: `ProxyIpList` in the
  controller (9.7) owns the Cloudflare and Bunny address lists, refreshes them
  itself, and persists them in the `proxy_ips` mount.
- `controller_sentry_dsn` defaults to empty (bug reporting off); v1 defaulted
  to ComputeStacks' own DSN.


## controller_seed (v1: the generated bootstrap.rake in roles/controller)

- Seeding is a rendered, schema-validated manifest applied with
  `cstacks seed` (`rake bootstrap:apply`), replacing v1's generated
  `bootstrap.rake` — embedded ruby inside a Jinja template that drifted from
  the controller's models across releases.
- `cr_le` is `cs_registry_domain`. v1's `bootstrap.rake` wrote
  `cs_portal_domain` there, a copy/paste bug.
- The load balancer domain is per az (`app_domain` host var, falling back to
  `cs_app_zone`), and each az's load balancer gets its own wildcard
  certificate. v1 had one `cs_app_url` for every load balancer's domain and
  the certificate's CN.
- `consul_token` is no longer set (dead column since the v3 agent cutover).
- Behavior kept from v1: the PowerDNS webserver password goes in `api_key`
  and the X-API-Key in `api_secret`; the PowerDNS API call is plain HTTP to
  the leader's `primary_ip`; `controller_seed_pid_limit` defaults to v1's 300.


## cs_agent (v1: roles/cs_agent)

- Native deb under systemd instead of a container plus a hand-written unit;
  no image pull, no `cs-agent.service.j2`.
- Not guarded by `when: ansible_facts.services[...] is not defined` — the role
  is convergent, so a version or config change actually applies on a rerun.
- Consul is gone (no `consul:` block, no consul/docker cert plumbing here).
- Every NFS key is gone.
- The borg key moved from `/etc/computestacks/.ssh/` to
  `/etc/computestacks/backup/.ssh/`, matching the agent's own default.
- `agent.yml`: `backups.borg.compress` -> `backups.borg.compression`;
  `mariadb` hoisted to the top level (the schema is cs-agent's
  `config/config.go`, not the upstream `agent.sample.yml`).
- Attaching to a v1 backup server needs `backup_host_path` (v1: `/mnt`) and
  `backup_borg_remote_path` (v1: `/usr/bin/borg`) set, plus `backup_ssh_port`
  when that server's sshd is not on `cs_ports.ssh`; `backups_key` must be the
  existing environment's value.


## docker_config (v1: roles/docker)

- `daemon.json` keeps v1's `live-restore: true`; `shutdown-timeout` drops
  from v1's 120 to 60. 120 could never be honored: docker's packaged unit sets
  no `TimeoutStopSec`, so systemd's `DefaultTimeoutStopSec` of 90s kills
  dockerd first.
- Registry mirrors move from v1's `--registry-mirror` ExecStart flag into
  `daemon.json` (`docker_config_registry_mirrors`), which is reloadable.
- **v1's post-install reboot is not carried over.** v1's
  `roles/docker/tasks/install.yml:63-82` rebooted every host right after
  installing docker, on this comment:

  > 2023-09: Docker is currently failing to start due to iptable/nftable issues.
  > A reboot seems to resolve this issue.

  It was added in commit `dd44c4e` ("Move our node agent into a container.
  Docker bugfix on debian 12") — a Debian 12 / bookworm-era workaround. The
  plausible mechanism is not docker-specific: the installer's earlier full apt
  upgrade can install a new kernel and remove the running kernel's
  `/lib/modules/$(uname -r)` tree, after which `modprobe` of `br_netfilter` /
  `nf_nat` / `ip_tables` fails and dockerd aborts while setting up its
  iptables chains. (The other candidate — iptables-nft alternative switching —
  does not normally need a reboot.) An unconditional reboot is a blunt
  instrument and fatal in attach mode (rebooting an existing production
  controller or metrics host), so v2 handles the root cause in
  `roles/common`'s reboot-if-required check instead, before docker is
  installed and never on `existing_env` hosts or live nodes.


## docker_tls (v1: roles/docker)

- Convergent re-issue (missing file, within 30 days of expiry, or SAN set
  changed) instead of v1's issue-once-forever (`when: not key_exists`) — a
  5-year certificate nobody watched.
- TTL 1 year, clamped to the CA's remaining lifetime (v1: 43800h, unclamped).
- Binds `primary_ip:2376`, not `0.0.0.0:2376`.
- SANs come from the inventory (`hostname`, `primary_ip`); v1 used
  `ansible_hostname` (a gathered fact — banned by contracts rule 1) and a
  `consul_listen_ip` var that defaulted to `127.0.0.1`, which quietly produced
  certificates with no usable IP SAN unless the inventory overrode it.
- Restart via handler (v1 stopped docker, wrote the drop-in, then started it —
  on every single run).
- File modes: dir 0750, `server.crt`/`ca.crt` 0640, `server.key` 0600 (v1: dir
  0740, everything 0440).
- `--registry-mirror` moved out of ExecStart into `daemon.json`
  (`docker_config_registry_mirrors`).
- `docker exec` without `-it`, so v1's `ansible_ssh_pipelining: no` workaround
  is gone.
- Kept from v1: the certificate directory `/etc/docker/certs` and its file
  names, the `startup.conf` drop-in filename (so a host can never carry two
  ExecStart drop-ins), the `--icc=false --userland-proxy=false` hardening
  flags, and the vestigial `dockremap` user (uid 1001;
  `docker_tls_create_dockremap_user: false` turns it off).


## firewall (v1: roles/iptables)

- One nftables-native table (`inet cs_static`) applied by a role-owned oneshot
  unit, instead of a generated `iptables` shell script re-run by a handler
  (which appended duplicate rules on every converge).
- No `expose-ports` / `container-inbound` chains: cs-agent renders its own
  native table and no longer needs the provisioner to pre-create them.
- IPv6 traffic is dropped by policy rather than `REJECT`-with-log; enable
  `firewall_log_dropped` for the log half.
- Container traffic to the host is limited to the agent port
  (`firewall_container_ports`); v1 accepted all TCP from `container_network`.
- All source addresses come from `primary_ip` / `public_ip`; v1's per-role
  address variables (`consul_listen_ip`, `metrics_ip_address`, `node_ip`, …)
  are gone along with consul.
- `extra_allowed_ipv4_addresses` / `_ipv6_addresses` are now
  `firewall_extra_allowed_ipv4` / `_ipv6`.
- Attach mode on a v1-built host appends to v1's `cs-recover_iptables` script
  with v1's own `lineinfile` idiom (anchored on the whole rule) and makes the
  rules live with `-C` / `-A`, rather than re-running the script.


## haproxy (v1: roles/haproxy)

- **Adds `/etc/haproxy/dhparam.pem`.** The controller's generated config
  carries `ssl-dh-param-file /etc/haproxy/dhparam.pem` unconditionally and
  haproxy refuses to start when the file is missing; nothing in v1 created it.
- **Adds `/etc/haproxy/errors/` and backfills missing error pages** with the
  generic 503 page (`force: false`). v1 installed only `default.http`, leaving
  the six other `errorfile` paths dependent on the distro package.
- **Holds the package** (v1 let unattended upgrades move it).
- **Enabled, not started** (v1 used `state: started`, which fails on a fresh
  node whose stock config has no listener).
- Not guarded by `when: ansible_facts.services[...] is not defined` — the role
  is convergent.
- `certs/` is 0700 (v1 left it at the default 0755 with private keys in it).


## loki (v1: roles/loki)

A direct port: same unit name (`loki.service`), container, paths and image
(`grafana/loki:2.9.10`); the static `loki-config.yml` is the same
configuration (only the YAML list indentation differs).

* v1's per-tag task gating (`tags: ['never', 'bootstrap', 'addnode']`,
  `when: ansible_facts.services['loki.service'] is not defined`) is gone — the
  role is convergent (docs/contracts.md rule 3): re-running reconciles
  config, image and unit state, and handlers fire only on actual change.
* v1 created the `ops` network itself (`community.general.docker_network` in
  `roles/loki/tasks/install.yml`) — one of two places v1 created it (the other
  being `roles/nginx/tasks/preflight.yml`). This role does not; `metrics` is
  the sole owner (docs/contracts.md §Ownership).
* `loki_image` is a single `repo:tag` pin, replacing v1's split
  `loki_image` / `loki_image_tag` variables — value unchanged (`2.9.10`).
* Uses `community.docker.docker_image_pull` / `docker_volume` (pinned
  collection) in place of v1's `community.general.docker_image` /
  `docker_volume`.


## metrics (v1: roles/prometheus + roles/alertmanager)

* **The `[0]`-indexed static_configs block is gone.** v1's `prometheus.yml.j2`
  had a `job_name: cadvisor` static_configs block hard-indexing
  `hostvars[groups['controller'][0]]` — a single, nonsensical extra cadvisor
  target on the controller (which never runs cadvisor). v2 iterates groups
  instead (docs/contracts.md rule 2).
* **The `rule_files` typo is corrected.** v1 referenced `alerts_prom.yml`, but
  the file installed (in v1 and v2) is `alerts_prometheus.yml`, so v1's
  prometheus-health alert rules (`PrometheusConfigurationReload`, etc.) were
  never loaded.
* Every per-node file_sd target comes from a **loop over this site's new
  nodes** (`metrics_file_sd_nodes`), one fragment per node and component,
  instead of v1's single "dump the whole fleet into one file named after
  whichever host runs the template" pattern — which only worked in a
  single-region install and could not extend cleanly in attach mode. The
  fragment paths and format are v1's, so v1's prometheus reads them unchanged.
* `prometheus_image` / `alertmanager_image` are single `repo:tag` pins in
  `versions.yml`, replacing v1's split `_image` / `_image_tag` variables.
* Prometheus moves to the **v3.13 LTS line** (`v3.13.2`) rather than v2.53 —
  see the comment in `versions.yml` for the rationale (the v2.53 LTS line and
  its successor 3.5 LTS are both past their support windows). Alertmanager
  moves to the latest stable `v0.34.0` (it has no separate LTS line). Neither
  change alters what the role's templates generate.
* v1's per-tag task gating (`tags: ['never', 'bootstrap', 'addnode']`,
  `when: ansible_facts.services[...] is not defined`) is gone — the role is
  convergent (docs/contracts.md rule 3).
* Container and systemd unit names are unchanged (`prometheus`,
  `alertmanager`), so v1-built and v2-built hosts match the same alert
  ignore-list. The systemd-wrapped `docker run --rm` pattern is v1's.


## node_exporter (v1: roles/node_exporter)

v1's role was a two-task `apt: state=latest` + `systemd` pair; v2 keeps the
distro package and adds a version pin and a hold.

* `state: present` at the `versions.yml` pin, plus a hold, instead of
  `state: latest` unheld, which let an unattended upgrade swap the exporter
  under a live scrape.
* The hold is released before the install, so the role stays convergent when
  the pinned version moves (docs/contracts.md rule 3).


## node_kernel (v1: roles/node)

- **v1's two conflicting inotify overrides are resolved to one.** v1's
  `kernel_params.yml` set `fs.inotify.max_user_instances` /
  `max_user_watches` to `8388608` / `16777216` in its main "high container
  density" block, then later overrode the same keys to the much smaller
  `1024` / `1048576` in a second block also commented "for high container
  density" — the opposite of what the comment claims, and most likely a
  leftover duplicate edit. v2 keeps only the larger values
  (`8388608` / `16777216`).
- v1 wrote every sysctl without a `sysctl_file` to `/etc/sysctl.conf`
  directly. v2 consolidates all of them into files under `/etc/sysctl.d/`
  (`60-cs-node.conf`, plus `61-cs-node-ipv6.conf` when applicable).
- **BBR is new** (`net.core.default_qdisc=fq`,
  `net.ipv4.tcp_congestion_control=bbr`).
- **IPv6 handling changed.** v1 shipped a static `/etc/sysctl.d/50-ipv6.conf`
  that disabled IPv6 entirely (`net.ipv6.conf.{all,default}.disable_ipv6 = 1`);
  v2 does not port it (enabling or disabling IPv6 is a network decision made
  elsewhere). Instead it adds the `accept_ra = 2` uplink fix, gated on the node
  having a global IPv6 address and an explicit `uplink_interface`.
- v1's `preload_images.yml` (container image preloads) moved to
  `node_observability`; no docker-related tuning lives in this role.
- Module loading and the sysctl reload are handler-driven (fire only when the
  owned file changes) rather than v1's always-run `modprobe` /
  immediate-apply tasks.


## node_observability (v1: roles/cadvisor, roles/fluentd, roles/node — preload_images.yml)

- **Restart only on change.** v1 restarted cadvisor on every converge and
  fluentd on every converge after the first, dropping buffered log lines each
  run. v2 bakes the pinned image tag into each unit's `ExecStart`, so only a
  pin bump re-renders the unit and restarts the container.
- **Image preloads pulled by pinned variable.** v1's `preload_images.yml` had
  the two xtrabackup task names swapped against their tags ("Pull image
  xtrabackup 8" pulled `2.4`, and vice versa). Both were pulled, so nothing
  broke, but the intent was unreadable.
- **cadvisor's address and port are explicit** (`-listen_ip`,
  `-port={{ cs_ports.cadvisor }}`); v1 relied on cadvisor's compiled-in
  defaults matching what prometheus scraped.
- The container names (`cadvisor`, `fluentd`) and host networking are
  unchanged from the existing fleet, so v1's `container_ignore_list` still
  matches.


## postgres_repo (v1: none)

v1 had no counterpart: it installed whatever postgres the distro shipped,
because on 22.04/24.04 that happened to be the major the controller wanted. On
26.04 it is not, and the mismatch is silent right up to the point where the
controller's migrations run against a cluster with a schema it does not
support. v2 adds the PGDG repository and pins the major
(`postgresql_version`) explicitly.


## powerdns (v1: roles/powerdns)

* **Credentials are sourced from the vaulted inventory, not generated** — the
  biggest behavioral change. v1 generated the API key, webserver key and db
  password on the leader on first converge, wrote them under
  `/root/.credentials/`, and every later converge (and the follower) `slurp`ed
  them back from that leader-local directory. v2 requires `pdns_api_key`,
  `pdns_web_key` and `pdns_db_password` in the vaulted secrets file; there is
  no `tasks/passwords.yml` equivalent, no `/root/.credentials`, and no
  first-converge race between leader and follower.
* **Package source**: the distro archive. v1 shipped `files/package-pin` (an
  apt preference pinning `pdns-*` to `origin repo.powerdns.com`) that no task
  ever copied anywhere; it is gone. PostgreSQL comes from PGDG
  (`roles/postgres_repo`).
* **No install-state guards**: v1's `tasks/main.yml` branched on
  `ansible_facts.services['postgresql.service']`/`['pdns.service']` before
  even including its install tasks, and had a separate `apt: state=latest`
  task gated on the same facts. v2 converges every run; the only one-time gate
  is the follower's `pg_basebackup` bootstrap.
* **Multiple followers**: v1 hardcoded `groups['follower_nameservers'][0]` in
  two places (the leader's pg_hba source and the follower's replication slot
  name `ns2`). v2 loops `groups['ns_followers']` and derives each slot name
  from `inventory_hostname`. The groups are now `ns_primary` / `ns_followers` (v1: `primary_nameserver`
  / `follower_nameservers`).
* **`until`/`retries` actually retries**: v1's `postgresql_ping` checks set
  `retries`/`delay` without `until`, so they only ever ran once.
* **Config files are 0640 `root:pdns`**: v1's `pdns.conf` and
  `pdns.local.gpgsql.conf` used the `template` module's default mode, leaving
  the API key, webserver password and db password world-readable.
* **`webserver-allow-from` is the controller only**: v1 opened the PowerDNS API
  to `0.0.0.0/0` and relied entirely on the host firewall. v2 restricts it to
  the controller's inventory addresses and binds `webserver-address` to the
  leader's `primary_ip` rather than `0.0.0.0`.
* **One unified `pdns.conf.j2`** instead of v1's two near-duplicate templates
  (`pdns.conf.j2` / `pdns-follower.conf.j2`).
* **Unchanged from v1**: the dual-stack DNS listener (`"0.0.0.0, ::"`),
  `trust`-by-source-IP replication auth, dnsupdate enabled, and the inline
  postgres install rather than `geerlingguy.postgresql`.
* **Var prefix**: `pdns_*` → `powerdns_*` for every role-defined variable. The
  three vaulted secrets keep their `pdns_` names.
* **systemd-resolved**: v1 disabled the service outright. v2 turns off only its
  stub listener and keeps resolved running, because on Ubuntu the stub is the
  host's resolver.
* **Load balancer records** (`tasks/lb_records.yml`) are new in v2. In attach
  mode this task file runs against the existing v1 nameserver — possibly a
  Debian box on PowerDNS 4.x — and adapts its `pdnsutil` name style to the
  installed major version.


## registry (v1: none)

There is no v1 counterpart — v1 left the registry host's contract unwritten.
v2's role is deliberately thin: the controller still provisions registries
itself over SSH (DockerSSH); the role only prepares `/computestacks-mnt` and
`/opt/container_registry/ssl` and checks that a controller exists.


## ssh_trust (v1: roles/ssh_transfer)

- The rendered `/root/.ssh/config` on the controller includes a `Host` block
  **per node only**. v1 also added `Host` blocks for `backup_server`,
  `metrics`, `registry` and both nameserver groups; nothing in v2 depends on
  them, and the template can be extended if a documented need arises.
- Keys are pushed to `groups['nodes']` and `groups['registry']` only. v1 ran
  `ssh_transfer` against `hosts: all`.
- No "verify controller can connect" smoke-test task in the role — that check
  is `roles/validate`'s `ssh` check.
- The application key path is probed on the controller (v2's
  `/etc/computestacks/.ssh/` first, then v1's `ssh_keys_directory`,
  `/var/lib/computestacks/sshkeys/`), so attach mode against a v1 controller
  installs the key its application actually uses.


## tailscale (v1: none)

v1 had no tailnet. v1's `local_dns` role has no v2 counterpart: the control
plane dials addresses (per-az `acme_server` addresses in the manifest) rather
than names, and the tailnet is joined with `--accept-dns=false` so MagicDNS
never rewrites a host's resolver. Attach mode defaults tailscale off, because
existing v1 hosts are not on a tailnet unless the operator joined them first.


## validate (v1: roles/validate)

* v1 checked only that a list of systemd units was running, per group, and
  called `cstacks test` on the controller. It never checked a single
  cross-host leg — not the agent, not the prometheus label contract, not
  borg, not the portal from a node — which is exactly where a ComputeStacks
  install goes wrong silently. v2 asserts each of those legs from the host
  that uses it.
* v1 asserted `cadvisor` on the controller, metrics and registry hosts, none
  of which run it in v2 (nor ran it usefully in v1).
* v1's consul check is gone with consul.
* `cstacks test` (`rake test_connection:all`) is not called by validate:
  `controller_post_enroll` already runs it, and it exits 0 even when a leg
  fails, so it is a report rather than an assertion. The legs it reports on
  are asserted individually.
* In attach mode, the `services` check asserts nothing on an `existing_env`
  host: a v1-built controller on Debian may run postgres, nginx or
  node_exporter under different unit names or in containers.


## vault (v1: roles/vault)

A slimmed-down version of v1's role. The on-disk layout under
`/etc/computestacks/.vault-bootstrap/` and the container name
`vault-bootstrap` are unchanged, which is what lets attach mode unseal and
issue from an existing v1 controller's vault.

* No `pki/consul` mount and no consul certificates — consul is gone from the
  stack.
* Leaf TTL 8760h (1 year) instead of 43800h (5 years), and clamped to the CA's
  remaining lifetime.
* Unseals whenever vault reports sealed, on every converge. v1 only unsealed on
  the run that happened to see a non-zero `vault status`, and its issuance
  tasks would then fail opaquely.
* Re-issues the controller client certificate inside the renewal window
  (`vault_cert_renew_days`); `roles/controller` recreates the portal container
  when the certificate changes.
* `docker exec` without `-it` (no TTY needed), so v1's
  `ansible_ssh_pipelining: no` workaround is gone.
* Key files are mode 0600 (v1 left them at the copy default).
* `client.pem` (cert+ca+key concatenation) is still written for v1 parity.

