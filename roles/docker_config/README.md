# docker_config

Owns `/etc/docker/daemon.json` and the `docker login` credentials on every
docker host; the engine itself comes from `geerlingguy.docker`.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Docker hosts" | `controller:metrics:registry:nodes` |
| `add-region.yml` | "New node docker" | `nodes` (the new node) |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** new node only; never runs on an existing shared host.
**Requires:** `geerlingguy.docker` earlier in the same play (skipped on a live
node whose docker is deferred — this role still runs there and only reloads
dockerd); `community.docker`.

## Purpose

Owns `/etc/docker/daemon.json` on **every** docker host — controller, metrics,
registry and nodes — and the `docker login` credentials in
`/root/.docker/config.json` on the same hosts. The engine itself is installed
by `geerlingguy.docker` (pinned in `requirements.yml`), and the node-only
TCP/TLS listener lives in `roles/docker_tls`.

The daemon settings:

```json
{
    "live-restore": true,
    "shutdown-timeout": 60
}
```

* `live-restore` keeps containers running across a daemon restart (patch-level
  daemon upgrades only — a major upgrade still stops everything, because the
  new daemon cannot re-attach to the old version's shims).
* `shutdown-timeout` is how long the daemon waits for containers to exit before
  killing them; docker's default 15s is too short for tenant databases to close
  cleanly on a node running hundreds of containers.

`shutdown-timeout` does nothing while `live-restore` is on. dockerd's shutdown
path returns early when live restore is enabled and containers are running — it
leaves them alone rather than stopping them, so the budget is never spent. The
value is here for the operator who sets `docker_config_live_restore: false`.

Why 60: docker's packaged unit sets no `TimeoutStopSec`, so systemd's
`DefaultTimeoutStopSec` of 90s is the real ceiling and would kill dockerd
first. 60 fits inside it with headroom for the
daemon's own teardown. Anything past ~85 needs a `TimeoutStopSec` drop-in for
`docker.service`, which this role does not own — `docker_tls` is node-only, so
that would mean a new unit-file override on every docker host to serve a path
that does not currently run.

Both are SIGHUP-reloadable, which is why they live in `daemon.json` and not in
an ExecStart flag, and why this role's handler **reloads** rather than restarts
dockerd. Docker hard-errors when an option is set in both `daemon.json` and the
command line, so nothing here may be repeated in the `docker_tls` drop-in.

## Private registry authentication

`docker login`, once per configured registry, written to
`/root/.docker/config.json`. It happens here rather than in whichever role
pulls first because that file is what **every** later root-run pull reads: the
controller's container create, `roles/node_observability`'s image preload, and
`cstacks upgrade` typed by hand months from now.

```yaml
docker_registries:
  - registry: registry.gitlab.com
    username: "gitlab+deploy-token-42"
    password: "{{ gitlab_deploy_token }}"
```

`registry` is the host\[:port\] exactly as it appears in the image reference. A
Docker Hub reference carries no host — write `docker.io`, and the role stores
the credentials under `https://index.docker.io/v1/`, which is the key docker
actually looks Hub credentials up under.

The list is composed in `playbooks/group_vars/all/registries.yml`, not here,
because `roles/preflight` needs the same answer one play earlier and a role's
`vars/` are invisible to a role that already ran. That file also folds the
controller's own `controller_registry_username` / `controller_registry_password`
into the list on controller hosts, so there is one login implementation rather
than two. An explicit `docker_registries` entry for the same host wins.

**Set `docker_registries` in the inventory, never in `playbooks/group_vars`.**
Playbook `group_vars` outrank inventory `group_vars`, so a default written
beside the composition would silently discard what the operator put in their
`secrets.yml` — the whole feature would no-op and the first sign of it would be
a `docker pull` denial in the controller play. The composition file therefore
defines no operator-facing variable at all; it reads
`docker_registries | default([])` at the point of use.

Three things this role deliberately does not do:

* **It does not verify the credentials.** `roles/preflight` already proved them
  against the registry's token endpoint, with a readable failure message,
  before any host was touched. The login task is `no_log` — the password is an
  argument — so a failure here prints nothing useful; the diagnostics live in
  preflight on purpose.
* **It does not pass `reauthorize`.** That option forces the credential write
  and reports `changed` on every converge (community.docker 4.6.1,
  `docker_login.update_credentials`). Without it the module compares the stored
  username and secret against the configured ones, so a rotated password still
  updates and an unchanged one is a no-op.
* **It does not log out.** Convergence here is one-directional: removing an
  entry from `docker_registries` stops the role refreshing that credential but
  leaves the existing one in `config.json`. Revoke the token at the registry —
  which is the only thing that actually takes access away — and delete the
  entry by hand if the host must not hold it.

The registry must serve a **publicly-trusted** TLS certificate. Nothing here
writes `/etc/docker/certs.d/<host>/ca.crt` and `insecure-registries` is not
offered; a registry behind a private CA needs that CA in the host trust store
by other means.

## ★ Play-level vars the play MUST set for geerlingguy.docker

`geerlingguy.docker` writes `/etc/docker/daemon.json` itself when
`docker_daemon_options` is non-empty, and notifies its own **restart** handler.
Two owners of one file is a config-flap, so the play must leave that var empty
(it is the role's default — the requirement is to never set it):

```yaml
- name: Docker hosts
  hosts: controller:metrics:registry:nodes
  roles:
    - role: geerlingguy.docker
      vars:
        docker_daemon_options: {}       # ← daemon.json is docker_config's; keep empty
        docker_install_compose: false   # standalone docker-compose binary: not used
        docker_users: []                # no non-root docker access on any host
    - docker_config
```

`playbooks/site.yml` ("Docker hosts") and `playbooks/add-region.yml` ("New
node docker") set exactly these, plus the pinned `docker_packages` below.
Other geerlingguy vars worth knowing:

| geerlingguy var | Wanted value | Why |
| --- | --- | --- |
| `docker_daemon_options` | `{}` (default) | **Required.** Otherwise it fights `docker_config` for `daemon.json` and restarts dockerd. |
| `docker_service_manage` | `true` (default) | Lets it enable/start docker; `docker_tls` restarts via its own handler afterwards. |
| `docker_restart_handler_state` | `restarted` (default) | Only fires on package/repo changes. |
| `docker_packages` / `docker_packages_state` | pin + `present` | Contract 4 (pin everything). Exact versions come from `docker_apt_packages` in `versions.yml`; `site.yml` and `add-region.yml` `apt-mark hold` them after install so an unattended `apt upgrade` cannot restart every container, and `make upgrade-docker` moves them on live nodes. |
| `docker_install_compose` | `false` (default) | Nothing uses the standalone binary. |
| `docker_apt_release_channel` | `stable` (default) | |

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `docker_config_dir` | `/etc/docker` | |
| `docker_config_live_restore` | `true` | |
| `docker_config_shutdown_timeout` | `60` | Seconds. Dormant while `live_restore` is on. Keep under ~85: systemd's 90s `DefaultTimeoutStopSec` kills dockerd first. |
| `docker_config_registry_mirrors` | `[]` | In `daemon.json` rather than an ExecStart flag: it is reloadable, and keeps the node ExecStart override clean. |
| `docker_config_extra_options` | `{}` | Merged last. **Only SIGHUP-reloadable options** — this role never restarts dockerd. Anything else needs a restart path added here first. |
| `docker_config_registries` | `{{ cs_registry_logins }}` | The composed `{registry, username, password}` list; the operator-facing name is `docker_registries`. Defaults to `[]` when the composition file is absent, so the role still works standalone. |

## No reboot here

This role never reboots a host. A fresh package install can pull in a new
kernel and remove the running kernel's `/lib/modules/$(uname -r)` tree;
`modprobe` of `br_netfilter` / `nf_nat` / `ip_tables` then fails and dockerd
aborts while setting up its iptables chains. That hazard is handled earlier,
by `roles/common`'s reboot-if-required check right after package install —
before docker is installed, never on an `existing_env` host and never on a
live node (`roles/common/README.md`). Ubuntu 26.04 ships nftables with
`iptables-nft` as the default alternative and docker-ce supports it directly,
so no iptables-alternative switching is involved. If a greenfield converge is
seen to fail with dockerd unable to create its iptables chains, look at that
reboot check — not at a reboot bolted onto this role.

## Requirements

`community.docker` (`docker_login`), pinned in `requirements.yml`. Runs after
`geerlingguy.docker` in the same play — the login needs a running daemon,
because the module authenticates through it.
