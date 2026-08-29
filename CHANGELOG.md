# Changelog

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
