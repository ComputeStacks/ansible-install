# Changelog

## October 14, 2026

* **geerlingguy.docker 8.0.0 and ansible.posix 2.2.2** (were 7.4.7 and
  1.6.2); run `make deps`. The docker role now writes its source as deb822
  (`/etc/apt/sources.list.d/docker.sources`) and deletes the `docker.list`
  7.x wrote, on the next converge of each docker host (a live node whose
  docker is deferred keeps its `docker.list` until then). The
  `docker_apt_repository` override no longer exists; the commented example
  in `group_vars/all/main.yml` is now `docker_add_repo: false`. ansible.posix
  2.2.1 fixed a symlink privilege escalation in `authorized_key`.
* **The cs-agent apt source is a deb822 file**,
  `/etc/apt/sources.list.d/computestacks.sources`. The next converge of a
  node deletes the old `computestacks.list`, so the repository is never
  listed twice; the signing key stays at
  `/etc/apt/keyrings/computestacks.gpg`.
* **Facts are read through `ansible_facts`** rather than the top-level
  `ansible_*` variables ansible-core is deprecating, in preflight and
  node_kernel. No behavior change.
* **An unreachable host now stops the run, before anything is changed.**
  Every playbook first pings each host it targets and fails, naming every
  host that did not answer with its address, port and ssh's error. Until
  now ansible dropped such a host and carried on, skipping every later play
  that targeted it, and failed much later somewhere unrelated. After that,
  any host that fails or becomes unreachable stops every host
  (`any_errors_fatal = True` in `ansible.cfg`) instead of being left behind;
  the validate plays still report every host. If you run ansible with your
  own `ansible.cfg`, copy the setting.
* **`make firewall ENV=<env>` applies a firewall change on its own**
  (`playbooks/firewall.yml`). After editing `firewall_allow_ssh_from`, a
  host's `ssh_port`, extra allowed addresses or a peer's address, it runs
  preflight's inventory checks and re-renders each host's firewall exactly as
  `site.yml` or `add-region.yml` would, without the rest of the converge.
  Existing shared hosts in an attach inventory still only receive node address
  appends. `--limit` and `--check --diff` are supported. The README's
  "Changing the firewall" covers restricting ssh without locking yourself out
  and moving sshd to another port.
* **IPv6 router advertisements now survive a reboot on netplan nodes.**
  systemd-networkd resets the kernel's `accept_ra` to 0 on links it manages,
  so the `accept_ra = 2` sysctl on `uplink_interface` was undone at boot.
  Where networkd manages the uplink, `node_kernel` now writes an
  `IPv6AcceptRA=yes` drop-in next to the uplink's `.network` file
  (`/etc/systemd/network/<file>.network.d/60-cs-node-accept-ra.conf`),
  removes `/etc/sysctl.d/61-cs-node-ipv6.conf`, and runs `networkctl reload`.
  The operator's netplan YAML is not edited. Uplinks networkd does not manage
  keep the sysctl. An `uplink_interface` the host does not have now fails the
  node instead of writing a sysctl for it.
* **Tailscale now installs from the `artis3n.tailscale` collection** (1.2.1,
  role `artis3n.tailscale.machine`) instead of the standalone galaxy role,
  whose deprecation task fails outright on ansible-core 2.19 and later. Run
  `make deps` after pulling; `galaxy_roles/artis3n.tailscale` is no longer
  used and can be deleted. The first converge replaces the old
  `/etc/apt/sources.list.d/tailscale.list` with the collection's deb822
  source (left in place, the two make apt refuse to update). Joined hosts are
  not re-authenticated, and every `tailscale_*` variable keeps its meaning.
* **`tailscale_access_only: true`** keeps a host or group on the tailnet for
  operator access only (Tailscale SSH needs `--ssh` in
  `tailscale_extra_args`): the controller dials its agent on `primary_ip`,
  prometheus scrapes it on `primary_ip`, its acme backend address is the
  controller's `primary_ip`, its agent binds `primary_ip:8500`, and its
  firewall accepts no control-plane traffic on `tailscale0` (UDP 41641 stays
  open). Default `false`, which is the existing behavior. Flipping it on an
  already-seeded host changes addresses the controller holds, so run that
  converge with `controller_seed_update_addresses: true` and no `--limit`.
* **Load balancer records now default to a 4-hour TTL**
  (`powerdns_lb_record_ttl: 14400`, was 300). The next converge rewrites the
  TTL on existing A, wildcard CNAME and `_cs-lb` records; set the variable to
  keep the old value.
* **Ubuntu's login clutter is gone.** Only the ComputeStacks banner and
  Ubuntu's "System restart required" notice print at login; the help links,
  sysinfo, ads, ESM/Pro notices and update counts are diverted out of
  `/etc/update-motd.d` with `dpkg-divert`, so package upgrades (including
  unattended ones) cannot bring them back. `common_motd_keep` lists the
  Ubuntu fragments to leave running.
* **`ssh_port` is back as a per-host inventory var**, as in v1. A host whose
  sshd is not on 22 sets it, and the host's own firewall opens that port; the
  controller's Node rows, its ssh config (greenfield) and the
  `registry_ssh_port` setting carry it; nodes dial their backup server on the
  backup host's value; and `validate` checks on it. Until now port 22 was assumed everywhere, so a node
  with sshd elsewhere locked itself out when its firewall loaded. Moving sshd
  is still the operator's job, and `ansible_port` is set separately (the
  example inventory derives it from `ssh_port`). `backup_ssh_port` still
  overrides the backup port. Preflight fails a host whose `ansible_port`
  differs from its `ssh_port`.

***

## October 10, 2026

* **PostgreSQL 18** on the controller and the nameservers
  (`postgresql_version: 18`), matching the controller's schema and CI. It
  still comes from PGDG rather than the Ubuntu archive, so a later major can
  be adopted within the 26.04 LTS. An existing host on 17 needs a
  `pg_upgrade` before converging; the provisioner does not do one.

***

## October 9, 2026

* **Pins moved to controller 9.8, cs-agent 3.4.0 and docker 29.9.0**
  (containerd.io 2.4.1, buildx 0.38.0). `make upgrade-docker` needs exactly
  these controller and agent versions; an existing fleet takes the agent on
  its next `site.yml` run (before a maintenance window, since the install
  restarts the agent) and the docker bump through `make upgrade-docker`.
* **The preloaded SFTP bastion image is `cs-docker-bastion:v4`.** `v2` reads
  the metadata path that controller 9.6 removed and crash-loops on start;
  controller 9.8.1 makes the same change to its default.
* **Nodes now require a second, blank disk, named by `docker_data_device`.**
  Docker 29 keeps images and container layers in containerd's store under
  `/var/lib/containerd`, which landed on the root disk. A new role,
  `roles/node_storage`, runs before docker is installed: it formats the disk
  XFS with project quotas (only when it is provably blank), mounts it at
  `/var/lib/docker-data` by UUID, bind-mounts it over `/var/lib/docker` and
  `/var/lib/containerd`, and makes `docker.service` and `containerd.service`
  require those mounts. A re-run on a finished node changes nothing.
* **A disk the role would not touch stops the run before any host changes.**
  Preflight probes every new node and refuses — listing each node and why —
  a missing device, a partition or the root disk, a partition table, any other
  filesystem, a mount elsewhere, a blank disk on a node where docker already
  wrote to `/`, or our filesystem mounted without `prjquota`.
* **Nodes built before this change must be rebuilt.** They have no data disk,
  and `site.yml`, `add-region.yml`, `make upgrade-docker` (in its preflight,
  before any node is held) and `validate`'s new `storage` check all refuse
  them.

***

## October 7, 2026

* **Attach mode links the default user group to the new regions only.** The
  attach manifest sent `user_group: {link_regions: all}`, which linked the
  default group to every region on the controller — including any an operator
  had deliberately kept off it — and links are never removed. It now sends
  `link_regions: new` (`controller_seed_attach_link_regions`), which needs
  controller 9.7.10 or later; the controller prep play checks the running
  image before the database backup and stops if it is older.
* **`backup_ssh_port` describes a backup server whose sshd is not on port 22.**
  The node's borg SSH backend and `validate`'s borg check both used
  `cs_ports.ssh`, which the inventory cannot override; an existing backup
  server listening elsewhere was unreachable.
* **`make upgrade-docker ENV=<env>` upgrades docker on the nodes** —
  `playbooks/upgrade-docker.yml`, run in a scheduled maintenance window after
  bumping `docker_apt_packages`. One node at a time: hold it with
  `cs-agent maintenance on`, stop the running tenant containers, upgrade
  docker and the pending OS updates, reboot when one is required, start
  exactly what was running, release the hold. The first failure stops the
  run; re-running resumes (`LIMIT=<node>` for one node). Requires cs-agent
  >= 3.4.0 and controller >= 9.8.0. See `roles/node_upgrade/README.md`.
* **`site.yml` and `add-region.yml` no longer upgrade docker or reboot a live
  node** (one with tenant containers). A drifting docker pin or a pending
  reboot there is skipped with a warning pointing at `make upgrade-docker`.
  Nodes without tenants and every other host behave as before.
  `docs/contracts.md` rule 3 is amended accordingly.

***

## September 26, 2026

* **With `dns_driver: powerdns`, the provisioner writes each az's load
  balancer records itself.** `docs/install.md` used to tell operators to put
  the `<app_domain>` A and `*.<app_domain>` CNAME in the public parent, below
  the delegation of `cs_app_zone` — where they are never served — while
  preflight failed the run until they resolved. `roles/powerdns` now writes
  the pair into `cs_app_zone` on the primary nameserver right after the zone
  exists, with a `_cs-lb.<app_domain> TXT "owner=<node>"` ownership marker,
  and converges it on every run. A matching hand-made pair is adopted;
  anything else at those names fails the run before any write. `add-region.yml`
  writes the new az's records on the existing `ns_primary` the same way.
  Stale names are warned about, never deleted. Turn it off with
  `powerdns_manage_lb_records: false` on the nameservers.
* **Preflight checks the delegation instead of the records** when they are
  managed: the parent zone's own servers must refer `cs_app_zone` to exactly
  the bundled nameservers. `validate` checks the records on every nameserver
  (`dns`) and through public resolution (`lb_domain_public`). With any other
  DNS driver, preflight's public check for hand-made records is unchanged.
  An environment that sets `preflight_check_app_domain_dns: false` (split
  horizon, private test estates) now also needs `lb_domain_public` in
  `validate_skip`, or the run fails at the end.
* Per-az delegation stays manual: an `app_domain` that is its own zone on the
  bundled nameservers is skipped with a warning.

***

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
  a name it no longer answers on. The `DRY_RUN` preview runs on every path
  that can reach an existing row — attach mode, or a run with the address
  flag — and the run fails when it reports drift on a load balancer domain.
  `controller_seed_dry_run_first` governs only whether the preview is
  printed, and no longer disarms the check. Changing an existing load
  balancer's domain stays a controller-side, human-operated action.
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
