# powerdns

**Owner: Wave 3H.** Port of v1's `powerdns` role.

## Purpose

Runs PowerDNS authoritative server (`pdns-server` + `pdns-backend-pgsql`) on
the `nameservers` inventory group, with a PostgreSQL 17 backend replicated
between the group's two sub-groups:

* `ns_primary` -- the writable **leader**. Runs the PowerDNS HTTP API/web
  server (`cs_ports.pdns_api`) and owns the writable postgres primary.
* `ns_followers` -- read replicas. Stream from the leader via postgres
  physical replication (`pg_basebackup` + WAL streaming); no API/webserver.

Both sub-groups serve DNS itself (`cs_ports.dns`, tcp+udp, dual-stack) --
PowerDNS answers queries from its local (replicated) copy of the zone data
regardless of leader/follower role.

## What it does, in order (`tasks/main.yml`)

1. Asserts `pdns_api_key` / `pdns_web_key` / `pdns_db_password` are set to
   real values (not blank, not the `CHANGEME` placeholder) and that
   `postgresql_version` is defined.
2. Installs and starts the distro `postgresql-{{ postgresql_version }}`
   package (`tasks/postgres.yml`).
3. **ns_primary only** (`tasks/leader.yml`): points postgres at its own
   inventory address and the ports-contract port, opens replication access to
   every host in `ns_followers` (iterated, not indexed), then creates the
   `pdns` database/role/schema and the replication role if they don't exist
   yet.
4. **ns_followers only** (`tasks/follower.yml`): if the local `pdns` database
   isn't reachable yet, stops postgres, wipes its data directory, and runs
   `pg_basebackup -R -C --slot=<this host>` against the leader to bootstrap
   streaming replication, then restarts postgres. This block is a one-time
   data bootstrap, not a "service already installed" guard -- see its file
   header for why that is not the convergent-role anti-pattern
   `docs/contracts.md` rule 3 forbids.

   It then rewrites `primary_conninfo`'s `host=` field to the leader's current
   `primary_ip` (in place, preserving everything `pg_basebackup` chose) and
   notifies a postgres **reload**. That part IS convergent and runs every time:
   `pg_basebackup -R` sets the leader address once, so without it a leader that
   changes address detaches every follower silently -- the replica keeps
   answering queries from the copy it already holds, which is why neither the
   converge nor an NS lookup notices. `roles/validate`'s `dns_replication`
   check reports that state; this is what stops it arising.
5. **Both roles** (`tasks/pdns.yml`): stops/disables `systemd-resolved` (it
   holds port 53), installs `pdns-server` + `pdns-backend-pgsql`, removes the
   default bind-backend files, renders the postgres-backend config and
   `pdns.conf`, starts/enables `pdns`, and verifies it can reach its database.
6. **ns_primary only** (`tasks/zone.yml`): `pdnsutil create-zone
   {{ cs_app_zone }} {{ powerdns_name }}` (rc 0 = created, rc 1 = already
   exists -- both are success).

## KEY DIFFERENCE from v1: credentials are inventory-sourced, not generated

v1 generated the API key, webserver key, and db password on the leader on
first converge, wrote them under `/root/.credentials/`, and every later
converge (and the follower) `slurp`'d them back from that leader-local
directory. This role **never generates or reads back a credential file**.
`pdns_api_key`, `pdns_web_key`, and `pdns_db_password` are required inputs
from the vaulted inventory (`inventories/<env>/group_vars/all/secrets.yml`,
already present as of Wave 0 -- see the sample file's `PowerDNS` section).
The role only *writes* them into `pdns.conf` / the postgres-backend config and
asserts they are set; there is no `tasks/passwords.yml` equivalent, no
`/root/.credentials`, and no `slurp`/`delegate_to` dance to move a
leader-generated secret to other hosts. This removes an entire class of
v1 "first converge race" concerns (e.g. a follower converging before the
leader has ever generated a credential file).

## The package-pin decision

v1 shipped `files/package-pin` (an apt preference pinning `pdns-*` to
`origin repo.powerdns.com`) that **no task ever copied anywhere** -- dead
weight. This role installs `pdns-server` / `pdns-backend-pgsql` /
`postgresql-{{ postgresql_version }}` straight from the Ubuntu 26.04 distro
archive (`state: present`, same pattern `roles/common` uses for base
packages) and does not add the upstream `repo.powerdns.com` apt repository at
all. Simpler, no dead file, no second apt source/GPG key to manage, and
`unattended-upgrades` (already installed by `common`) carries patch releases
the same way it does for every other distro package in this repo. If a wave
owner later needs a PowerDNS version newer than what 26.04 ships, wiring the
upstream repo is a follow-up, not a silent gap -- flagged here rather than
half-done.

## Postgres: inline install, not `geerlingguy.postgresql`

`geerlingguy.postgresql` (pinned in `requirements.yml`) is already used by
the `controller` host group. It was **not** reused here: this role's leader
side needs a hand-rolled `listen_addresses`/`pg_hba` replication opening and,
on the follower side, a `pg_basebackup` bootstrap -- none of which
`geerlingguy.postgresql` manages. Wiring the galaxy role in just for package
install + `postgresql.conf` templating and then hand-writing the replication
logic on top anyway (which is most of what this role does) is more moving
parts for the same result than a small inline `apt` + `lineinfile` +
`postgresql_pg_hba` sequence, matching v1's approach. `postgresql_version:
17` (`playbooks/group_vars/all/versions.yml`) is the same global pin the
controller's `geerlingguy.postgresql` instance uses -- both just mean "major
version 17"; they are unrelated running instances on different hosts.

## Deviations from v1

* **Credentials**: sourced from vaulted inventory, not generated/slurped (see
  above) -- the single biggest behavioral change from v1.
* **Package source**: distro archive instead of the (unused, now deleted)
  `repo.powerdns.com` pin file.
* **No install-state guards**: v1's `tasks/main.yml` branched on
  `ansible_facts.services['postgresql.service']`/`['pdns.service']` before
  even including its install tasks, and had a separate `apt: state=latest`
  task gated on the same facts. This role always runs `state: present` +
  re-templates config + `state: started, enabled: true`, convergently
  (`docs/contracts.md` rule 3). The only "only do this once" gate left is the
  `pg_basebackup` bootstrap, which is a data operation, not an install guard.
* **Multiple followers, not just one**: v1 hardcoded
  `groups['follower_nameservers'][0]` in two places (the leader's pg_hba
  source, and the follower's replication slot name `ns2`). This role loops
  `groups['ns_followers']` for pg_hba (`docs/contracts.md` rule 2 -- iterate,
  never index `[0]`) and derives each follower's replication slot name from
  its own `inventory_hostname`, so an environment with more than one follower
  works without modification.
* **`until`/`retries` actually retries**: v1's `postgresql_ping` checks set
  `retries`/`delay` without `until`, which is a Two-key no-op in Ansible --
  the task only ever runs once. This role adds `until:` on every such check.
* **Config files are group-readable only, not world-readable**: v1's
  `pdns.conf` and `pdns.local.gpgsql.conf` were written with the `template`
  module's default mode, leaving the API key/webserver password/db password
  world-readable. This role writes both `owner: root, group: pdns,
  mode: "0640"`.
* **`webserver-allow-from` is not `0.0.0.0/0`**: v1 opened the PowerDNS API to
  the entire internet (relying entirely on the host firewall). This role
  restricts it to the controller's inventory addresses
  (`groups['controller']`'s `primary_ip` and `public_ip`), matching the
  `cs_ports.pdns_api` contract comment ("controller -> primary nameserver").
  It also binds `webserver-address` to the leader's `primary_ip` rather than
  `0.0.0.0`, so the socket is not open on every interface for
  `webserver-allow-from` to then reject.
* **One unified `pdns.conf.j2`** instead of v1's two near-duplicate templates
  (`pdns.conf.j2` / `pdns-follower.conf.j2`); the api/webserver block is
  gated by `powerdns_is_leader` inside the one file.
* **IPv6 dual-stack listener carried forward**: `powerdns_dns_address`
  defaults to `"0.0.0.0, ::"` (v1 parity) so PowerDNS binds both address
  families for DNS traffic.
* **Replication auth is still `trust`-by-source-IP**, exactly as v1 had it --
  this is a mechanism carried over verbatim, not hardened, per this wave's
  brief ("keep the mechanism, modernizing only style"). Worth a security
  follow-up if this role's scope is ever revisited.
* **Var prefix**: `pdns_*` (v1) -> `powerdns_*` for every var this role
  *defines* (`ansible-lint`'s `var-naming[no-role-prefix]` expects the
  variable prefix to match the role directory name, `powerdns`). The three
  vaulted secrets (`pdns_api_key`, `pdns_web_key`, `pdns_db_password`) are
  **not** renamed -- they are inventory-owned inputs, not role-defined vars,
  and the wave brief specifies those exact names.

## Flagged for the manager

* `powerdns_primary_host: "{{ groups['ns_primary'][0] }}"` (in
  `vars/main.yml`) assumes exactly one `ns_primary` host, the same way
  `vault_host: groups['controller'][0]` assumes exactly one controller.
  Nothing in `roles/preflight` currently asserts `ns_primary`'s group size
  the way the controller/one-node-per-az invariants are enforced elsewhere.
  If that assumption should be a hard preflight check, that's a preflight
  change, out of this role's file allowlist.
* The base-schema restore path
  (`/usr/share/doc/pdns-backend-pgsql/schema.pgsql.sql`) is carried over
  unchanged from v1's Debian/Ubuntu package layout assumption. Worth
  confirming against the actual Ubuntu 26.04 `pdns-backend-pgsql` package the
  first time this role converges for real (same spirit as a versions.yml
  `VERIFY` marker, just not a version string).

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `powerdns_name` | `ns1.example.com` | SOA identity for zones the leader creates. **Override per environment.** |
| `powerdns_default_soa` | derived from `powerdns_name` | |
| `powerdns_default_ttl` | `14400` | |
| `powerdns_query_cache_ttl` | `20` | |
| `powerdns_dns_address` | `"0.0.0.0, ::"` | Dual-stack DNS listener. |
| `powerdns_web_address` | `"{{ primary_ip }}"` | Leader-only. Bound to the inventory address the controller dials, not `0.0.0.0`; `webserver-allow-from` restricts callers on top of that (see above). |
| `powerdns_replication_user` | `repuser` | Postgres replication role name. |
| `powerdns_manage_zone` | `true` | Set `false` to skip `pdnsutil create-zone`. |
| `powerdns_enable_dnsupdate` | `true` | v1 parity; per-zone updates still require `tsig`/`allow-dnsupdate-from` zone metadata. |
| `powerdns_postgresql_data_dir` / `_main_dir` / `_conf` / `_hba_conf` | distro paths under `/var/lib/postgresql/{{ postgresql_version }}` and `/etc/postgresql/{{ postgresql_version }}/main` | |
| `pdns_api_key` / `pdns_web_key` / `pdns_db_password` | **required, no default** | From the vaulted inventory. Asserted non-blank and not `CHANGEME`. |
| `postgresql_version` | **required** (global, `versions.yml`) | Currently `17`. |
| `cs_app_zone` | **required** (global, `main.yml`) | Tenant DNS zone created on the leader. |
| `cs_ports.dns` / `cs_ports.pdns_api` / `cs_ports.postgres` | **required** (global, `ports.yml`) | `53` / `8081` / `5432`. |

Role-internal computed vars (`vars/main.yml`, not meant to be overridden):
`powerdns_is_leader`, `powerdns_is_follower`, `powerdns_primary_host`,
`powerdns_primary_ip`, `powerdns_replication_slot`.

## Requirements

* `community.postgresql` (pinned in `requirements.yml`) for
  `postgresql_ping`, `postgresql_user`, `postgresql_db`, `postgresql_pg_hba`.
* Inventory: `nameservers` group with `ns_primary` (exactly one host) and
  `ns_followers` (zero or more) sub-groups, each host carrying `hostname`,
  `primary_ip`, `public_ip` (`docs/contracts.md` required node vars).
* Vaulted `pdns_api_key`, `pdns_web_key`, `pdns_db_password`.

## Example

```yaml
- hosts: nameservers
  roles:
    - powerdns
```

(`playbooks/site.yml` already wires this play; see the `nameservers` play
there.)
