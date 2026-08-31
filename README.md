# ComputeStacks provisioner

Ansible playbooks that install and maintain a complete [ComputeStacks](https://www.computestacks.com)
environment: the controller, the metrics and logging stack, the backup server,
the container registry host, authoritative DNS, and one or more regions of
container nodes.

Two entry points:

| Playbook | Use it for |
| --- | --- |
| `playbooks/site.yml` | A whole environment, from bare Ubuntu 26.04 hosts. |
| `playbooks/add-region.yml` | A new region/AZ/node attached to an environment that already exists. |

Both are convergent for host configuration, images and package versions:
re-running reconciles them rather than skipping work it thinks is already
done. Everything externally sourced is pinned in
`playbooks/group_vars/all/versions.yml`, and a bump there rolls out on the
next run. The controller's own database is the one exception: the bootstrap
manifest only creates rows that are missing, so an existing region, node,
load balancer or setting is never rewritten by a rerun, and a manifest that
disagrees with the database is reported as drift, not applied. The one carve-out
is paired credentials the provisioner owns on both ends (metric/log client
basic-auth, DNS API keys, the load balancer's shared certificate and stats
password): rotating those in the vault DOES converge the controller's copy on
the next run, because the server side of each pair converges from the same
variables. See [`roles/controller_seed/README.md`](roles/controller_seed/README.md).

Start with [`docs/install.md`](docs/install.md) for a step-by-step greenfield
install. This file is the reference.

## Requirements

**Control machine.** ansible-core 2.19 or newer, plus the `cryptography`
python library — several roles inspect certificates with
`community.crypto`, which runs on the control machine rather than on the
managed hosts so the fleet needs no python crypto libraries.

```bash
ansible-galaxy role install -r requirements.yml -p galaxy_roles
ansible-galaxy collection install -r requirements.yml -p collections
```

`galaxy_roles/` and `collections/` are gitignored; `ansible.cfg` already
points `roles_path` and `collections_path` at them. Re-run both commands after
a `requirements.yml` bump.

The `Makefile` is a thin wrapper around exactly these commands and the
playbook runs below — `make deps`, `make site ENV=prod`,
`make add-region ENV=prod`, `make validate ENV=prod`, `make lint`,
`make check`. Every playbook target requires `ENV` (there is no default
inventory) and honours `LIMIT=`, `ARGS=` and `VAULT=`; `make help` lists them.
Nothing in this README depends on it.

**Managed hosts.** Ubuntu 26.04 LTS, amd64, with root SSH from the control
machine and a resolvable, unique, single-word hostname (`node101`, not
`node101.example.com`). Nothing else — the playbooks install their own
prerequisites. Hosts in an *existing* v1 environment are the exception: they
are Debian, they are flagged `existing_env: true`, and attach mode only ever
adds to them.

**DNS.** The portal, metrics and registry domains must resolve to their hosts
before the run, because the TLS certificates are issued during it:

```
portal.example.com.       IN A     <controller public ip>
metrics.example.com.      IN A     <metrics public ip>
cr.example.com.           IN A     <registry public ip>
usercontent.example.com.  IN NS    ns1.example.com.
```

## Inventory

One inventory describes the whole install — every region, every shared host.
Copy the example and edit it:

```bash
cp -r inventories/example inventories/prod
```

* `hosts.yml` — the groups (`controller`, `metrics`, `backup`, `registry`,
  `nameservers` with `ns_primary`/`ns_followers`, `nodes`) and the host vars.
  Every node needs `hostname`, `primary_ip`, `public_ip`, `region`, `az`,
  `container_network` and `container_network_name`; preflight asserts all of
  them. `region` becomes a controller *Location*, `az` becomes a controller
  *Region*, and there is exactly one node per `az`.
* `group_vars/all/main.yml` — domains, locale, currency, DNS driver, ACME
  settings.
* `group_vars/all/secrets.yml` — everything secret. Encrypt it (below).
* `constructed.yml` — generates `region_<name>` and `az_<name>` groups from
  the node host vars, so `--limit region_exm001` works with no maintained
  nesting.

Templates read **inventory vars only**, never gathered facts. That is what
makes `--limit` safe: a partial run still renders the full picture, instead of
quietly dropping the regions it did not target out of the prometheus
configuration or the firewall.

The one sanctioned exception is a node's **tailscale address**, which cannot be
an inventory var because tailscale assigns it (docs/contracts.md §Facts
exception). It is read from a fact the `tailscale` role persists on each host,
and hosts outside a `--limit` supply it from the on-disk fact cache
(`.ansible_facts_cache/`, gitignored). So from a **fresh clone the first run
must be un-limited** — the cache is empty, and a `--limit`ed first run renders
every un-targeted tailnet node as if it had no tailnet address. After one full
converge, `--limit` is safe again.

### Secrets

```bash
ansible-vault encrypt inventories/prod/group_vars/all/secrets.yml
ansible-playbook -i inventories/prod playbooks/site.yml --ask-vault-pass
```

Generate the two immutable values once, before the first run:

```bash
openssl rand -hex 64   # secret_key_base
openssl rand -hex 64   # user_auth_secret
```

> **`secret_key_base` and `user_auth_secret` can never be changed.** Every
> encrypted column in the controller — agent tokens, DNS API keys, load
> balancer certificates — is keyed off `secret_key_base`, and
> `Secret.decrypt!` returns `nil` on a mismatch rather than raising. Rotating
> it does not lock you out; it silently empties every credential the
> controller holds, and the symptoms appear days later as failing DNS,
> unreachable agents and broken TLS. **Rotation is a reinstall.** These
> playbooks never generate either value (v1 did, when they were blank) and
> preflight fails on a blank or short one.

## Running an install

```bash
ansible-playbook -i inventories/prod playbooks/site.yml --ask-vault-pass
```

The play order is a contract, not a convenience (`docs/contracts.md`):

```
preflight -> base system -> tailscale -> nameservers -> docker
  -> controller (vault, postgres, redis, nginx/acme, portal)
  -> node docker TLS -> metrics -> registry -> backup -> node base stack
  -> firewalls -> ssh trust
  -> controller_seed        creates the Node rows and mints the agent tokens
  -> cs_agent on the nodes  installs and enrols against those rows
  -> controller_post_enroll datachannel + metadata backfills
  -> validate
```

Seeding has to precede enrolment (the token does not exist before it), and the
backfills have to follow it (they call every node's agent, which rejects an
unenrolled node). Re-ordering those three breaks the install.

Useful flags: `--limit region_exm001` for one region, `--check` for a dry run
(with the usual check-mode caveats around anything driven by a command's
output), `--tags enroll` to re-enrol a node on its own.

## Validating

The last play asserts the things that otherwise fail silently: services and
containers up, the controller reaching each node's agent, the prometheus
label contract answering the controller's own placement query, borg reachable
from every node, the portal reachable from every node, root SSH from the
controller, and the tenant zone resolving on every nameserver. Run it on its
own at any time:

```bash
ansible-playbook -i inventories/prod playbooks/site.yml --tags validate
```

See [`roles/validate/README.md`](roles/validate/README.md) for the full check
list, what each failure means, and how to skip one.

## Adding a region

For a v2 environment, add the node to the same inventory and re-run
`site.yml` — optionally `--limit` scoped, which is safe once the fact cache
has been populated by one un-limited run (see "Templates read inventory vars
only", above).

For an environment built by the **v1** playbooks, use attach mode:

```bash
ansible-playbook -i inventories/prod playbooks/add-region.yml --ask-vault-pass
```

It builds the new node in full and touches the existing shared hosts only
additively — a prometheus `file_sd` fragment, firewall appends, a borg
authorized_keys entry, and two appended environment keys on the controller. It
never re-renders a file on a host it does not fully describe. Read
[`docs/attach-mode.md`](docs/attach-mode.md) first: it has prerequisites,
including a controller already upgraded to a release carrying
`rake bootstrap:apply`, and it restarts the portal.

## Upgrades

**Anything pinned in `versions.yml`** — container images, the cs-agent
package, borg, acme.sh, prometheus, loki — is upgraded by editing that file
and re-running the playbook. The roles are convergent: a changed pin
re-renders the unit or re-installs the package and the handler restarts the
service. Nothing floats, so nothing moves on its own.

Two pins move together and are marked as such in `versions.yml`:
`borg_version` (the server binary) with `borg_image` (the client container),
and `loki_image` with `fluentd_loki_image`.

**The controller** is pinned by *minor* tag (`controller_image_tag: "9.7"`),
which is a deliberate rolling channel: patch releases inside the line are
picked up by

```bash
cstacks upgrade      # on the controller: pg_dump, pull, migrate, restart
```

which the `controller` role also runs for you when the running container's
image no longer matches the configured tag. A new minor or major line is an
edit to `versions.yml` and a re-run. Read the controller's own release notes
first; this playbook does not know what a given release needs.

**The playbooks themselves**: pull, re-install the galaxy dependencies, re-run
`site.yml`.

## Controller database backup and restore

`cstacks database-backup` writes a gzipped `pg_dump` of the controller
database to `/var/lib/computestacks/backups/`, connecting as the peer-auth
`root` superuser over the unix socket. `cstacks upgrade` runs it first, before
it pulls or migrates anything, so a failed dump aborts the upgrade instead of
leaving you mid-migration with no copy.

This is the *controller's own* database. Tenant volumes are backed up
separately by cs-agent to the borg server; nothing else backs up the
controller. Schedule it — a systemd timer or a cron entry calling
`cstacks database-backup` daily, with the output directory copied off-host —
and keep the dumps somewhere that survives the controller.

To restore:

```bash
cstacks stop
# as root, on the controller:
dropdb cloudportal && createdb -O computestacks cloudportal
zcat /var/lib/computestacks/backups/cloudportal-<stamp>.sql.gz | psql cloudportal
cstacks run
```

A restore is only usable with the **same** `secret_key_base` the dump was
taken under. Keep the vaulted `secrets.yml` with the backups; the dump alone
is not a recoverable environment.

## Repository layout

```
ansible.cfg                     roles_path, collections_path, ssh settings
Makefile                        thin wrapper: deps, site, add-region, validate, lint, check
requirements.yml                pinned galaxy roles and collections
playbooks/site.yml              greenfield converge
playbooks/add-region.yml        attach mode
playbooks/group_vars/all/       versions.yml (pins) and ports.yml (the ports contract)
playbooks/vars/                 platform vars a pinned galaxy role is missing
inventories/example/            copy this
roles/                          one README.md per role: what it owns and why
tests/roles.yml                 per-role syntax harness
docs/
```

* [`docs/install.md`](docs/install.md) — step-by-step greenfield install.
* [`docs/attach-mode.md`](docs/attach-mode.md) — adding a region to an
  existing v1 environment.
* [`docs/acme-providers.md`](docs/acme-providers.md) — ACME challenge methods
  and the DNS-01 provider matrix.
* [`docs/control-plane.md`](docs/control-plane.md) — who dials whom, on what
  address, with what credential. The 19-row graph `ports.yml`, the firewall
  rules and the play order are all derived from.
* [`docs/contracts.md`](docs/contracts.md) — the rules every role obeys:
  ownership, play ordering, the ports and prometheus contracts, attach-mode
  limits. Read this before changing anything.

Each role's `README.md` documents what it owns, its variables, and how it
differs from the v1 role it replaces. They are the reference for behaviour;
this file is the reference for running the thing.
