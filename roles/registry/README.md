# registry

**Owner: Wave 3G.** `hosts: registry`.

## Purpose

The container registry host. This role is deliberately thin, because **the
controller provisions the registries, not ansible**: when a user creates a
container registry, the controller opens an SSH connection to this host and
creates the container through DockerSSH
(`app/models/container_registry.rb#docker_client`). Everything this role does
is prepare the filesystem those containers bind-mount.

There is no v1 counterpart — v1 left the registry host's contract unwritten.

## What it does

1. Creates `/computestacks-mnt` (0700), the bind-mount root. Each registry
   gets `/computestacks-mnt/<name>/{auth,data}`, created by docker at
   container start.
2. Creates `/opt/container_registry` and `/opt/container_registry/ssl` (0700).
3. Warns when `ssl/fullchain.pem` is missing — that is `acme_web`'s output,
   and registries created without it serve a broken TLS endpoint.
4. Asserts the inventory has a controller at all; without one, nothing can
   ever use this host.

## The contract with the rest of the install

| Piece | Owner | Detail |
| --- | --- | --- |
| Root `authorized_keys` entry | `roles/ssh_trust` | The controller's **application** key, `/etc/computestacks/.ssh/id_ed25519.pub` (`roles/controller`), plus its root key. Control-plane edge #19. |
| TLS certificate | `roles/acme_web` | `--install-cert` writes `fullchain.pem`/`privkey.pem` into `ssl/`, and its `--reloadcmd` hook refreshes them and restarts running registry containers on renewal. |
| Firewall | `roles/firewall` | `cs_ports.tenant_https` plus `cs_ports.tenant_port_begin`–`tenant_port_end` (10000–50000), the range the controller allocates registry ports from. SSH from the controller rides the global ssh accept. |
| Settings | manifest (`controller_seed`, Wave 4I) | `registry_node` (this host's IP), `registry_base_url` (`cs_registry_domain`), `registry_ssh_port` (string, default `"22"`), `cr_le` (`cs_registry_domain`), and the `updated_cr_cert` feature — which is what makes the controller mount `/opt/container_registry/ssl` and read `fullchain.pem`/`privkey.pem` rather than the two legacy certificate layouts. |
| Registry image | the controller | Hard-coded as `cmptstks/registry:latest` in the controller source. It is not pinned or pre-pulled here: the provisioner does not choose it, and pulling it would only pre-warm a cache. |

`validate` (Wave 4J) checks the controller → registry SSH path.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `registry_data_dir` | `/computestacks-mnt` | Hard-coded in the controller; the variable only keeps the literal out of the tasks. |
| `registry_home_dir` | `/opt/container_registry` | |
| `registry_ssl_dir` | `{{ registry_home_dir }}/ssl` | Written by `acme_web`, mounted read-only into every registry container as `/certs`. |

## Requirements

None beyond `ansible.builtin`. Runs after `geerlingguy.docker`,
`docker_config` and `acme_web` on this host (`playbooks/site.yml`), and before
`ssh_trust`.
