# postgres_repo

**Owner: Wave 4J.** `hosts: controller` and `hosts: nameservers`, always
**before** the role that installs a postgres package.

## Why it exists

Ubuntu 26.04 ("resolute") ships **PostgreSQL 18** — its `postgresql`
metapackage is `18+290ubuntu1`, depending on `postgresql-18` — and carries
**no `postgresql-17` at all**, in `resolute`, `resolute-updates` or
`resolute-backports` (verified 2026-08-28). The controller's schema and CI are
validated against 17 only, and `postgresql_version: 17` is what
`playbooks/vars/Ubuntu-26.yml` builds every package name and path from. Without
this role, `geerlingguy.postgresql` fails on the controller with "no package
matching postgresql-17", and `roles/powerdns` fails the same way on the
nameservers.

The pinned `geerlingguy.postgresql` 4.0.0 cannot solve this itself: its
`postgresql_enablerepo` is RedHat-only (`tasks/setup-RedHat.yml`), and its
`tasks/setup-Debian.yml` is a bare `apt: name={{ postgresql_packages }}`. So
the repository is this role's job, and the role is deliberately separate from
both consumers — two host groups need it, and neither of them owns apt policy
for the other.

PGDG publishes the major for this suite: `resolute-pgdg` carries
`postgresql-17` at `17.11-1.pgdg26.04+2`.

## What it does

1. Asserts `postgresql_version`, `pgdg_key_url` and `pgdg_key_checksum` are
   set (all three live in `playbooks/group_vars/all/versions.yml`).
2. Installs `python3-debian` — `ansible.builtin.deb822_repository` needs it on
   the **target** host — and `gnupg`.
3. Fetches the PGDG signing key to `/etc/apt/keyrings/pgdg.asc`, verified
   against `pgdg_key_checksum`, and dearmors it to `pgdg.gpg`. Same idiom as
   `roles/cs_agent`: apt refuses an armored key in `signed-by`, and the
   dearmor re-runs only when the key changed or the keyring is missing.
4. Writes the `pgdg` deb822 source (`apt.postgresql.org/pub/repos/apt`, suite
   `resolute-pgdg`, component `main`, `amd64`).
5. Writes `/etc/apt/preferences.d/pgdg.pref` (below), then updates the apt
   cache if either file changed.
6. Asserts `apt-cache policy postgresql-<major>` reports an installation
   candidate — so a repository problem fails here, naming the repository,
   rather than three tasks later inside a galaxy role.

## The apt preferences, and why there is no `apt-mark hold`

Two stanzas, in this order (apt takes the **first** matching stanza for a
given package version, so the refusals must come first):

1. **The unversioned metapackages are refused from every origin**
   (`Pin-Priority: -1`): `postgresql`, `postgresql-client`,
   `postgresql-contrib`, `postgresql-all`, `postgresql-doc`. The archive's
   copies track *its* default major (18) and PGDG's copies track the newest
   major *it* ships; either would install a second cluster next to the pinned
   one. Every consumer in this repository names `postgresql-<major>`
   explicitly, so nothing legitimate pulls these, and an apt error naming the
   package is the intended outcome if something starts to.
2. **PGDG wins for everything else matching `postgresql*` / `libpq*`**
   (`Pin-Priority: 1001`, `Pin: origin apt.postgresql.org`). Above 1000 so it
   can downgrade a version the archive already installed — specifically
   `postgresql-common`, whose default-version logic is what decides where an
   unversioned request resolves.

There is deliberately **no `apt-mark hold`** here, unlike `roles/cs_agent`,
`roles/node_exporter` and the docker packages. Those pin an exact version
string, so an unattended upgrade would move them off the pin. Here the pin is
the **major**, and the major is part of the package name: `postgresql-17` can
only ever receive 17.x updates, which are the security updates the fleet wants
and the reason a hold would be actively harmful. What a hold protects against
elsewhere — drifting onto a different version — is structurally impossible for
a versioned postgres package.

## Attach mode

`playbooks/add-region.yml` does **not** run this role, and does not need to:
attach mode installs no postgres package anywhere. Its plays touch the new
nodes (which run no database), the existing controller (firewall appends,
`cstacks`, seeding, backfills — never `geerlingguy.postgresql`), the existing
metrics and backup hosts, and never the nameservers. An existing v1
environment's postgres is not this repository's to re-point.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `postgres_repo_uri` | `https://apt.postgresql.org/pub/repos/apt` | PGDG base URI. |
| `postgres_repo_suite` | `resolute-pgdg` | PGDG names suites `<codename>-pgdg`. Change with the distro. |
| `postgres_repo_components` | `[main]` | |
| `postgres_repo_architectures` | `[amd64]` | The fleet is amd64 (README, Requirements). |
| `postgres_repo_name` | `pgdg` | Basename of the `.sources` file. |
| `postgres_repo_keyring_dir` / `_keyring_asc` / `_keyring` | `/etc/apt/keyrings`, `pgdg.asc`, `pgdg.gpg` | |
| `postgres_repo_preferences_file` | `/etc/apt/preferences.d/pgdg.pref` | |
| `postgres_repo_pin_priority` | `1001` | PGDG preference; >1000 to permit a downgrade. |
| `postgres_repo_metapackage_pin_priority` | `-1` | Refusal, not a preference. |
| `postgres_repo_blocked_metapackages` | see `defaults/main.yml` | The unversioned metapackages. |

Consumed, not owned: `postgresql_version`, `pgdg_key_url`,
`pgdg_key_checksum` (all `playbooks/group_vars/all/versions.yml`).

## Changing the major

Three files move together, and the controller repo has to agree:
`postgresql_version` in `versions.yml`, the paths and package names in
`playbooks/vars/Ubuntu-26.yml`, and the controller's own schema/CI. Nothing
here upgrades an existing cluster — apt installing a new major installs a
*second* cluster; the data migration is a separate, manual operation.

## Deviations from v1

v1 installed whatever postgres the distro shipped, because on 22.04/24.04 that
happened to be the major the controller wanted. On 26.04 it is not, and the
mismatch is silent right up to the point where the controller's migrations run
against a cluster with a schema it does not support.
