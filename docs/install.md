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

Nodes also need a **second, blank disk** for docker and containerd: a whole
device with no partition table, no filesystem and nothing in fstab, sized for
every image, container layer and volume the node will hold. The provisioner
formats it and mounts it before docker is installed; you name it with
`docker_data_device` (§4).

[requirements.md](requirements.md) has the sizing per host, the node disk
layout and what makes the provisioner refuse a disk, and the ports a provider
firewall must allow.

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

The provisioner installs no DNS server and creates no DNS records: you
publish every name below at your own DNS host.

### Every load balancer needs its own pair of records

Each node runs its own haproxy load balancer, and each answers on its own
`app_domain` (`cs_app_zone` itself if the node sets none — a single-az install
therefore needs exactly one pair). Tenant containers are published as
`<container>.<app_domain>`, so every az needs:

```
exm-001.usercontent.example.com.    IN A     <that node's public ip>
*.exm-001.usercontent.example.com.  IN CNAME exm-001.usercontent.example.com.
```

and, only for an az with a public IPv6 address (the node's `public_ipv6`):

```
exm-001.usercontent.example.com.    IN AAAA  <that node's public_ipv6>
```

**These records are yours, and this is the step that is most often missed.**
Create the pair for every az, in the public DNS that serves `<app_domain>`,
before the run: preflight digs for them and fails if the apex does not resolve
to the node's `public_ip`. For an add-region run on an existing environment
that still uses its old nameservers, create the new az's records there by hand
first.

**The wildcard MUST be a CNAME. An A record fails.** The controller proves the
wildcard exists by querying a random label under the domain *for a CNAME
record* (`LetsEncryptServices::ValidateDomainService#load_wildcard_cname!`), so
a wildcard A record answers nothing and the load balancer is marked
`domain_valid: false` with event_code `2721edc59787a807` ("Missing CNAME
record"). Two neighboring failures worth recognizing: a wildcard CNAME
pointing anywhere other than the domain itself is `635285f7f2889009`, and an A
record that is not one of that load balancer's own public addresses is
`510806d5621c97d5`. That rule covers **AAAA records too**: every address at
the apex must be one of the load balancer's public addresses, and the only
IPv6 address the provisioner gives a load balancer is the node's
`public_ipv6`. An AAAA at the apex with no matching `public_ipv6` is
therefore an INVALID load balancer — set `public_ipv6`, or remove the AAAA.
Preflight warns about such an AAAA on hand-made records; `roles/validate`
does on records the provisioner writes.

Until `domain_valid` is true the load balancer never becomes `active?` and
nothing deploys into that region. Nothing in the run fails on it, because the
check is asynchronous — `LoadBalancerWorkers::ValidateDomainWorker` runs from
the row's `after_save` and writes the verdict minutes later. `roles/validate`'s
`lb_domain` check reads that verdict back, waits a couple of minutes for it,
and fails the run if it came back negative; if it has not been written yet the
check says so and does not fail, so **re-run `--tags lb_domain` if you see
that message.**

These names must resolve publicly, from the control machine as well as from
the controller (`preflight_check_app_domain_dns: false` skips the check, for a
control machine that cannot see public DNS).

### Certificates for the load balancer domains

The controller (9.9) issues each load balancer's `<app_domain>` and
`*.<app_domain>` certificate itself, over DNS-01, with the credential you give
it. Set that up in the inventory before the run; see
[lb-certificates.md](lb-certificates.md). Until the certificate is active the
load balancer serves a self-signed one.

## 4. Build the inventory

```bash
cp -r inventories/example inventories/prod
$EDITOR inventories/prod/hosts.yml
$EDITOR inventories/prod/group_vars/all/main.yml
$EDITOR inventories/prod/group_vars/all/secrets.yml
```

`hosts.yml` carries the topology. Every node needs `hostname`, `primary_ip`,
`public_ip`, `region`, `az`, `container_network`, `container_network_name` and
`docker_data_device`.

`docker_data_device` is the blank data disk from §1, as a whole device under
`/dev/`: `/dev/vdb` on KVM, `/dev/xvdb` on xcp-ng, or a stable
`/dev/disk/by-id/...` name where the platform offers one. The example sets it
once as a group var on `nodes`; set it per host where nodes differ.

Each node may also carry `app_domain`, the domain that az's load balancer
answers on — the pair of DNS records above. It is optional and must be unique
per az; a node that sets none uses `cs_app_zone`, which is what a single-az
install wants and what every inventory written before per-az domains existed
does. `app_domain` must sit at or under `cs_app_zone`, and `cs_app_zone`
itself must be 2 to 5 labels long; preflight asserts both, and
`docs/contracts.md` §Per-az application domains explains why.

`main.yml` carries the environment-wide domains (`cs_portal_domain`,
`cs_metrics_domain`, `cs_registry_domain`, `cs_app_zone` — the single parent
tenant zone), the admin email, locale/currency, and the ACME settings. The load balancer
certificate variables (`cs_lb_acme_dns_*`) are described in
[lb-certificates.md](lb-certificates.md).

`secrets.yml` carries everything else. Generate the immutable pair **once**:

```bash
openssl rand -hex 64   # -> secret_key_base
openssl rand -hex 64   # -> user_auth_secret
```

and then fill in `cs_admin_password`, `node_enrollment_token`,
`postgres_password`, `prometheus_basic_auth_password`,
`loki_basic_auth_password`, `backups_key`, and, if you use them, the load balancer DNS credentials
(`cs_lb_acme_dns_env`, see [lb-certificates.md](lb-certificates.md)).
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

* **Tailscale.** Set `tailscale_enabled: true` (in `group_vars/all` for every
  host, or per host or group) and those hosts join a tailnet; the controller
  then dials each node's agent over it and prometheus scrapes over it,
  instead of sending an admin Bearer token and exporter scrapes in cleartext
  across the public internet. The first run that joins a host needs a
  short-lived `tailscale_authkey` in `secrets.yml`; once a host is logged in
  no run needs a key again, so let it expire (roles/tailscale/README.md,
  "Joining once"). Leave a same-L2 region off with `tailscale_enabled: false`
  on the host or group, or keep it
  on the tailnet for operator access only with `tailscale_access_only: true`
  (control-plane traffic then stays on `primary_ip`). Without a
  tailnet, a remote region's control-plane traffic is cleartext — a supported
  choice, but an explicit one.
* **Ubuntu Pro.** Set `ubuntu_pro_token` for livepatch.
* **Backup export to S3** ("download backup"). Set `cs_agent_s3_export_bucket`,
  `cs_agent_s3_export_endpoint` (omit for AWS), `cs_agent_s3_export_region`,
  and `s3_export_access_key` / `s3_export_secret_key` in `secrets.yml`; every
  other `cs_agent_s3_export_*` knob defaults to cs-agent's own value. They are
  read per node, so a region that exports to its own endpoint sets them in
  `group_vars/region_<name>.yml` (vault that file if it holds the keys) or in
  a node's `host_vars`. Unset, export stays off. The exported archive is
  plaintext: keep the bucket private with an object-expiry lifecycle rule.
  `inventories/example/group_vars/all/main.yml` lists every knob, and
  [roles/cs_agent/README.md](../roles/cs_agent/README.md#backup-export-to-s3)
  what preflight checks.
* **`uplink_interface`** on any node with a global IPv6 address. Docker turns
  on IPv6 forwarding the first time it creates an IPv6 network, which
  withdraws the RA-learned default route about half an hour later; the fix
  needs the interface name and the playbook will not guess it.
* **`public_ipv6`** on any node whose load balancer answers on IPv6 too: that
  az's public IPv6 address, quoted, in canonical form (`"2001:db8:1::10"`;
  preflight checks it). The seed adds it to the load balancer as a public
  address; publish it as the apex AAAA of `app_domain` at your DNS host.
  **Set it whenever `app_domain` has an AAAA**: the controller
  requires every A and AAAA at the apex to be one of the load balancer's
  public addresses, and marks the domain INVALID otherwise (see §3).
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
manifest, enrolls every node and runs the backfills. Watch for these
checkpoints:

1. **Preflight** — fails immediately on a host ansible cannot reach (naming
   every one, with its address, port and ssh's error), a missing node var, a duplicate `az`,
   a short `secret_key_base`, a non-Ubuntu host or a node data disk that is
   not blank. Nothing has been changed at
   that point.
2. **Controller infrastructure** — vault initializes and issues the docker
   PKI. Nothing to do by hand: a converge unseals a sealed vault on its way
   past, and `playbooks/unseal.yml` does only that if you need it sooner.
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
* Copy the vaulted `secrets.yml` off the controller.
* Schedule `cstacks database-backup` (see the README) and copy its output
  off-host. Tenant volumes go to the borg server; the controller's own
  database does not.
* **Lock down ssh.** Every host's firewall accepts ssh from anywhere until
  you narrow it; the run ends by naming the hosts still open. Set
  `firewall_allow_ssh_from` to the machines you run ansible from (the
  controller and the nodes are accepted where they need to be without being
  listed), preview with `make lock-ssh ENV=<name> ARGS='--check --diff'`,
  then run `make lock-ssh ENV=<name>`. It refuses a list that would lock this
  run out, rolls back any host it cannot reach again, and ends by checking
  the controller's and the nodes' ssh. See the README's "Restricting ssh".

## Known rough edges on Ubuntu 26.04

Two package sources need explaining, and both fail loudly rather than
silently:

* **Docker's apt repository** is keyed on the release codename, which
  `geerlingguy.docker` takes from `ansible_facts['distribution_release']`.
  On 26.04 that is `resolute`, and
  `download.docker.com/linux/ubuntu/dists/resolute/` exists, so nothing
  needs overriding. The role has no suite override; the
  `docker_add_repo: false` example commented in `group_vars/all/main.yml` is
  there only for a future release Docker has not published a suite for, with
  the source then written by hand. If you ever do that, change the pinned
  versions in `versions.yml` with it, because those version strings name the
  `resolute` suite.
* **PostgreSQL is pinned at major 18 and comes from PGDG, not the Ubuntu
  archive**, even though 26.04 also ships 18. `roles/postgres_repo`
  configures the PGDG repository (`apt.postgresql.org`, `resolute-pgdg`
  suite) with an apt preferences pin and runs before `geerlingguy.postgresql`
  on the controller, the only place a postgres package is installed. The archive only ever carries
  its one default major; PGDG carries every supported one, so the fleet can
  move to a later major within this LTS by changing `postgresql_version`, once
  the controller is validated against it. Moving an existing host needs a
  `pg_upgrade` the provisioner does not perform. `playbooks/vars/Ubuntu-26.yml`
  builds every package name and path from `postgresql_version`, and supplies
  the `Ubuntu-26` platform vars `geerlingguy.postgresql` ships no copy of;
  `include_vars` picks it up from the playbook directory, so no fork or patch
  is needed.

The `docker_apt_packages` and `node_exporter_apt_version` entries in
`versions.yml` both carry exact apt versions, verified against the live
indexes on the date noted there. Both packages are also `apt-mark hold`-ed
after install, so they cannot move on their own; the holds are released
before each install, so bumping a pin still rolls through on a rerun.
