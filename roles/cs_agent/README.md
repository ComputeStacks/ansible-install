# cs_agent

**Owner:** Wave 2D. **Hosts:** `nodes`.

Installs the ComputeStacks node agent (cs-agent v3.3.0) from the upstream apt
repository, renders `/etc/computestacks/agent.yml`, enrolls the node against
the controller, and authorizes the node's borg key on the shared backup
server.

Runs **after** the `controller_seed` play — enrollment reads a hash that only
exists once the controller has a `Node` row for this hostname
(`playbooks/site.yml` freezes that order).

## What it does

1. **apt.** Fetches `computestacks.gpg.asc`, dearmors it to
   `/etc/apt/keyrings/computestacks.gpg`, writes
   `/etc/apt/sources.list.d/computestacks.list`
   (`deb [signed-by=…] <repo> stable main`), installs
   `cs-agent={{ cs_agent_apt_version }}` and holds the package. The hold is
   released immediately before each install so a version bump in
   `versions.yml` still rolls through (convergent — contracts.md rule 3).
   The unit itself ships in the deb.
2. **Config directories.** `/etc/computestacks` (0750) and
   `/etc/computestacks/backup/.ssh` (0700).
3. **Borg SSH keypair** at `/etc/computestacks/backup/.ssh/id_ed25519`
   (`regenerate: never`). It has to live under `/etc/computestacks` because
   that is the only host directory bind-mounted into the borg container.
4. **Backup server authorization.** The public key is added to the `cstacks`
   account's `authorized_keys` on **this node's site's** backup host
   (delegated), one additive entry per node commented `cs-agent <hostname>` —
   safe against an `existing_env` server that already carries other nodes'
   keys. No forced command: the agent runs `mkdir -p`/`rm -rf` over this
   connection to create and tear down repositories, and a `borg serve`
   ForceCommand would break repository creation outright.

   Steps 2–4 are **skipped entirely when this node's site names no backup
   server** — see below.
5. **Enrollment** (`tasks/enroll.yml`, tagged `enroll`) — see below.
6. **`agent.yml`** (0600), then enable and start the service. Config changes
   notify a restart.

## Which backup server (site scoping)

Backup servers are **site-scoped** (docs/contracts.md §Vocabulary): a node
backs up to the one backup server standing in its own physical facility. The
role resolves it through `cs_site_backup_hosts[cs_site]`, the frozen spelling
from `playbooks/group_vars/all/sites.yml`; it re-derives nothing and there is
no `cs_agent_backup_group` any more (that map is built from a literal
`groups['backup']`, so the indirection named a group nobody read).

Those maps reference `hostvars` and are therefore **local reads only**
(contracts.md hard rule 10) — every one here happens on the node itself, keyed
by that node's own `cs_site`.

A **single-site inventory sets `site` nowhere**, every host lands in the site
called `default`, the map holds exactly one key, and the role resolves the
same host `groups['backup'] | first` used to return.

## No backup server

A backup server is optional, and optional **per site**. A site may hold **one**
backup host or **none**. Two in one site is caught twice: by `preflight` for
the whole inventory, and by the backstop assert at the top of `tasks/main.yml`
for this node's own site. The backstop is not redundant — `cs_site_backup_hosts`
is built by zipping sites onto hosts, so the second host claiming a site does
not error, it *wins*, and every node in the site silently starts writing its
borg repository somewhere else. The assert counts the raw list, which is the
only place that duplicate is still visible.

With this node's site holding no backup host, the node still gets its agent,
its enrollment and its metadata front door. What is skipped:

* the borg key directory, the keypair, and the `authorized_key` delegation —
  all three, as one block. That last task's `delegate_to` still has to resolve
  to *something*: ansible templates it at task setup, before any `when` is
  evaluated, so a bare `cs_site_backup_hosts[cs_site]` fails the task even when
  the block is skipped. It uses a total lookup
  (`cs_site_backup_hosts.get(cs_site, inventory_hostname)`) for that reason;
  the fallback host is never contacted.
* the `backups.borg` block in `agent.yml`. `backups.enabled` renders `false`,
  though `backups.key` is still written so that adding a backup server later
  is purely an inventory change.
* `validate`'s borg reachability and version checks
  (`roles/validate/tasks/main.yml` gates them on the same emptiness).

`backups_key` is still required in the secrets file — it is what makes adding
a server later a no-decision change.

## Enrollment

Per docs/contracts.md, delegated to the controller host:

```
cstacks runner "puts Node.find_by!(hostname: %q{<hostname>}).agent_token_hash"
```

The output must be 64 lowercase hex characters or the task fails loudly; it is
written to `metadata.admin_token_hash` and the agent restarted. cs-agent's own
documented HTTP enrolment endpoint is deliberately **not** used (it is
source-IP matched and would have to be reachable from every node); the
controller only ever stores the hash, so reading it controller-side is the
cheaper path.

`cstacks` is a **Wave 3G** deliverable. Until it lands these tasks fail with a
missing command, which is the intended behaviour — **end-to-end verification of
this path is Wave 4J's** (`validate`). Nothing here is stubbed.

Re-enrol a node on its own with `--tags enroll`: the `agent.yml` render carries
the same tag so the fetched hash is written out and the service restarted.
`tasks/read_token_hash.yml` (also tagged) pre-seeds the hash from whatever is
already in `agent.yml`, so a converge that does not re-enrol can never
re-render the file with an empty hash — an empty hash disables the admin scope
and the controller loses the node.

Phase 4 of cs-agent replaces `tasks/enroll.yml` with `cs-agent enroll`; nothing
else in this role changes.

## agent.yml schema notes

The schema is cs-agent's `config/config.go`, **not** the upstream
`agent.sample.yml`, which is wrong in ways that fail silently:

| Sample says | Reality (`config/config.go`) |
|---|---|
| `backups.borg.compress` | `backups.borg.compression` |
| `backups.mariadb.*` | `mariadb.*` is **top level** |
| `backups.export.cleanup_freq`, `failed_retention_sec` | dead keys, not read |

Only installer-owned keys are rendered; everything else keeps the agent's
compiled-in default (prune/compact schedules, borg lock waits, changelog and
task retention, `metadata.max_body_bytes`). **There are no NFS keys anywhere** —
v2 is SSH/borg only.

`metadata.listen_addr` is `:8500` when the node is on the tailnet, else
`<primary_ip>:8500`, and never the tailscale address itself: containers reach
the agent through `metadata.internal`, which resolves to `primary_ip`. The port
is `cs_ports.agent_http` and cannot move — it is baked into customer
containers.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `cs_agent_apt_version` | versions.yml (`3.3.0`) | Pinned apt package version. |
| `cs_agent_apt_repo` / `cs_agent_apt_key_url` | versions.yml | Repository and signing key. |
| `cs_agent_listen_addr` | derived | Metadata front-door bind address (see above). |
| `cs_agent_admin_token_hash` | `""` | Filled by enrollment; pre-seeded from the existing file. |
| `cs_agent_backup_configured` | derived | True when this node's site has a backup host (`cs_site in cs_site_backup_hosts`). Gates every borg task and the `backups.borg` block in `agent.yml`. |
| `cs_agent_backup_hosts_in_site` | derived | Backup hosts declaring this node's site, as a raw list. Only the backstop assert reads it. |
| `cs_agent_backups_enabled` | `true` | Operator intent. `agent.yml` gets this **AND** `cs_agent_backup_configured`. |
| `cs_agent_backup_ssh_user` | `cstacks` | Account on the backup server. |
| `cs_agent_backup_host_path` | `backup_host_path` or `/var/lib/computestacks/backups` | Repository path **on the backup server**. |
| `cs_agent_borg_remote_path` | `backup_borg_remote_path` or `borg_binary_path` | borg **on the backup server**. |
| `cs_agent_borg_compression` | `zstd,3` | `backups.borg.compression`. |
| `cs_agent_queue_numworkers` | `3` | Agent job workers. |
| `cs_agent_s3_export_*` | empty | Backup export to S3; inert until a bucket **and** `s3_export_access_key`/`s3_export_secret_key` are set. |
| `cs_agent_mariadb_*` | agent defaults | Top-level `mariadb.*` block. |
| `cs_agent_enroll_command` | `cstacks` | Set to an absolute path if `cstacks` is not on the controller's non-interactive `PATH`. |

Consumed, not owned: `hostname`, `primary_ip` (inventory), `backups_key`,
`sentry_dsn`, `s3_export_*` (secrets), `cs_portal_domain`, `cs_ports`,
`borg_image`, `borg_binary_path`, `tailscale_authkey`/`tailscale_enabled`,
`cs_site` and `cs_site_backup_hosts` (`playbooks/group_vars/all/sites.yml` —
frozen spellings, local reads only).

`agent.yml` is rendered `no_log: true`: it carries `backups.key` and the admin
token hash, and a `--diff` run would otherwise print both.

**Attach mode:** `backup_host_path` and `backup_borg_remote_path` are
*required* inventory inputs describing the existing environment (v1 servers:
`/mnt` and `/usr/bin/borg`), and `backups_key` must be the existing
environment's value.

## Deviations from v1 (`roles/cs_agent`)

- Native deb under systemd instead of a container + a hand-written unit; no
  image pull, no `cs-agent.service.j2`.
- Not guarded by `when: ansible_facts.services[...] is not defined` — the role
  is convergent, so a version or config change actually applies on a rerun.
- Consul is gone (no `consul:` block, no consul/docker cert plumbing here).
- Every NFS key is gone.
- The key moved from `/etc/computestacks/.ssh/` to
  `/etc/computestacks/backup/.ssh/`, matching the agent's own default.
- `compress` -> `compression`; `mariadb` hoisted to top level.
