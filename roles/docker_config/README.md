# docker_config

**Owner: Wave 1B.**

## Purpose

Owns `/etc/docker/daemon.json` on **every** docker host — controller, metrics,
registry and nodes — and the `docker login` credentials in
`/root/.docker/config.json` on the same hosts. The engine itself is installed
by `geerlingguy.docker` (pinned in `requirements.yml`), and the node-only
TCP/TLS listener lives in `roles/docker_tls`.

Values ported from v1's `roles/docker`:

```json
{
    "live-restore": true,
    "shutdown-timeout": 120
}
```

* `live-restore` keeps containers running across a daemon restart (patch-level
  daemon upgrades only — a major upgrade still stops everything).
* `shutdown-timeout` 120s: docker's default 15s is too short for tenant
  databases to close cleanly on a node running hundreds of containers.

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

Two things this role deliberately does not do:

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

Wave 4J wires this into `site.yml`/`add-region.yml` (this role does not edit
either playbook). Other geerlingguy vars worth knowing:

| geerlingguy var | Wanted value | Why |
| --- | --- | --- |
| `docker_daemon_options` | `{}` (default) | **Required.** Otherwise it fights `docker_config` for `daemon.json` and restarts dockerd. |
| `docker_service_manage` | `true` (default) | Lets it enable/start docker; `docker_tls` restarts via its own handler afterwards. |
| `docker_restart_handler_state` | `restarted` (default) | Only fires on package/repo changes. |
| `docker_packages` / `docker_packages_state` | pin + `present` | Contract 4 (pin everything). The engine version pin belongs in `versions.yml` and is **not yet set** — flagged to Wave 4J, since `versions.yml` edits outside `vault_image` are outside this wave's allowlist. v1 additionally ran `apt-mark hold` on the docker packages so an unattended `apt upgrade` could not restart every container; Wave 4J should keep that behaviour. |
| `docker_install_compose` | `false` (default) | Nothing uses the standalone binary. |
| `docker_apt_release_channel` | `stable` (default) | |

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `docker_config_dir` | `/etc/docker` | |
| `docker_config_live_restore` | `true` | |
| `docker_config_shutdown_timeout` | `120` | Seconds. |
| `docker_config_registry_mirrors` | `[]` | v1 passed a `--registry-mirror` ExecStart flag; `daemon.json` is reloadable and keeps the node ExecStart override clean. |
| `docker_config_extra_options` | `{}` | Merged last. **Only SIGHUP-reloadable options** — this role never restarts dockerd. Anything else needs a restart path added here first. |
| `docker_config_registries` | `{{ cs_registry_logins }}` | The composed `{registry, username, password}` list; the operator-facing name is `docker_registries`. Defaults to `[]` when the composition file is absent, so the role still works standalone. |

## ⚠ v1's post-install reboot (NOT ported)

v1's `roles/docker/tasks/install.yml:63-82` rebooted every host right after
installing docker, on this comment:

> 2023-09: Docker is currently failing to start due to iptable/nftable issues.
> A reboot seems to resolve this issue.

It was added in commit `dd44c4e` ("Move our node agent into a container. Docker
bugfix on debian 12") — i.e. it is a Debian 12 / bookworm-era workaround from
the same run that installs the OS packages. The plausible mechanism is not
docker-specific: the installer does a full apt upgrade earlier in the run, which
can install a **new kernel** and remove the running kernel's
`/lib/modules/$(uname -r)` tree; `modprobe` of `br_netfilter` / `nf_nat` /
`ip_tables` then fails and dockerd aborts while setting up its iptables chains.
A reboot into the new kernel fixes it. (The other candidate — iptables-nft
alternative switching — does not normally need a reboot.)

**Decision: v2 does not reboot.** It is a blunt instrument, it is fatal in
attach mode (rebooting an existing production controller/metrics host), and the
root cause belongs to whoever upgrades packages. Ubuntu 26.04 ships nftables
with `iptables-nft` as the default alternative and docker-ce supports it
directly, so the nft half of the theory does not apply.

**Flagged for Wave 1A (`common`) / Wave 4J:** the kernel-upgrade hazard is real
and unverified on 26.04. `common` should handle `reboot-if-required`
(`/var/run/reboot-required`) *before* the docker play, on **new hosts only**
(never on `existing_env: true` hosts), which removes the hazard properly. If a
greenfield 26.04 converge is seen to fail with dockerd unable to create its
iptables chains, that is this issue and the reboot-first fix is the answer — not
a reboot bolted back onto this role.

## Requirements

`community.docker` (`docker_login`), pinned in `requirements.yml`. Runs after
`geerlingguy.docker` in the same play — the login needs a running daemon,
because the module authenticates through it.
