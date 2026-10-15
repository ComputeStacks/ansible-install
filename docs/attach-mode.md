# Attach mode: a new region against an existing environment

`playbooks/add-region.yml` provisions a new region, availability zone and node
against an environment that already exists — classically one built by the v1
playbooks, on Debian, with a containerized metrics stack and an iptables
firewall.

**An environment this repository built works too.** `existing_env` means "not
mine to rebuild, and this inventory is a partial view of the fleet" — it does
not mean "v1". The two differ in exactly one place, the host firewall, and the
role picks the path by looking for v1's script rather than by assuming: see the
firewall rows in the write set below.

It builds the new node in full, exactly as `site.yml` would. On the existing
shared hosts it performs **only** the write set enumerated below, and nothing
else. That restriction is not politeness: in attach mode the inventory is a
*partial view of the fleet*, so any file rendered whole from it would silently
drop every region this inventory does not happen to list — the prometheus
scrape configuration and the firewall are the two that would take a running
environment down.

If you are extending a **v2** environment, you do not need this playbook. Add
the node to the same inventory and re-run `site.yml`, un-limited — a new
region needs the controller, its site's metrics host and the shared hosts'
firewalls, which a `--limit region_<name>` would skip (see the README,
"Adding a region").

## Prerequisites

**The existing controller must already be upgraded** to a release that carries
`rake bootstrap:apply` — the manifest apply this playbook seeds through — and
accepts `link_regions: new`, i.e. **9.7.10 or later**. An older apply rejects
`new` up front, before anything is written. Do
that first, the normal way:

```bash
cstacks upgrade
```

The controller prep play checks this before it changes anything: it reads the
running portal container's rake task list and aborts if `bootstrap:apply` is
not in it. That check exists because the failure used to arrive at the worst
possible moment — after the database dump, the environment append, the
`cstacks` swap, the portal restart *and* a fully built new node, with
`cstacks seed` dying on `Don't know how to build task 'bootstrap:apply'`. It
also asserts that every directory the v2 `cstacks` script bind-mounts already
exists, so a key missing from `/etc/default/computestacks` cannot quietly take
v2's default and have docker mount an empty directory over a populated one.
Both checks are read-only, and both run before the `pg_dump`.

**`secret_key_base` in your vaulted `secrets.yml` must be the existing
controller's value, byte for byte.** Copy it out of
`/etc/default/computestacks` on the controller. Never the other way around:
the manifest apply encrypts with this value, and applying under a different
one writes credentials the running controller decrypts to `nil` — silent,
total credential loss that a `pg_dump` does not protect you from, because the
damage happens after the dump. The controller prep play asserts the match and
aborts if it fails.

**These inputs describe the existing backup server** and have no safe default,
because v2's defaults are not what a v1 server has (the server itself is the
one in the new node's `site` — see below):

| Var | v1 value | Why it matters |
| --- | --- | --- |
| `backup_host_path` | `/mnt` | Where repositories live on that server. |
| `backup_borg_remote_path` | `/usr/bin/borg` | v2's default is `/opt/computestacks/bin/borg`, which does not exist there — every backup on the new node would fail with "command not found". |
| `ssh_port` (on the backup host) | that server's sshd port | Defaults to `cs_ports.ssh` (22). A server whose sshd listens elsewhere needs it, or every backup on the new node fails to connect. `backup_ssh_port` still overrides it, for a port that differs from sshd's own (a forward in between). |
| `backups_key` (secrets) | the environment's value, or the node's own | The borg passphrase for this node's repositories. Each node may have its own (a host var, e.g. a per-node vaulted file); it must never change once the node has backups, or its existing repositories become unreadable. |

**Everything else in the inventory must describe the environment that
exists, not a new one.** Attach mode never re-renders a shared host's
configuration, so a value that disagrees with the running environment is not
corrected — it just fails, usually somewhere unhelpful:

* `cs_portal_domain`, `cs_registry_domain` and `cs_app_zone` are the existing
  environment's domains.
* **The metrics and logging endpoint is per site.** The metric and log clients
  are matched by exact endpoint string, built from the metrics host's own
  `metrics_domain` host var — falling back to the environment-wide
  `cs_metrics_domain` when it sets none — and `cs_ports.metrics_*`. An
  environment with one metrics host needs only `cs_metrics_domain`, exactly as
  before. An environment with several needs `site` on the node being attached
  and on each metrics and backup host in the inventory, and `metrics_domain`
  on every metrics host that does not answer on `cs_metrics_domain`. Getting
  the site wrong points the new region at another facility's prometheus and
  loki, which is not an error anywhere — it is just the wrong data in the
  wrong place.
* **The nginx basic-auth credentials are per metrics host.** The htpasswd
  files on the existing host are not rewritten, so a fresh password means the
  new node's fluentd cannot ship logs and the controller cannot read metrics —
  and a wrong *username* fails identically, silently, with a 401. Read both
  off the existing host's nginx configuration. `prometheus_basic_auth_password`
  and `loki_basic_auth_password` are the environment-wide values; where two
  metrics hosts do not share credentials — and separately built ones do not —
  name the site in `metrics_site_credentials` instead
  (`inventories/example/group_vars/all/secrets.yml` shows both forms). A v1
  metrics host has ONE password covering both vhosts, which is what the
  `username`/`password` shorthand is for.
* `cs_admin_email` / `cs_admin_password` are still asserted present even
  though an attach manifest carries no `admin_user` section. Any valid value
  will do; the existing admin account is not touched.

**The inventory for an attach run holds the existing SHARED hosts and the one
new node — and no other node.** An already-provisioned node must not appear at
all: preflight stops the run with

```
node1001 is in `nodes` and flagged `existing_env: true`. Attach mode builds
the new node from bare metal; an already-provisioned node does not belong in
this run.
```

and leaving it unflagged is worse, because it would then be in `cs_new_nodes`
and this playbook would rebuild it from bare metal. So keep a separate
inventory directory for attach runs, or drop the other nodes from the copy you
run this with. Everything the new region needs from its siblings comes from
the controller, not from their inventory entries.

**The new node needs a second, blank disk named by `docker_data_device`,**
exactly as a greenfield node does ([requirements.md](requirements.md#node-disk-layout)).

**Give every host whose sshd is not on 22 its `ssh_port`** — existing and new
alike. The new node's firewall opens it, the controller's Node row for the new
node carries it, and the new node's backups dial the backup host's. The
controller's `/root/.ssh/config` is not re-rendered in attach mode, so it gets
no block for the new node; the portal dials nodes on the Node row's port and
does not need one, but add a block by hand if you ssh there from the
controller. Nothing
edits `sshd_config`: move the new node's sshd yourself before the run, and
keep `ansible_port` in step (the example inventory derives it from
`ssh_port`).

**Backup export to S3 is per node.** If the environment offers "download
backup", give the new node its region's export settings —
`cs_agent_s3_export_bucket`, `_endpoint`, `_region` and the vaulted
`s3_export_access_key` / `s3_export_secret_key` — in
`group_vars/region_<name>.yml` or the node's `host_vars`. Without a bucket the
new node's agent simply has export off. Preflight checks the settings before
anything changes ([roles/cs_agent/README.md](../roles/cs_agent/README.md#backup-export-to-s3)).

**Flag every existing shared host** — controller, metrics, backup, registry,
nameservers:

```yaml
controller:
  hosts:
    ctl1:
      ansible_host: 192.0.2.10
      hostname: ctl1
      primary_ip: 10.100.0.10
      public_ip: 192.0.2.10
      existing_env: true
```

The playbook asserts this: an unflagged shared host stops the run with a
pointer to `site.yml`. The new node must **not** be flagged — that flag is
exactly what `cs_new_nodes` subtracts to work out which node this run builds.

**Tailscale is off by default.** A host is on the tailnet only with
`tailscale_enabled: true` (docs/contracts.md §Tailnet membership). An existing
v1 controller and metrics host are not on a tailnet, and joining them is an
operator action taken *before* this playbook runs. Any host that carries a
`tailscale_authkey` must also declare `tailscale_enabled`, or preflight
refuses the run.

To put the new node on the tailnet, set `tailscale_enabled: true` on it and
give **this run** a short-lived auth key for its first join — for example a
vaulted per-node file, `host_vars/<node>/tailscale.yml`:

```yaml
tailscale_authkey: !vault |
  $ANSIBLE_VAULT;1.1;AES256
  ...
```

The key is used once, to join the node while it is not logged in to the
tailnet, and never again: a later run (a re-run of this playbook, a
`site.yml` converge, `maintain-nodes.yml`) finds the node logged in, never
runs `tailscale up` on it, and needs no key — remove the file once the node
has joined, or leave the expired key there; it is never read. A node already
logged in whose tailscale settings differ from the inventory's flags is
refused, not re-joined (roles/tailscale/README.md, "Joining once").

## What it writes on the existing hosts

Exactly this, and nothing else:

| Host | Write | Notes |
| --- | --- | --- |
| controller | *(nothing)* the `bootstrap:apply` probe and the bind-mount path assertions | Read-only gates, before the dump. See Prerequisites. |
| controller | `cstacks database-backup` | The gate. Everything after it writes to the database. |
| controller | `NODE_ENROLLMENT_TOKEN` and `CS_PROXY_IPS_PATH` appended to `/etc/default/computestacks` | `lineinfile`, append-only, only when the key is absent. No `regexp`, so an existing value can never be rewritten. |
| controller | the v2 `cstacks` script | One of the two whole-file exceptions. It holds no environment-specific values and adds the `seed`, `runner` and `database-backup` subcommands plus the proxy_ips mount. |
| controller | portal container recreated | ~30 seconds of downtime — see below. |
| controller | firewall: one accept per new node address | **v1-built host:** v1's own `lineinfile` idiom against `/usr/local/bin/cs-recover_iptables`, plus the same rules made live. **v2-built host:** one `add rule` line per address appended to `/etc/nftables.d/cs-peers.nft`, which `cs-firewall.service` loads straight after `cs-static.nft`. Neither path re-renders anything. |
| metrics | `/etc/prometheus/{node_exporter,cadvisor,haproxy}/<az>.yml` | New per-AZ fragment files, in v1's exact paths, on **the metrics host in the new node's site** and no other. Prometheus picks them up within one scrape interval; nothing is restarted. The other whole-file exception. |
| metrics | firewall: the same node-address appends | |
| backup | the `cstacks` account's `~/.ssh` | On **the backup server in the new node's site**. The repository path is only `stat`ed, never chowned — v1's value is `/mnt`, and 0770 on it would clear world traverse for every unrelated process reading a filesystem mounted underneath. The existing account's shell and shadow entry are left alone. v2's borg is **not** installed over the server's existing one. |
| backup | one `authorized_keys` entry for the new node | Added by `cs_agent`, commented with the node's hostname. |
| backup | firewall: the same node-address appends | Latent on most v1 servers — their script's `default_allow_ssh` accepts 22 from anywhere, so borg already reaches them. On a server built with `default_allow_ssh: false` the appends are what keeps the new node's backups from failing silently. |
| controller | the new az's `public_ipv6` as a `public` LoadBalancerAddr, through `cstacks runner` | Only for a new node that sets `public_ipv6`, after the seed apply, on the new az's load balancer only. Additive; an address already on it under another role fails the run before the apply. |
| nameserver (`ns_primary`) | the new az's load balancer records in `cs_app_zone`: `<app_domain>` A (and AAAA, when the node sets `public_ipv6`), `*.<app_domain>` CNAME and the `_cs-lb.<app_domain>` TXT ownership marker | Only with `dns_driver: powerdns` and `powerdns_manage_lb_records` not turned off on the nameservers. Written with `pdnsutil` (PowerDNS 4.x and 5.x alike), for the new region's names only. Never overwrites a record it does not own: a conflict at any of those names fails the run before anything is written. The followers receive them through replication; nothing is written on them. |
| vault (on the controller) | nothing | The new node's docker certificate is *issued* from the existing PKI; the playbook unseals the vault if it is sealed and writes nothing. |

`/etc/default/computestacks` is append-only, always. No shared-host file is
ever re-rendered.

### The portal restart

Appending the two environment keys and installing the v2 `cstacks` script both
notify a portal recreate, and the prep play flushes that handler immediately —
`controller_seed` runs next and needs a portal that carries
`NODE_ENROLLMENT_TOKEN` and the proxy_ips mount. The portal is down for
roughly the time `docker run` takes, usually under a minute. **Schedule the
run accordingly**: tenant sites keep serving (they run on the nodes), but the
portal, the API and the job queues are unavailable while it restarts.

## Running it

```bash
ansible-playbook -i inventories/prod playbooks/add-region.yml --ask-vault-pass
```

The run stops and waits once, on purpose. `controller_seed` applies the
manifest with `DRY_RUN=1` first, prints what it would create plus any drift
warnings, and pauses for you to read it. That is the last point before
anything is written to the controller's database.

**On an attach run the `DRY_RUN` pass cannot be switched off.** It is the
input to gate G, which fails the run when the preview reports drift on a load
balancer's `domain` — the apply rotates a load balancer's certificate on an
existing row but never rewrites its domain, so a manifest that disagrees would
hand a live load balancer a certificate whose CN no longer matches the name it
still serves. A gate whose input never ran passes without checking anything,
so no output flag is allowed to skip the pass. `DRY_RUN=1` writes nothing.

The two opt-outs govern only the human-facing half:
`controller_seed_confirm: false` skips the prompt in CI (the `pause` module
needs a tty), and `controller_seed_dry_run_first: false` stops the preview
being PRINTED. Neither disables the gate.

### Two-step attach

For a run with no terminal to pause on (CI), the confirmation splits into two
runs of the same playbook, with a human reading the preview in between:

```bash
# Phase 1: build, preview, stop. Nothing is applied to the controller database.
ansible-playbook -i inventories/prod playbooks/add-region.yml \
  -e controller_seed_stop_after_preview=true -e controller_seed_confirm=false

# Phase 2: the full run again, applying only the preview that was reviewed.
ansible-playbook -i inventories/prod playbooks/add-region.yml \
  -e controller_seed_expect_preview_sha256=<fingerprint> -e controller_seed_confirm=false
```

**Phase 1** runs every play up to and including the seed's `DRY_RUN` preview
and its read-only gates (load balancer domain drift, the `public_ipv6` plan),
prints the preview and a line `controller_seed_preview_sha256=<64 hex>`,
deletes the manifest from the controller, and ends green: no `cstacks seed`
apply, no agent enrollment, no post-enrollment tasks, no validation. Every
play after the seed starts with a guard that ends it, and the run's last
message says it stopped. A wrapper reads the fingerprint with
`grep -oE 'controller_seed_preview_sha256=[0-9a-f]{64}'`.

What phase 1 leaves in place is everything before the seed: the new node
built, the portal recreated (if the environment file or `cstacks` changed), a
fresh `pg_dump`, and the additive writes on the shared hosts in the table
above — the nameserver's load balancer records, the firewall appends, the
prometheus fragments and the controller's ssh trust for the node. None of them
is an apply, and every one of them is a no-op when run again.

**A rejected preview** is fixed in the inventory (or, for drift, in the admin
UI) and phase 1 is run again; it prints a new preview and a new fingerprint.

**Phase 2** is a full re-run. Everything before the seed converges to what
phase 1 left (the portal is recreated only by a handler, which fires only
when the environment append or the `cstacks` install changes something; a
second `pg_dump` is taken). The seed previews again, and applies only when
that preview's fingerprint equals `controller_seed_expect_preview_sha256`;
otherwise it fails before the apply, printing both, with nothing applied —
the database or the inventory changed since the review, so phase 1 has to be
run and read again. The fingerprint is a sha256 of the preview with only the
ids of rows the rolled-back preview creates normalized
(`roles/controller_seed/README.md`, "Two-step attach").

Each new az also needs its own `app_domain` and its own pair of public DNS
records — `<app_domain>` A to that node's public IP and `*.<app_domain>`
**CNAME** to `<app_domain>` (docs/install.md §3). If the new az answers on
IPv6 too, set its `public_ipv6`: the seed adds it to the new load balancer as
a public address (a `cstacks runner` step after the apply, planned before the
confirm prompt), and with `dns_driver: powerdns` it is written as the apex
AAAA. An AAAA on `<app_domain>` that is not one of the load balancer's public
addresses makes the controller mark the domain INVALID. `roles/preflight` requires
the new node's `app_domain` to be set and to differ from `cs_app_zone`, so a
new az never lands on the zone apex an existing region already answers on.

**With `dns_driver: powerdns` the playbook writes that pair itself**, on the
existing `ns_primary`, in a play that runs straight after preflight — see the
nameserver row above. A hand-made pair that already matches is adopted; a name
that holds anything else (records that differ, or a marker naming another
node) stops the run before any write, and the message says how to resolve it:
a different `app_domain`, or deleting the stale records by hand. Preflight
does not check the zone's delegation in attach mode — it is already live — so
it is first checked at the end of the run, by `roles/validate`'s `dns` (every
nameserver) and `lb_domain_public` (public resolution), and then `lb_domain`
reads the controller's verdict back.

This needs the existing nameservers in the inventory in the same shape
`site.yml` uses — a `nameservers` group with `ns_primary` and `ns_followers`
children, each host flagged `existing_env: true`, and cs_app_zone's zone
present on the leader (the play fails, naming the zone, if it is not). **If
the inventory has no `ns_primary`**, that play has no hosts and writes
nothing, and the run treats the records as yours, exactly as for `dns_driver:
none`: create the pair by hand before the run, and preflight digs for it in
public DNS and fails if it is missing. The same holds with
`powerdns_manage_lb_records: false` set on the nameservers.

The attach manifest carries **topology only**: the new location, region, node,
network and load balancer, plus the user-group region link without which the
new region is invisible to every existing user. That link is `link_regions:
new`: the default group is linked to the regions this run creates and to no
existing one, so a region deliberately kept off the default group stays off. It carries no settings, no DNS
driver, no products and no admin user, so nothing global is touched.
`controller_seed_full_manifest: true` renders the whole greenfield document
against the live controller instead. Almost nothing in it is an overwrite
there — settings are seeded only while still unconfigured, the DNS driver is
never reconfigured once it exists, and a difference from the manifest surfaces
as a drift warning rather than being applied. The exception is paired
credentials (metric/log client basic-auth, DNS `api_key`/`api_secret`, the LB
shared certificate and stats password): those converge to the manifest's
values, reported as `[rotate]` lines — which is why the secrets in your vault
MUST be the existing environment's values, not fresh ones. The DRY_RUN gate
previews any rotations before they land. Do it deliberately, if only for the
fuller drift report, or not at all.

The regions still reference the existing metric and log clients **by exact
endpoint string**, and a miss aborts the apply rather than creating a second
client. That is wanted: a duplicate client with different credentials splits
the placement metrics and every order in the new region is rejected for "no
capacity" with nothing else to show for it. If it aborts, reconcile the endpoint the manifest built — from that site's
`metrics_domain` or `cs_metrics_domain`, and `cs_ports.metrics_*` — with what
the controller actually has.

## After the run

`validate` runs against the controller and the new node only — the existing
metrics and backup hosts were not built by these playbooks and their unit set
is not this repository's to assert. To re-run just the checks later, use
`add-region.yml --tags validate` (`make add-region-validate ENV=<name>`), not
`site.yml`. Two of its checks matter most here:

* **borg version parity.** The client half of every backup is the
  `cs-docker-borg` image; the server half is whatever borg the existing
  server has. The check compares them and fails on a mismatch, because a
  mismatch corrupts repositories in ways that only surface at restore time.
  Fix the pair rather than skipping the check;
  `validate_borg_version_compare: false` downgrades it to a warning if you
  are knowingly running a mixed fleet.
* **the prometheus label contract.** It runs the controller's own placement
  query against the new node's labels. An empty result is the failure that
  rejects every order in the new region while every dashboard stays green.

Then confirm in the portal that the new Location/Region/Node exist and the
node reports capacity.

## What attach mode is not

It does not migrate an existing environment to v2. The existing hosts keep
their v1 configuration, their v1 firewall and their v1 packages; only the
`cstacks` script and the two appended environment keys change, and both are
additive. Converging old hosts onto the v2 roles is out of scope —
`site.yml` against a v1 host would re-render files it does not fully
describe, which is exactly what the flag exists to prevent. What differs
between the v1 roles and these is listed in
[migrating-from-v1.md](migrating-from-v1.md).
