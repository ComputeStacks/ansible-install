# acme_web

**Owner: Wave 3G.** `hosts: controller`, `metrics`, `registry`.

## Purpose

The TLS edge for every host that terminates HTTPS on the ComputeStacks control
plane: a pinned nginx container plus [acme.sh](https://github.com/acmesh-official/acme.sh)
pinned to a release tag. One role, three vhost sets, selected by group
membership.

| Group | Listens on | Proxies to |
| --- | --- | --- |
| `controller` | `cs_ports.controller_https` (443), `cs_ports.controller_http` (80) | `127.0.0.1:{{ cs_ports.controller_acme_backend }}` — the portal container's own nginx |
| `metrics` | `cs_ports.metrics_prometheus` (3101), `cs_ports.metrics_loki` (3102) | `127.0.0.1:9090` (prometheus), `127.0.0.1:3100` (loki), both behind basic auth |
| `registry` | 80/443 | nothing — its job here is the certificate, which the registry containers mount |

The 3101/3102 interface is exactly the one `roles/metrics/README.md`
documents: prometheus and loki publish loopback-only on this host and this
role is the only way in.

## Host networking is load-bearing

The container runs `--network=host` on **every** host, with **no `-p` /
`--publish`**, and that is a firewall-enforceability requirement, not a
preference:

> A published port is DNAT'd in `PREROUTING` and never traverses the nftables
> input chain. Publishing 443/3101/3102 would turn the accepts in
> `roles/firewall`'s `cs_static` table into intent rather than enforcement.

`roles/firewall/README.md` §"What an input chain can and cannot enforce"
flagged this as pending on Wave 3G; this is the resolution. Host networking is
also what lets these vhosts reach their `127.0.0.1` upstreams — a container on
a user-defined bridge cannot. The metrics-host nginx therefore does **not**
join the `ops` docker network.

## What it does, in order (`tasks/main.yml`)

1. Asserts `nginx_image` is an exact pin and `acme_sh_version` is a release
   tag; asserts this host has a certificate domain; asserts the metrics
   basic-auth passwords are set.
2. Installs `python3-passlib`, `rsync`, `git`; creates the nginx tree.
3. Installs `nginx.conf`, the error pages, the dhparam group and the ACME
   challenge snippet.
4. Pulls the image and renders the systemd unit.
5. `tasks/acme_install.yml` — acme.sh at the pinned tag.
6. `tasks/acme_cert.yml` — issue, install, arm the renewal timer.
7. `tasks/vhosts.yml` — the real vhosts and the htpasswd files.
8. Ensures the service is enabled and running.

### acme.sh (`tasks/acme_install.yml`)

Cloned at `version: "{{ acme_sh_version }}"`, installed with `--nocron
--noprofile --home /opt/acme --config-home /opt/acme/data`, then
**`--auto-upgrade 0`**. v1 cloned the default branch and then ran `--upgrade
--auto-upgrade`, which let acme.sh replace itself with HEAD from cron and made
any pin decorative.

Convergence is by installed version: the role reads `acme.sh --version` and
re-installs only when `acme_sh_version` is not in the output, so a bump in
`versions.yml` rolls on the next run and an unchanged pin is a no-op.

Renewal is a role-owned `acme_renew.timer` (daily, 1h jitter, `Persistent`),
not acme.sh's cron. `SuccessExitStatus=0 2` — acme.sh exits 2 when nothing was
due.

### Issuance and the bootstrap vhost (`tasks/acme_cert.yml`)

Every real vhost references a certificate that does not exist on a fresh host,
and nginx refuses to start without it. So when `fullchain.pem` is absent the
role writes a plain `:80` vhost that serves nothing but the challenge path,
restarts nginx, confirms it is actually active, and only then issues. Once the
certificate exists, `vhosts.yml` overwrites that file with the real
`default.conf`.

Issuance is gated on the certificate being absent — renewal is the timer's
job, not a converge's. `--install-cert` is likewise gated on the target files
being absent (or `acme_web_force_install_cert: true`), because on the registry
host running it restarts every registry container.

### DNS-01

`acme_web_dns_providers` maps `acme_challenge_method` to an acme.sh `--dns`
plugin and the environment it reads, in one data structure. v1 had eleven
near-identical task files under `roles/nginx/tasks/dns/`; adding a provider is
now a defaults edit. The issuance task is `no_log`. Full per-provider
documentation, ported from v1's `ACME_VALIDATIONS.md`, is in
`docs/acme-providers.md`.

Operator-facing variable names are unchanged from v1 (`acme_challenge_method`,
`acme_cf_token`, …); the role reads each through an `acme_web_`-prefixed
variable so both the documented vocabulary and the role-prefix lint rule hold.

### Port 80 on the metrics and registry hosts

`roles/firewall` opens `cs_ports.controller_http` on the controller, the
metrics host and the registry host, so the default HTTP-01 challenge works
for `cs_portal_domain`, `cs_metrics_domain` and `cs_registry_domain` alike.
A host that cannot expose 80 to the internet at all needs a DNS-01 provider
(`docs/acme-providers.md`).

### htpasswd file permissions

The htpasswd directory is `0755` and the files inside it `0644`, matching v1.
nginx's worker processes drop to `user nginx` (uid 101) inside the container
and read the files at request time; `0750`/`0640 root:root` makes every
authenticated 3101/3102 request fail. The files hold bcrypt/md5-crypt hashes,
not plaintext, and the metrics host has no untrusted local users.

## The registry certificate, and why one hook does two jobs

acme.sh stores **one** `--install-cert` configuration per certificate. On the
registry host the role installs twice — for nginx, then for
`/opt/container_registry/ssl` — so the registry install is the one renewal
fires, and its `--reloadcmd` has to cover both. `post_acme_reload`
therefore rsyncs the renewed files into nginx's directory, reloads nginx, and
then restarts every running registry container (they read their certificate
once, at start). This is v1's arrangement, with two bugs fixed: v1 passed two
`--filter ancestor=` values to a single `docker ps` (same-key filters are not
reliably OR'd, so it could match nothing), and it `rm -rf`'d nginx's
certificate directory before copying into it.

`/opt/container_registry/ssl` is created here because `site.yml` runs
`acme_web` before `registry` and `--install-cert` needs its target to exist.
`roles/registry` owns the tree.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `nginx_image` | `nginx:1.30.4` | `versions.yml`; asserted to be an exact pin. |
| `acme_sh_version` | `3.1.4` | `versions.yml`; cloned verbatim as a git tag (the 3.x line has **no** leading `v`). |
| `use_zerossl` | `true` | Inventory name. ZeroSSL is the default CA; `false` selects Let's Encrypt. |
| `acme_account_email` | `{{ cs_admin_email }}` | Inventory name. |
| `acme_challenge_method` | `http` | Inventory name. See `docs/acme-providers.md`. |
| `acme_web_domains` | derived from `group_names` | `cs_portal_domain` / `cs_metrics_domain` / `cs_registry_domain`. A host in several groups gets one certificate covering every name. |
| `acme_web_cert_name` | first of the above | acme.sh's identifier for `--install-cert` and renewal. |
| `acme_web_force_install_cert` | `false` | Re-run `--install-cert` even when the targets exist — needed after changing a `--reloadcmd`. Off by default because the hook restarts registry containers. |
| `acme_web_prometheus_upstream` / `acme_web_loki_upstream` | `127.0.0.1:9090` / `127.0.0.1:3100` | Must match what `roles/metrics` and `roles/loki` publish. |
| `acme_web_prometheus_password` / `_loki_password` | `{{ prometheus_basic_auth_password }}` / `{{ loki_basic_auth_password }}` | From the vaulted secrets. Asserted non-empty on the metrics host — they are the only thing gating 3101/3102. |
| `acme_web_registry_images` | `cmptstks/registry:latest`, `registry:2` | Which running containers the reload hook restarts. Deliberately not pinned: the controller hard-codes the image when it creates a tenant registry. |

Consumed, not owned: `cs_ports.*`, `cs_portal_domain`, `cs_metrics_domain`,
`cs_registry_domain`, `cs_admin_email`.

## Handlers

* **`Reload nginx`** — vhosts, snippets, dhparam. `ExecReload` sends `SIGHUP`
  to the container.
* **`Restart nginx`** — the unit file, `nginx.conf` and the image, all of
  which are fixed at container creation.

## Deviations from v1 (`roles/nginx` + `roles/acme`)

* The two v1 roles are merged. Splitting acme.sh from the nginx that serves
  its challenges bought nothing and let the two drift.
* `--auto-upgrade 0` and a pinned clone (see above).
* `nginx_image` is a single `repo:tag` pin instead of v1's split
  `nginx_image` / `nginx_image_tag` floating on `stable`.
* The container drops `--privileged` — nothing it does needs it.
* Config mounts are `:ro`.
* `listen … http2` (deprecated since nginx 1.25.1) is replaced by the
  `http2 on;` directive.
* v1's `grafana.conf` vhost is not ported — no grafana is deployed.
* Certificate issuance no longer passes `--force`, which re-issued a
  perfectly good certificate every time the task ran.
* v1's `enable_cloudflare_real_ip` cron hook is gone; `ProxyIpList` in the
  controller owns the CDN address lists now.
* The default NSUPDATE TSIG algorithm moves from `hmac-md5` to `hmac-sha256`
  (documented in `docs/acme-providers.md`; set it explicitly to reuse an
  existing v1 key).
* No `ops` docker network membership, no `-p` publishing — see "Host
  networking is load-bearing".

## Requirements

`community.docker` (image pull), `community.general` (`htpasswd`), both
pinned in `requirements.yml`. Runs after `geerlingguy.docker` +
`docker_config`, and before `metrics`/`loki`/`registry` on their hosts
(`playbooks/site.yml`).
