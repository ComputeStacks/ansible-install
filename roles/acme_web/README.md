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
   tag with its tarball checksum beside it; asserts this host has a
   certificate domain; asserts this site's metrics basic-auth credentials
   (both usernames and both passwords) are set.
2. Installs `python3-passlib` and `rsync`; creates the nginx tree.
3. Installs `nginx.conf`, the error pages, the dhparam group and the ACME
   challenge snippet.
4. Pulls the image and renders the systemd unit.
5. `tasks/acme_install.yml` — acme.sh from the pinned, checksummed tarball.
6. `tasks/acme_cert.yml` — issue, install, arm the renewal timer.
7. `tasks/vhosts.yml` — the real vhosts and the htpasswd files.
8. Ensures the service is enabled and running.

### acme.sh (`tasks/acme_install.yml`)

Installed from the pinned release tarball, sha256-verified against
`acme_sh_tarball_sha256` (`versions.yml`), into a per-version directory:

```
/opt/acme/acme.sh-<version>/   the release tree
/opt/acme/current              symlink to the version in use
/opt/acme/data/                state: account key, DNS creds, certs (0700)
```

The upstream installer **never runs** — its only jobs are a cron entry,
shell-profile edits and the self-upgrade machinery, all unwanted here. There
is no git clone (tags are mutable upstream; the checksum is not) and no
`--upgrade` invocation ever (in acme.sh that is the *upgrade command*: it
contacts the GitHub API and replaces the install with git master — v1 ran it
from cron, which made any pin decorative). `AUTO_UPGRADE="0"` is additionally
pinned in `account.conf`, because the renewal timer runs `--cron`, which
self-upgrades whenever that value is `1`.

Convergence is by directory: a `versions.yml` bump downloads and unpacks the
new release, repoints `current`, and removes superseded version directories.
An unchanged pin is a no-op with no network access.

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

Issuance converges on the **SAN set**: the installed certificate's names are
read with `community.crypto.x509_certificate_info`, executed on the ansible
control machine (`delegate_to: localhost`, needs `cryptography` there — the
fleet carries no python crypto libraries), normalized to lowercase, and
compared to `acme_web_domains`, so a host that joins or leaves the
metrics/registry groups, or a changed domain var, re-issues on the next run —
against the *running* nginx, whose every vhost serves the challenge path, so
the bootstrap vhost is not involved. Certificate **expiry** is deliberately
not a converge trigger: renewal is the timer's job, and a playbook run must
not race it. A CA switch (`letsencrypt_test` → `letsencrypt`) is invisible to
the SAN set — that is what `acme_web_force_issue: true` is for (one-shot).
`--install-cert` re-runs after every (re)issue and is otherwise gated on the
target files being absent (or `acme_web_force_install_cert: true`), because
on the registry host running it restarts every registry container.

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

### Which metrics name goes on the certificate (per site)

A metrics host serves exactly one **site** (docs/contracts.md §Vocabulary),
and each site answers on its own public name — production has
`metrics.example.com` and `metrics.west.example.com`. `acme_web_domains`
therefore puts **this host's site's** name on the certificate, not the
environment-wide one:

```
cs_site_metrics_domains[cs_site] | default(cs_metrics_domain)
```

`cs_site_metrics_domains` is the frozen site-contract spelling
(docs/contracts.md §Site scoping); the operator input behind it is the
`metrics_domain` **host var** on the metrics host, and the fallback is
`cs_metrics_domain`, which is what a single-site install has always used and
what the map itself yields for a site that sets no override. The guard sits
on the **subscript**, never on a field, and the map is read **locally** on
the host being certified — never `hostvars[h].cs_site_metrics_domains`
(hard rule 10: that read silently returns Undefined, and the `| default`
then turns it into the other site's name with no error at all).

Nothing else changes: a host in several groups still gets one certificate
covering every name, and the SAN-set converge below re-issues by itself the
first time a metrics host is given a `metrics_domain` it did not have.

### Per-site basic-auth credentials

The two production metrics VMs do **not** share a password: each v1-built
host has one nginx basic-auth credential covering both its prometheus and
its loki vhost. So the htpasswd files are built from
`acme_web_metrics_credentials`, this host's own site's entry in the frozen
`cs_site_metrics_credentials` map, with the whole dict falling back to
`cs_metrics_credentials_default` when the site names no override:

```yaml
# vaulted secrets, optional, keyed by site
metrics_site_credentials:
  east:
    username: promuser        # fills BOTH usernames
    password: "<that metrics host's nginx basic-auth password>"
  west:
    prometheus_username: promuser
    prometheus_password: "..."
    loki_username: loguser
    loki_password: "..."
```

**The usernames matter as much as the passwords.** v2 defaults to
`promuser` / `loguser`; a v1-built metrics host being attached to may use
neither, and a wrong username is a 401 with exactly the same symptom as a
wrong password — logs silently dropped, and `roles/validate` failing at the
very end of the converge. Both are asserted non-empty, and both come out of
the same resolved dict so they cannot be sourced from different places.

`acme_web_prometheus_username` / `acme_web_loki_username` survive as the
**environment-wide** defaults. This role's tasks no longer read them;
`playbooks/group_vars/all/sites.yml` does, by name, to build
`cs_metrics_credentials_default`. That is why they must stay plain literals:
resolving either of them through the site map makes the template recursive
(the map's own default reads them back) and ansible aborts the run.

### Port 80 on the metrics and registry hosts

`roles/firewall` opens `cs_ports.controller_http` on the controller, the
metrics host and the registry host, so the default HTTP-01 challenge works
for `cs_portal_domain`, this site's metrics name and `cs_registry_domain`
alike.
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
| `acme_sh_version` | `3.1.4` | `versions.yml`; names the release tarball verbatim (the 3.x line has **no** leading `v`). Bump together with `acme_sh_tarball_sha256`. |
| `acme_keylength` | `ec-256` | Inventory name. acme.sh's own default made explicit; `"2048"` for RSA. |
| `acme_challenge_alias` | unset | Inventory name. DNS alias mode: validate through a permanent `_acme-challenge` CNAME so the DNS credential only writes to the alias zone. |
| `acme_web_force_issue` | `false` | One-shot re-issue when the SAN set cannot see the change (CA switch). |
| `acme_ca` | `zerossl` | Inventory name. Anything acme.sh's `--server` takes: `letsencrypt`, `letsencrypt_test` (staging — untrusted chain, no meaningful rate limits; `roles/validate` relaxes TLS verification for the `*test` CAs automatically), `google`, a directory URL, … |
| `acme_eab_kid` / `acme_eab_hmac_key` | unset | Inventory names. External Account Binding, for CAs that hand out account credentials out of band (Google Trust Services requires it; ZeroSSL accepts it instead of email registration). Both or neither; the HMAC key belongs in the vaulted `secrets.yml`. |
| `acme_account_email` | `{{ cs_admin_email }}` | Inventory name. |
| `acme_challenge_method` | `http` | Inventory name. See `docs/acme-providers.md`. |
| `acme_web_domains` | derived from `group_names` | `cs_portal_domain` / this site's metrics name / `cs_registry_domain`. A host in several groups gets one certificate covering every name. See "Which metrics name goes on the certificate". |
| `acme_web_cert_name` | first of the above | acme.sh's identifier for `--install-cert` and renewal. |
| `acme_web_force_install_cert` | `false` | Re-run `--install-cert` even when the targets exist — needed after changing a `--reloadcmd`. Off by default because the hook restarts registry containers. |
| `acme_web_prometheus_upstream` / `acme_web_loki_upstream` | `127.0.0.1:9090` / `127.0.0.1:3100` | Must match what `roles/metrics` and `roles/loki` publish. |
| `acme_web_prometheus_username` / `_loki_username` | `promuser` / `loguser` | The **environment-wide** usernames. Not read by this role's tasks: `playbooks/group_vars/all/sites.yml` reads them by name to build `cs_metrics_credentials_default`. Must stay plain literals — see "Per-site basic-auth credentials". |
| `acme_web_metrics_credentials` | `cs_site_metrics_credentials[cs_site]`, else `cs_metrics_credentials_default` | This site's four basic-auth values, and what the htpasswd files are actually built from. All four asserted non-empty on the metrics host — they are the only thing gating 3101/3102. Guard on the subscript, never on a field. |
| `acme_web_registry_images` | `cmptstks/registry:latest`, `registry:2` | Which running containers the reload hook restarts. Deliberately not pinned: the controller hard-codes the image when it creates a tenant registry. |

Consumed, not owned: `cs_ports.*`, `cs_portal_domain`, `cs_metrics_domain`,
`cs_registry_domain`, `cs_admin_email`, and the site contract `cs_site` /
`cs_site_metrics_domains` / `cs_site_metrics_credentials` /
`cs_metrics_credentials_default` (`playbooks/group_vars/all/sites.yml`,
spellings frozen — this role re-derives none of them).

Retired: `acme_web_prometheus_password` / `acme_web_loki_password`. They were
aliases for the vaulted `prometheus_basic_auth_password` /
`loki_basic_auth_password`, which `sites.yml` now reads directly; keeping
them would have left two spellings of the environment-wide password with
only one of them site-aware.

## Handlers

* **`Reload nginx`** — vhosts, snippets, dhparam. `ExecReload` sends `SIGHUP`
  to the container.
* **`Restart nginx`** — the unit file, `nginx.conf` and the image, all of
  which are fixed at container creation.

## Deviations from v1 (`roles/nginx` + `roles/acme`)

* The two v1 roles are merged. Splitting acme.sh from the nginx that serves
  its challenges bought nothing and let the two drift.
* Checksum-verified tarball install, per-version directories, no upstream
  installer, self-upgrade pinned off (see above).
* `nginx_image` is a single `repo:tag` pin instead of v1's split
  `nginx_image` / `nginx_image_tag` floating on `stable`.
* The container drops `--privileged` — nothing it does needs it.
* Config mounts are `:ro`.
* `listen … http2` (deprecated since nginx 1.25.1) is replaced by the
  `http2 on;` directive.
* v1's `grafana.conf` vhost is not ported — no grafana is deployed.
* v1 passed `--force` on an ungated issuance task, re-issuing a perfectly
  good certificate every time it ran. Now the *gate* lives in ansible (SAN
  convergence, above) and `--force` is passed only once that gate has decided
  issuance must happen — acme.sh's "not due yet" skip must not veto it, and
  without it a failed `--install-cert` after a successful `--issue` wedges
  behind acme.sh's renewal window.
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
