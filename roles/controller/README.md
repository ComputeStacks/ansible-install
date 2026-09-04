# controller

**Owner: Wave 3G.** `hosts: controller`.

## Purpose

The ComputeStacks controller host: the `cstacks` CLI, `/etc/default/
computestacks`, the `portal` container, the application ssh keypair that every
node and the registry host trust, and the self-signed tenant wildcard
certificate every load balancer serves.

It does **not** own the manifest or the seeding step — `controller_seed`
(Wave 4I) renders the manifest and calls `cstacks seed`. It does not own the
TLS terminator either; `acme_web` runs before it in `playbooks/site.yml`.

## What it does, in order (`tasks/main.yml`)

1. Asserts the image is `repo` + a non-floating tag, that the immutable
   secrets are present and long enough, that `postgres_password` is URL-safe,
   and that `cs_app_zone` is a bare zone.
2. Creates the directory layout (below).
3. Generates the application ssh keypair at
   `/etc/computestacks/.ssh/id_ed25519` (`regenerate: never`).
4. Generates the self-signed tenant wildcard **once**.
5. Installs the `cstacks` CLI and its bash completion.
6. Renders `/etc/default/computestacks` — **only when not
   `existing_env`** (`no_log`, mode 0600).
7. Creates the peer-auth postgres `root` superuser and its database
   (`tasks/postgres.yml`).
8. Records the docker client certificate's checksum, so a rotation by
   `roles/vault` restarts the portal.
9. `cstacks bootstrap-db` on a greenfield database (`creates:` the sentinel).
10. `cstacks upgrade` when the running container's image no longer matches the
   configured tag.
11. Flushes handlers, then starts the portal if it is not already running.

## Directory layout

| Path | Contents |
| --- | --- |
| `/etc/computestacks` | Config root. |
| `/etc/computestacks/certificates` | `docker/` holds the vault-issued client cert (`roles/vault`), mounted into the container as `/root/.docker`. |
| `/etc/computestacks/.ssh` | **v2 change.** The application ed25519 keypair. v1 kept it in `/var/lib/computestacks/sshkeys`; it moved here because `roles/ssh_trust` reads `id_ed25519.pub` from this path to seed root's `authorized_keys` on every node and on the registry host. |
| `/var/lib/computestacks` | State root. Holds the `.db_provisioned` sentinel and the docker-cert checksum. |
| `/var/lib/computestacks/backups` | `cstacks database-backup` output. |
| `/var/lib/computestacks/branding` | Mounted as `public/assets/custom`. Seeded per-file from the image's `public/custom` (see below); drop your own assets here and they are never overwritten. |
| `/var/lib/computestacks/proxy_ips` | **v2 new.** `ProxyIpList`'s persistent store, mounted as `lib/proxy_ips`. |
| `/var/lib/computestacks/.ssl_wildcard` | `sharedcert.pem` (cert **and** key in one file — the form haproxy wants) plus the openssl config that produced it. |

## The `cstacks` CLI

Installed at `/usr/local/bin/cstacks`, mode 0750. It reads every setting from
`/etc/default/computestacks` and holds no environment-specific values, which
is what lets attach mode drop it unchanged onto a v1-built controller. Every
value it reads has a fallback, because on such a host that file carries only
what v1 wrote plus the two keys attach mode appends.

| Subcommand | What it does |
| --- | --- |
| `run` | Removes any existing `portal` container and `docker run -d`s a new one: `--net=host`, `--restart=unless-stopped`, `--log-driver=journald`, label `com.computestacks.role=system`. |
| `stop` | Stops the container (leaves it in place). |
| `upgrade` | `database-backup` → `docker pull` → stop/rm → `db:migrate` → `run`. The backup goes **first**: `set -e` means a failed pg_dump aborts before the image or schema move. |
| `migrate` | `rails db:migrate` in a one-off container. |
| `bootstrap-db` | `rails db:schema:load` in a one-off container, then writes `.db_provisioned`. Refuses to run if the sentinel exists or a `portal` container is present. **`db:schema:load`, not migrate-from-zero** — they are not equivalent for this application. |
| `seed <manifest>` | Bind-mounts the manifest read-only at `/tmp/manifest.yml` and runs `bundle exec rake bootstrap:apply[/tmp/manifest.yml]`. `DRY_RUN=1 cstacks seed …` passes through. |
| `runner '<ruby>'` | **The cross-role contract** — see below. |
| `console` / `container` | `rails c` / `bash`, exec'd into the running container or a one-off. |
| `test` | `rake test_connection:all`. |
| `database-backup` | `pg_dump <db> -Z 5` into `$DB_BACKUPS_PATH`, as root over the unix socket. |
| `logs` / `tail-logs` | `journalctl CONTAINER_NAME=portal`. |

### The `runner` contract

```
cstacks runner '<ruby one-liner>'
```

`docker exec portal bin/rails runner '<ruby>'`, via `exec`, with **no tty**.
Only the ruby program's own output reaches stdout and its exit code is the
process's exit code. `roles/cs_agent`'s enroll task depends on both
(docs/contracts.md §Controller invocation helper): it runs

```
cstacks runner "puts Node.find_by!(hostname: %q{<hostname>}).agent_token_hash"
```

delegated to this host and parses the 64-hex result. A tty would inject
carriage returns into that value, which is why `-it` is deliberately absent
here while `console` uses it.

Exits non-zero with a message on stderr if the container is not running.

### Container arguments

Built once into shell arrays, so every subcommand launches the image the same
way (v1 repeated the whole flag list in seven places and they had drifted).

Mounts: `$CS_CERT_PATH/docker` → `/root/.docker`, `$CS_BRANDING_PATH` →
`public/assets/custom`, `$CS_SSH_KEYS_PATH` → `lib/ssh`, `$CS_PROXY_IPS_PATH`
→ `lib/proxy_ips`. **No consul mount** — consul is gone from the stack.

## Postgres

`geerlingguy.postgresql` is applied by the play, not by this role. The play
must set (Wave 4J owns the wiring; these are the values this role's
`DATABASE_URL` assumes):

```yaml
postgresql_version: "{{ postgresql_version }}"        # versions.yml, 17
postgresql_global_config_options:
  - option: listen_addresses
    value: '127.0.0.1'                                # geerlingguy defaults to '*'
postgresql_databases:
  - name: cloudportal
    owner: computestacks
postgresql_users:
  - name: computestacks
    password: "{{ postgres_password }}"
    role_attr_flags: SUPERUSER
```

geerlingguy's default `pg_hba` entries already match v1's: `peer` for local
socket connections, `md5` for `127.0.0.1/32` — which is exactly what
`DATABASE_URL` and `cstacks database-backup` respectively need.

**What geerlingguy does not create, and this role does** (`tasks/
postgres.yml`): v1's peer-auth `root` SUPERUSER role and a `root` database.
`pg_dump` with no `-d` connects to a database named after the connecting role,
so without them `cstacks database-backup` fails — and under `set -e` that
takes `cstacks upgrade` down with it, before it ever migrates.

**Redis** must stay loopback-bound with **no `requirepass`**: `REDIS_HOST` is
interpolated bare into `redis://` URLs and there is no password path in the
application.

## Attach mode (`tasks/attach_prep.yml`)

Attach mode runs `tasks/attach_prep.yml` against an existing, v1-built
controller; `tasks/main.yml` never runs there.

> **Entry point, and a bug in the current `add-region.yml` (Wave 4J owns that
> file).** `tasks_from:` is a parameter of `include_role` / `import_role`, not
> of a play's `roles:` keyword. `playbooks/add-region.yml` currently says
>
> ```yaml
>   roles:
>     - role: controller
>       tasks_from: attach_prep
> ```
>
> which ansible silently ignores — it runs `tasks/main.yml`, the greenfield
> converge, against a live controller (verified against ansible-core; the
> `tasks_from` value just becomes an unused role variable). The correct form
> is `include_role` with `tasks_from`, as in `tests/wave3g.yml`. Until that is
> fixed, `tasks/main.yml`'s first task asserts `not existing_env` and fails
> loudly with the corrected snippet rather than converging.

Assert first, then act:

1. `/etc/default/computestacks` must exist.
2. **`secret_key_base` in the vaulted secrets must equal the file's
   `SECRET_KEY_BASE` byte-for-byte**, or the play aborts. The manifest apply
   encrypts with that value; applying under a different one writes credentials
   the running controller decrypts to `nil` — silent, total credential loss.
   The fix is always to copy the existing value into `secrets.yml`, never the
   other way around.
3. `cstacks database-backup` — the gate. Everything downstream
   (`controller_seed`) writes to the database.
4. `lineinfile` **appends** `NODE_ENROLLMENT_TOKEN` and `CS_PROXY_IPS_PATH`,
   and only when the key is absent. No `regexp:` is used, so an existing value
   can never be rewritten. docs/contracts.md: the environment file is
   append-only, always.
5. Installs the v2 `cstacks` script (with `backup: true`) — one of the two
   whole-file exceptions to the attach-mode rule.
6. Flushes the handler explicitly, so the recreated portal carries the new
   environment and the proxy_ips mount before `controller_seed` runs.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `controller_image_repo` / `controller_image_tag` | `ghcr.io/computestacks/controller` / `9.7` | `versions.yml`. Composed into `CS_REG`. The **minor** tag is a deliberate rolling channel; a digest pin would make `cstacks upgrade` a permanent no-op. |
| `controller_image_allow_floating_tag` | `false` | Permit a `latest`/`stable`/`main`/`master` tag. For an image that publishes no immutable line (an internal build cut on demand); set it as a host var beside the repo override. Costs reproducibility: nothing then records which build a host runs. |
| `controller_auto_upgrade` | `true` | When the running container's image differs from `CS_REG`, run the full `cstacks upgrade` rather than recreating on an unmigrated schema. Set false to make tag bumps manual. |
| `controller_wildcard_domain` | `{{ cs_app_zone }}` | CN and `*.` SAN of the shared certificate. |
| `controller_wildcard_days` | `3650` | Generated once; never rotated by a converge. |
| `controller_app_id` | `default` | Links Sentry reports to this installation. |
| `controller_sentry_dsn` | `{{ sentry_dsn \| default('') }}` | Empty disables bug reporting. v1 defaulted to ComputeStacks' own DSN. |
| `controller_postgres_*` | `computestacks` / `cloudportal` / `127.0.0.1` / pool 40 | Composed into `DATABASE_URL`. |
| `controller_rails_*`, `controller_puma_workers`, `controller_queue_*` | v1 values | Concurrency. |
| `controller_registry_username` / `_password` | unset | Optional pull credentials for a private image repo, in `secrets.yml`. This role does **not** log in — `playbooks/group_vars/all/registries.yml` folds the pair into `docker_registries` and `roles/docker_config` performs the login, one play earlier. |

Consumed, not owned: `cs_ports.redis`, `secret_key_base`, `user_auth_secret`,
`node_enrollment_token`, `postgres_password`, `cs_app_zone`, `locale`,
`currency`, `existing_env`.

## Handler

**`Recreate the portal container`** → `cstacks run`. A `docker restart` is not
enough: the environment is baked into the container at creation and the docker
client certificate is read once at boot. Notified by the environment file, the
`cstacks` script, the docker client certificate checksum, and attach mode's
appends.

The certificate watch is the fix for the issue Wave 1B flagged: `roles/vault`
re-issues `/etc/computestacks/certificates/docker/cert.pem` as it nears
expiry, and nothing else would notice — the controller would keep dialing
every node's dockerd with an expired client certificate until someone
restarted the portal by hand.

## Deviations from v1 (`roles/controller`, `roles/controller_postgres`)

* **`SECRET_KEY_BASE` / `USER_AUTH_SECRET` are never generated.** v1 ran
  `openssl rand -hex 64` when they were blank, which silently produced a
  different key on a re-run against a wiped `/etc/default/computestacks` and
  destroyed every encrypted column. Both are asserted inputs now
  (docs/contracts.md rule 8).
* **The wildcard certificate is generate-once.** v1 regenerated it on every
  run; under v2's convergent rule that would rotate the load balancer shared
  certificate fleet-wide on every converge.
* **`bootstrap-app` is gone.** v1's generated `bootstrap.rake` embedded ruby
  in a Jinja template and drifted from the controller's models. Seeding is now
  `cstacks seed` → `rake bootstrap:apply[<manifest>]` against a versioned
  schema.
* **No consul mount, no `/root/.consul` directory, no consul token slurp.**
* **The branding CDN downloads are gone.** v1 fetched `application.css`, its
  map and a login image from `f.cscdn.cc` on every bootstrap — unpinned
  third-party content pulled into the portal's asset path. The same files are
  copied out of the pinned controller image instead (`controller_image_branding_source`,
  default `/usr/src/app/public/custom`), once the portal is up and only for
  names the branding directory does not already have.

  This is not cosmetic. The production layout links
  `/assets/custom/application.css` unconditionally, and that path *is* the
  mount — so an empty branding directory masks whatever the image had there
  and the portal comes up unstyled, 404ing on its own stylesheet. Copying
  per-file rather than per-directory means an operator's own
  `logo-login.png` survives while the stylesheet is still filled in. Set
  `controller_seed_default_branding: false` to own the directory outright.
* **`/computestacks-mnt` is not created here.** v1 created the registry data
  root on the *controller*, where nothing uses it; `roles/registry` creates it
  on the registry host, where the registry containers bind-mount it.
* **No `tags: ['never', 'bootstrap', 'addnode']` gating.** The role is
  convergent by default; attach mode is a separate task file rather than a tag.
* **`cstacks` is `set -euo pipefail` and shellcheck-clean**, builds its docker
  arguments once, and gained `stop`, `seed`, `runner` and `migrate`. `console`
  and `test` no longer force `-it` when there is no terminal.
* The cloudflare real-IP cron script is not ported: `ProxyIpList` in the
  controller (9.7) now owns the Cloudflare and Bunny address lists, refreshes
  them itself, and persists them in the `proxy_ips` mount.

## Requirements

`community.crypto` (ssh keypair), `community.postgresql` (root role/db) — all pinned in `requirements.yml`.
Runs after `geerlingguy.docker`, `docker_config`, `vault`,
`geerlingguy.postgresql`, `geerlingguy.redis` and `acme_web` on the controller
(`playbooks/site.yml`).
