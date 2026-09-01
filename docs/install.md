# Greenfield install

A complete ComputeStacks environment on fresh Ubuntu 26.04 hosts. For adding
a region to an environment that already exists, see
[attach-mode.md](attach-mode.md) instead.

Read [../README.md](../README.md) for the reference material; this is the
order to do things in.

## 1. Plan the hosts

The smallest sensible install is five machines plus one node:

| Group | What it runs | Notes |
| --- | --- | --- |
| `controller` | portal, postgres, redis, vault, nginx/acme | Exactly one. |
| `metrics` | prometheus, alertmanager, loki behind nginx | Shared by every region. |
| `backup` | borg over SSH | Shared by every region. |
| `registry` | docker + nginx; the controller creates the registries over SSH | Optional but assumed by the registry settings. |
| `nameservers` | PowerDNS, postgres-replicated | One `ns_primary`, zero or more `ns_followers`. Optional if `dns_driver: none`. |
| `nodes` | docker, cs-agent, haproxy, cadvisor, fluentd | One per availability zone. |

Each host needs:

* Ubuntu 26.04 LTS, amd64, freshly installed;
* a single-word lowercase hostname, no dots — `node101`, never
  `node101.example.com` (preflight asserts this);

  ```bash
  hostnamectl set-hostname node101
  ```

* root SSH from the control machine, by key;
* a private address the control plane can use (`primary_ip`) and a public one
  (`public_ip`). They may be the same address.

Nodes additionally need a container network CIDR that does not overlap
anything else in the environment (RFC 1918, at least a `/28`, `/22` is the
usual size).

## 2. Prepare the control machine

```bash
git clone https://github.com/ComputeStacks/ansible-install computestacks
cd computestacks
ansible-galaxy role install -r requirements.yml -p galaxy_roles
ansible-galaxy collection install -r requirements.yml -p collections
```

You need ansible-core 2.19+ and the `cryptography` python library.

## 3. Point DNS at the hosts

The certificates are issued during the run, so the names have to resolve
first:

```
portal.example.com.      IN A   <controller public ip>
metrics.example.com.     IN A   <metrics public ip>
cr.example.com.          IN A   <registry public ip>
```

and, when you are running the bundled nameservers, delegate the tenant zone
to them:

```
usercontent.example.com. IN NS  ns1.example.com.
usercontent.example.com. IN NS  ns2.example.com.
ns1.example.com.         IN A   <ns1 public ip>
ns2.example.com.         IN A   <ns2 public ip>
```

Port 80 must be reachable from the internet on the controller for the default
HTTP-01 challenge. The metrics and registry hosts also serve HTTP-01 on 80.
If a host cannot expose 80, use a DNS-01 provider —
[acme-providers.md](acme-providers.md) has the matrix.

## 4. Build the inventory

```bash
cp -r inventories/example inventories/prod
$EDITOR inventories/prod/hosts.yml
$EDITOR inventories/prod/group_vars/all/main.yml
$EDITOR inventories/prod/group_vars/all/secrets.yml
```

`hosts.yml` carries the topology. Every node needs `hostname`, `primary_ip`,
`public_ip`, `region`, `az`, `container_network`, `container_network_name`;
every nameserver needs a unique `powerdns_name` (its FQDN — it becomes the
zone's NS records).

`main.yml` carries the domains (`cs_portal_domain`, `cs_metrics_domain`,
`cs_registry_domain`, `cs_app_zone`), the admin email, locale/currency, the
DNS driver, and the ACME settings.

`secrets.yml` carries everything else. Generate the immutable pair **once**:

```bash
openssl rand -hex 64   # -> secret_key_base
openssl rand -hex 64   # -> user_auth_secret
```

and then fill in `cs_admin_password`, `node_enrollment_token`,
`postgres_password`, `prometheus_basic_auth_password`,
`loki_basic_auth_password`, `backups_key`, and the three PowerDNS keys.
`openssl rand -hex 32` is fine for all of them.

> `secret_key_base` cannot be changed later. It keys every encrypted column in
> the controller, and a mismatch decrypts to `nil` rather than raising —
> rotating it silently destroys every stored credential. Treat rotation as a
> reinstall, and keep this file with your database backups.

Then encrypt it:

```bash
ansible-vault encrypt inventories/prod/group_vars/all/secrets.yml
```

### Optional inputs worth deciding now

* **Tailscale.** Set `tailscale_authkey` in `secrets.yml` and every host joins
  a tailnet; the controller then dials each node's agent over it and
  prometheus scrapes over it, instead of sending an admin Bearer token and
  exporter scrapes in cleartext across the public internet. Opt a same-L2
  region out with `tailscale_enabled: false` on the host or group. Without a
  tailnet, a remote region's control-plane traffic is cleartext — a supported
  choice, but an explicit one.
* **Ubuntu Pro.** Set `ubuntu_pro_token` for livepatch.
* **`uplink_interface`** on any node with a global IPv6 address. Docker turns
  on IPv6 forwarding the first time it creates an IPv6 network, which
  withdraws the RA-learned default route about half an hour later; the fix
  needs the interface name and the playbook will not guess it.
* **`haproxy_stats_password`.** Optional, recommended: without it the
  controller's column default is used, which is the same on every install.
* **A private controller image.** If `controller_image_repo` points at a
  registry that needs credentials, add them to `secrets.yml`:

  ```yaml
  docker_registries:
    - registry: registry.gitlab.com
      username: "gitlab+deploy-token-42"
      password: "CHANGEME"
  ```

  `registry` is the host as it appears in the image reference. The login is
  written to `/root/.docker/config.json` on every docker host, so nodes can
  pull privately mirrored images from the same list, and `cstacks upgrade`
  still works months later.

  On GitLab, use a **deploy token** with the `read_registry` scope (project or
  group, Settings → Repository → Deploy tokens). Its username is the whole
  `gitlab+deploy-token-<n>` string, and its password is shown once. Deploy
  tokens can be given an expiry date — when one lapses, every pull and every
  `cstacks upgrade` fails until it is replaced, so record the date somewhere.
  A personal access token with `read_registry` also works but ties the fleet
  to one person's account; a CI job token is far too short-lived.

  The registry must serve a publicly-trusted TLS certificate — the provisioner
  writes no `/etc/docker/certs.d` material and enables no insecure registries.

  Preflight proves the repository, the tag and the credentials before anything
  is installed, so a wrong tag or an expired token stops the run on the first
  task rather than half way through the controller.

## 5. Check connectivity

```bash
ansible -i inventories/prod all -m ping
```

## 6. Run it

```bash
ansible-playbook -i inventories/prod playbooks/site.yml --ask-vault-pass
```

The first run takes a while: it installs docker, pulls a dozen images, issues
certificates, loads the controller schema, seeds it from the rendered
manifest, enrols every node and runs the backfills. Watch for these
checkpoints:

1. **Preflight** — fails immediately on a missing node var, a duplicate `az`,
   a short `secret_key_base` or a non-Ubuntu host. Nothing has been changed at
   that point.
2. **Controller infrastructure** — vault initialises and writes its unseal
   keys to `/etc/computestacks/.vault-bootstrap/keys/`. Copy them somewhere
   safe; a rebooted controller comes back with a sealed vault.
3. **Seed controller** — renders `/var/lib/computestacks/manifest.yml` (root,
   0600) and applies it with `cstacks seed`. The file is deleted after a
   successful apply and kept after a failed one, because it is the thing to
   look at.
4. **Node agents** — each node reads its token hash back from the controller
   and restarts cs-agent. A failure here almost always means the seeded
   `Node.hostname` and the node's `hostname` var disagree.
5. **Validate** — the assertions. Read
   [../roles/validate/README.md](../roles/validate/README.md) for what each
   failure means.

## 7. Afterwards

* Log in at `https://<cs_portal_domain>` with `cs_admin_email` /
  `cs_admin_password`.
* Copy the vault unseal keys and the vaulted `secrets.yml` off the
  controller.
* Schedule `cstacks database-backup` (see the README) and copy its output
  off-host. Tenant volumes go to the borg server; the controller's own
  database does not.

## Known rough edges on Ubuntu 26.04

Two package sources need explaining, and both fail loudly rather than
silently:

* **Docker's apt repository** is keyed on the release codename, which
  `geerlingguy.docker` derives from `ansible_distribution_release`. On 26.04
  that is `resolute`, and `download.docker.com/linux/ubuntu/dists/resolute/`
  exists, so nothing needs overriding. The `docker_apt_repository` example
  commented in `group_vars/all/main.yml` is there only for a future release
  Docker has not published a suite for; if you ever set it, change the pinned
  versions in `versions.yml` with it, because those version strings name the
  `resolute` suite.
* **PostgreSQL is pinned at major 17 and does not come from the Ubuntu
  archive.** 26.04 ships PostgreSQL 18 and carries no `postgresql-17`, so
  `roles/postgres_repo` configures the PGDG repository
  (`apt.postgresql.org`, `resolute-pgdg` suite) with an apt preferences pin
  and runs before `geerlingguy.postgresql` on the controller and before
  `roles/powerdns` on the nameservers — the only two places a postgres
  package is installed. **Do not "fix" a mismatch by bumping
  `postgresql_version` to whatever major the archive offers**: 17 is the
  major the controller's schema and CI are validated against, which is the
  entire reason `postgres_repo` exists. Changing it means re-validating the
  controller repo and editing `playbooks/vars/Ubuntu-26.yml` in the same
  commit. That file also supplies the `Ubuntu-26` platform vars
  `geerlingguy.postgresql` ships no copy of; `include_vars` picks it up from
  the playbook directory, so no fork or patch is needed.

The `docker_apt_packages` and `node_exporter_apt_version` entries in
`versions.yml` both carry exact apt versions, verified against the live
indexes on the date noted there. Both packages are also `apt-mark hold`-ed
after install, so they cannot move on their own; the holds are released
before each install, so bumping a pin still rolls through on a rerun.
