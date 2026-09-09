# Changelog

## September 9, 2026

* **Every availability zone gets its own load balancer domain.** `app_domain`
  is a new optional node host var: the name that az's load balancer answers
  on, and the CN of the wildcard certificate it serves. v1 had two variables
  for this and v2 collapsed them into one, so a single scalar was rendered
  into every az's load balancer — an estate whose nine regions each answer on
  a different name could not be expressed at all. `cs_app_zone` is unchanged
  and stays environment-wide: one parent tenant zone, one `pdnsutil
  create-zone`, one `dns.zones` entry, and every `app_domain` under it. An
  inventory that names no `app_domain` renders exactly the manifest it
  rendered before, byte for byte.
* **One self-signed wildcard per domain, generated once per domain.** The
  `creates:` guard used to sit on a path with no domain in it, so the first
  az to converge won the file and every other az was seeded with a
  certificate for the first az's name. The default domain keeps the legacy
  path deliberately — moving it would change the value the controller
  compares against and push a fresh certificate to every load balancer in the
  fleet on the next converge.
* **Attach mode generates the new region's certificate instead of reusing
  whatever sat at the legacy path.** `add-region.yml` runs
  `roles/controller` with `tasks_from: attach_prep`, which previously
  generated nothing: the seed then read the *existing* environment's pem and
  installed it as the new region's shared certificate while the manifest
  rendered the new region's own domain. Preflight now requires a new az to
  name an `app_domain` of its own, and to differ from `cs_app_zone`.
* **The seed refuses to rotate a certificate onto a load balancer whose
  domain it cannot change.** The apply writes a load balancer's
  `shared_certificate` on an existing row but only *reports* its `domain`, so
  a stale manifest would leave a live load balancer serving a certificate for
  a name it no longer answers on. The `DRY_RUN` preview is now unconditional
  and the run fails when it reports drift on a load balancer domain;
  `controller_seed_dry_run_first` governs only whether the preview is
  printed. Changing an existing load balancer's domain stays a
  controller-side, human-operated action.
* **`validate` closes the loop against reality.** A new `lb_domain` check
  reads back the controller's own asynchronous verdict — that the load
  balancer reached `domain_valid`, and that its domain matches its
  certificate's CN. A missing `*.<app_domain>` CNAME used to pass preflight,
  pass the seed, end the run green, and surface weeks later as one region
  with no working ingress. `docs/install.md` now spells out both records; the
  wildcard must be a **CNAME**, an A record fails.
* **The bootstrap manifest has a render harness** (`tests/manifest_render.yml`,
  run by `make check` against both shipped inventories). The single most
  consequential template in the tree could previously only be exercised
  against a live controller.
* `controller_wildcard_domain` and `controller_seed_lb_domain` are retired;
  an inventory that still sets either fails the run rather than being
  silently ignored.

***

## September 4, 2026

* **`site` — the physical facility a host lives in — is a first-class
  concept.** A provisioner-only one: the controller has no column for it and
  nothing about it is seeded. It decides which metrics host scrapes a node
  and which backup server that node writes to, which is the case no
  controller-side grouping expresses — two Locations can share one metrics
  server. One metrics host and at most one backup server per site.
* **Every per-site value resolves through the site maps, never a global.**
  The backup server was `groups['backup'] | first`, which in a two-site fleet
  handed every node whichever host sorted first: the node then SSHed at
  another site's server, failed on a key that was never installed there, and
  named the wrong host in the failure. The prometheus endpoint had the same
  shape through `cs_metrics_domain`, and so did the loki push URL.
* **The metrics nginx basic-auth username is per site too, not just the
  password.** A wrong username is a 401 indistinguishable from a wrong
  password, and a v1-built metrics host answers to whatever its own htpasswd
  holds.
* **One metric client and one log client per site**, matched by exact
  endpoint string; `file_sd` fragments and the metrics certificate are scoped
  the same way.
* **An inventory that never mentions `site` is unchanged.** Every host lands
  in the site called `default`, every map has one key, and every role
  resolves what it always did. `tests/fixtures/single-site` is that
  inventory, frozen as the regression baseline.
* `--limit region_*` works: a directory inventory is parsed alphabetically,
  so the constructed plugin's source file has to sort before the plugin
  config, and `make check` now regression-tests exactly that.
* Attach mode no longer rewrites an existing backup server's configuration,
  the v1 firewall append is anchored on the whole rule rather than the
  address, and `attach_prep` is guarded before it touches the controller.

***

## v2 — August 28, 2026

Ground-up rewrite. The previous tree is tagged `v1-final`; an environment
built by it keeps working and is extended with `playbooks/add-region.yml`
rather than converged onto this one.

* **Ubuntu 26.04 LTS** replaces Debian. Consul is gone from the whole stack,
  along with the consul PKI, the containerized backup agent, and the
  generated `bootstrap.rake`.
* **Multi-region is first class.** One inventory describes the whole
  install; `region`/`az` are node host vars, and shared-host config is
  rendered by iterating groups instead of indexing `[0]`. Templates read
  inventory vars only, never gathered facts, so `--limit` can no longer drop
  an untargeted region out of the prometheus configuration or the firewall.
* **Two playbooks:** `site.yml` for a whole environment, `add-region.yml` for
  a new region against an existing one — which writes to the existing shared
  hosts only additively (`docs/attach-mode.md`).
* **Controller seeding is a versioned manifest** applied by
  `rake bootstrap:apply`, replacing the generated rake task that had drifted
  from the application's models across three releases.
* **cs-agent v3.3.0** as a native deb, enrolled by reading the token hash
  controller-side.
* **PostgreSQL comes from PGDG, pinned at major 17** (`roles/postgres_repo`,
  on the controller and the nameservers). Ubuntu 26.04 defaults to 18 and
  carries no `postgresql-17`, and 17 is what the controller's schema is
  validated against; the role adds the repository and an apt preferences pin
  that also refuses the unversioned `postgresql*` metapackages outright, so a
  stray dependency cannot drag the fleet onto another major.
* **Every version is pinned** in `playbooks/group_vars/all/versions.yml`, and
  every role is convergent: a pin bump rolls out on the next run, and no role
  skips its work because a service already exists.
* **`SECRET_KEY_BASE` and `USER_AUTH_SECRET` are immutable inputs.** v1
  generated them when blank, which silently destroyed every encrypted column
  on a re-run against a wiped environment file. Preflight now fails instead.
* **nftables in a dedicated table**, never `flush ruleset` — v1's script
  destroyed docker's and the agent's rules on every reload, and appended
  duplicate rules on every converge.
* **Backups are SSH/borg only**; NFS is gone, the borg client and server are
  pinned to the same version, and the node authenticates as an unprivileged
  account with its own key.
* **Optional tailscale mesh** for control-plane traffic between regions, and
  optional Ubuntu Pro livepatch.
* **A `validate` role that checks the legs that fail silently**: the
  prometheus label contract, the datachannel latch, the controller's dial
  path to each agent, borg, root SSH, and the portal from the nodes.
* Secrets live in an ansible-vault file; nothing in the tree carries a real
  hostname, address or credential.

***

## December 3, 2025

* Update cadvisor, node backup agent, and docker daemon.
* Various updates and improvements to match current version of ComputeStacks.

## November 25, 2025

* Update for Debian 13
* Separete nameserver provisioning process

## May 21, 2024

* Various bug fixes.
* Update repositories and versions.
* Our node agent now runs in a container.

***

## Sept 21, 2023

* Docker installation on debian 12 seems to have issues with iptables/nftables. To resolve this, the docker role will now reboot after docker is installed, and then wait for the server to come back online before proceeding with the rest of the installation.
* Node agent now runs within a container
* Remove remnants of our CentOS days (selinux labels).

***

## July 6, 2023

**Significant Change: ComputeStacks has deprecated support for multi-node availability zones. This playbook now only installs a single node per-az.**

* Debian 12 Bookworm
* Added linux bridge networks for containers
* Removed calico
* Removed etcd
* Removed corosync and pacemaker
* Haproxy v2.8 is now used
* Full support for ipv6

***

## May 8, 2023

* Redis will now use the redis apt repo.
* Add script to ensure real IP is recovered on the controller when using Cloudflare.
* Various updates for v8.1.

***

## Apr 8, 2023

* Remove option to add ComputeStacks Support access. Can be manually added later.

***

## Apr 6, 2023

* Remove dnsmasq. No longer necessary.

***

## Mar 14, 2023

* Make SSH the default backup transport (previous was NFS).
