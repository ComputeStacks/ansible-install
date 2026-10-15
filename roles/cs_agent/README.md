# cs_agent

Installs the ComputeStacks node agent (cs-agent, pinned in `versions.yml`) from the upstream apt
repository, renders `/etc/computestacks/agent.yml`, enrolls the node against
the controller, and authorizes the node's borg key on the shared backup
server.

Runs **after** the `controller_seed` play — enrollment reads a hash that only
exists once the controller has a `Node` row for this hostname
(`playbooks/site.yml` freezes that order).

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Node agents (requires seeded Node rows)" | `nodes` |
| `add-region.yml` | "New node agent (requires the seeded Node row)" | `nodes` (the new node) |
| `maintain-nodes.yml` (via `node_upgrade` `tasks_from: maintain`) | "Maintain the nodes, one at a time" | `maintain_node_targets` (live nodes, inside a maintenance hold) |
| `maintain-nodes.yml` (`tasks_from: immutable_check`, via `node_upgrade`) | "Preflight every targeted node", and again per node before its hold | `maintain_node_targets` |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** new node only; on the existing backup server it adds one
delegated `authorized_keys` entry for the new node and nothing else.
**Requires:** `controller_seed` (the Node row), `controller` (`cstacks` on the
controller), `backup_server` (`~cstacks/.ssh` on this site's backup host).

## What it does

1. **apt.** Fetches `computestacks.gpg.asc`, dearmors it to
   `/etc/apt/keyrings/computestacks.gpg`, writes the deb822 source
   `/etc/apt/sources.list.d/computestacks.sources` (`<repo>`, suite `stable`,
   component `main`, `Signed-By` that keyring) and deletes the one-line
   `computestacks.list` an earlier version of this role wrote, installs
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
   notify a restart. Before any of this, `tasks/export_check.yml` checks the
   backup export settings (see below).

## On a live node (`maintain-nodes.yml`)

`maintain-nodes.yml` runs this whole role on enrolled, live nodes, inside a
cs-agent maintenance hold (roles/node_upgrade/README.md, "maintain-nodes").
Three things are different or worth knowing there:

* **The agent is upgraded inside the hold, never downgraded.** The package
  install restarts the agent (postinst, and the "Restart cs-agent" handler);
  the hold lives in control.db and survives it, and node_upgrade waits for
  `maintenance status --json` to show the agent active with the hold still in
  place before it goes on. `cs_agent_allow_downgrade: false` is passed, and
  node_upgrade's preflight refuses a `cs_agent_apt_version` below the
  installed version before any hold.
* **Immutable keys** (`tasks/immutable_check.yml`, filter
  `cs_agent_immutable_changes`). Before any hold, the role's render of
  `agent.yml` is compared with the node's file, and the run is refused if it
  would change `store.data_dir`, `metadata.admin_token_hash`,
  `metadata.listen_addr`, `backups.key` or `backups.borg.ssh.{user, host,
  port, host_path}` -- the hold's own database, the controller's token, the
  front door, the borg passphrase and where the repositories live. The values
  are compared as sha256 digests and never printed. A per-node `backups_key`
  must therefore be byte for byte the node's current passphrase, with no
  trailing newline. Encrypt it with
  `IFS= read -rs K && printf %s "$K" | ansible-vault encrypt_string --stdin-name backups_key; unset K`
  and paste the passphrase at the silent prompt (it stays out of the shell
  history and survives any character in the key).
* **Enrollment is a read.** For an enrolled node the controller returns the
  hash the node already has, `agent.yml` is unchanged, and nothing is
  restarted for it.

Everything a converge of this role does OFF the node, in any playbook:

| Host | What | Write? |
| --- | --- | --- |
| controller | `cstacks runner "puts Node.find_by!(hostname: ...).agent_token_hash"` (`bin/rails runner` inside the portal container) | no -- a read |
| this site's backup host | `authorized_key` for `cstacks`: the node's borg key, comment `cs-agent <hostname>`; `manage_dir` creates `~cstacks/.ssh` if missing | only when the entry is missing (never on a node converged before) |
| the apt repository | `get_url` of the signing key; `apt-get update` | no |

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

## Backup export to S3

cs-agent's "download backup" feature (`backups.export.*`) streams a chosen
borg archive to S3 or S3-compatible storage and hands the controller a
presigned URL. It is **off until `cs_agent_s3_export_bucket` is set**: with
the bucket empty no `export:` block is rendered and the agent keeps the
feature inert, whatever else is set.

| Variable | Default | `agent.yml` key |
|---|---|---|
| `cs_agent_s3_export_bucket` | `""` (off) | `backups.export.s3.bucket` |
| `cs_agent_s3_export_endpoint` | `""` (AWS) | `backups.export.s3.endpoint` — an `https://` URL for MinIO, Ceph and the like |
| `cs_agent_s3_export_region` | `us-east-1` | `backups.export.s3.region` |
| `cs_agent_s3_export_prefix` | `exports/` | `backups.export.s3.prefix` |
| `cs_agent_s3_export_force_path_style` | `false` | `backups.export.s3.force_path_style` — `true` for MinIO / path-style |
| `s3_export_access_key` (secrets) | — | `backups.export.s3.access_key` |
| `s3_export_secret_key` (secrets) | — | `backups.export.s3.secret_key` |
| `cs_agent_s3_export_workers` | `1` | `backups.export.workers` |
| `cs_agent_s3_export_tar_filter` | `gzip` | `backups.export.tar_filter` — `""` uploads uncompressed |
| `cs_agent_s3_export_timeout_sec` | `14400` | `backups.export.timeout_sec` |
| `cs_agent_s3_export_part_size_mb` | `64` | `backups.export.s3.part_size_mb` |
| `cs_agent_s3_export_concurrency` | `4` | `backups.export.s3.concurrency` |
| `cs_agent_s3_export_sse` | `AES256` | `backups.export.s3.sse` |
| `cs_agent_s3_export_default_ttl_sec` | `43200` | `backups.export.s3.default_ttl_sec` |
| `cs_agent_s3_export_max_ttl_sec` | `86400` | `backups.export.s3.max_ttl_sec` |

The defaults are cs-agent's own (`config/config.go`), so an operator sets the
bucket, the endpoint (unless AWS), the region and the two keys, and nothing
else. When the bucket is set, every key is rendered.

**Per region.** Each variable is read on the node, so it can differ per
region, per az or per node: put it in the inventory's
`group_vars/region_<name>.yml` (or `az_<name>.yml`; both group families are
built by `zz_constructed.yml`) or in a node's `host_vars`. The credentials
are secrets: keep them in the vaulted `group_vars/all/secrets.yml`, or in a
vaulted per-region group_vars file when regions use different accounts.
`agent.yml` is 0600 and rendered with `no_log`.

**Checks** (`tasks/export_check.yml`, filter `cs_agent_export_problems` in
`filter_plugins/cs_agent_export.py`). Pure; `preflight` includes it on every
new node before anything changes, and this role runs it again as a backstop.
With a bucket set it requires a region and both keys (neither left as the
sample's `CHANGEME`, compared trimmed and case-insensitively; the keys are
never printed), an `https://` endpoint
if one is set, whole numbers ≥ 1 for workers, timeout, concurrency and both
TTLs, `part_size_mb` ≥ 5, an `sse` of `AES256`, `aws:kms`, `aws:kms:dsse` or
`""`, and `default_ttl_sec` ≤ `max_ttl_sec`. cs-agent itself only *logs* a
bad export config at startup and then fails every export job.

`max_ttl_sec` must also stay **below cs-agent's terminal task-row retention**
(`tasks.retention_sec`, 7 days): a completed export whose link is still live
must not be reaped. This role does not render `tasks.retention_sec`, so the
check compares against `cs_agent_upstream_tasks_retention_sec` (`604800`),
which mirrors the agent's compiled-in default and is not a knob.

The exported tar is **plaintext** — the repository's encryption does not
carry over. Keep the bucket private, leave SSE on, and give it an
object-expiry lifecycle rule and `AbortIncompleteMultipartUpload`. Each large
export holds roughly `part_size_mb × concurrency` of memory per worker.

## Enrollment

Per docs/contracts.md, delegated to the controller host:

```
cstacks runner "puts Node.find_by!(hostname: %q{<hostname>}).agent_token_hash"
```

The output must be 64 lowercase hex characters or the task fails loudly; it is
written to `metadata.admin_token_hash` and the agent restarted. cs-agent's own
documented HTTP enrollment endpoint is deliberately **not** used (it is
source-IP matched and would have to be reachable from every node); the
controller only ever stores the hash, so reading it controller-side is the
cheaper path.

`cstacks` (and its `runner` subcommand) is installed on the controller by
`roles/controller`; set `cs_agent_enroll_command` to an absolute path if it is
not on the controller's non-interactive `PATH`. `roles/validate` asserts the
result end to end: a node whose enrollment failed never latches its
datachannel backfill.

Re-enroll a node on its own with `--tags enroll`: the `agent.yml` render carries
the same tag so the fetched hash is written out and the service restarted.
`tasks/read_token_hash.yml` (also tagged) pre-seeds the hash from whatever is
already in `agent.yml`, so a converge that does not re-enroll can never
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

`metadata.listen_addr` is `:8500` when the node routes control-plane traffic
over the tailnet (`cs_agent_tailnet_routed`: a member without
`tailscale_access_only: true`), else `<primary_ip>:8500`, and never the tailscale address itself: containers reach
the agent through `metadata.internal`, which resolves to `primary_ip`. The port
is `cs_ports.agent_http` and cannot move — it is baked into customer
containers.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `cs_agent_apt_version` | versions.yml | Pinned apt package version. |
| `cs_agent_allow_downgrade` | `true` | Let the pinned install move the agent to an older version. `maintain-nodes.yml` passes `false`. |
| `cs_agent_apt_repo` / `cs_agent_apt_key_url` | versions.yml | Repository and signing key. |
| `cs_agent_listen_addr` | derived | Metadata front-door bind address (see above). |
| `cs_agent_admin_token_hash` | `""` | Filled by enrollment; pre-seeded from the existing file. |
| `cs_agent_backup_configured` | derived | True when this node's site has a backup host (`cs_site in cs_site_backup_hosts`). Gates every borg task and the `backups.borg` block in `agent.yml`. |
| `cs_agent_backup_hosts_in_site` | derived | Backup hosts declaring this node's site, as a raw list. Only the backstop assert reads it. |
| `cs_agent_backups_enabled` | `true` | Operator intent. `agent.yml` gets this **AND** `cs_agent_backup_configured`. |
| `cs_agent_backup_ssh_user` | `cstacks` | Account on the backup server. |
| `cs_agent_backup_host_path` | `backup_host_path` or `/var/lib/computestacks/backups` | Repository path **on the backup server**. |
| `cs_agent_borg_remote_path` | `backup_borg_remote_path` or `borg_binary_path` | borg **on the backup server**. |
| `cs_agent_backup_ssh_port` | `backup_ssh_port`, else the backup host's `ssh_port`, else `cs_ports.ssh` | sshd port **on the backup server**. |
| `cs_agent_borg_compression` | `zstd,3` | `backups.borg.compression`. |
| `cs_agent_queue_numworkers` | `3` | Agent job workers. |
| `cs_agent_s3_export_*` | cs-agent's defaults, bucket empty | Backup export to S3; off until `cs_agent_s3_export_bucket` is set. See "Backup export to S3". |
| `cs_agent_upstream_tasks_retention_sec` | `604800` | cs-agent's compiled-in `tasks.retention_sec`; not rendered, only the export check reads it. |
| `cs_agent_mariadb_*` | agent defaults | Top-level `mariadb.*` block. |
| `cs_agent_enroll_command` | `cstacks` | Set to an absolute path if `cstacks` is not on the controller's non-interactive `PATH`. |

Consumed, not owned: `hostname`, `primary_ip` (inventory), `backups_key`,
`sentry_dsn`, `s3_export_*` (secrets), `cs_portal_domain`, `cs_ports`,
`borg_image`, `borg_binary_path`, `tailscale_authkey`/`tailscale_enabled`/
`tailscale_access_only`,
`cs_site` and `cs_site_backup_hosts` (`playbooks/group_vars/all/sites.yml` —
frozen spellings, local reads only).

`agent.yml` is rendered `no_log: true`: it carries `backups.key`, the admin
token hash and, with export on, the S3 keys, and a `--diff` run would
otherwise print them.

**Attach mode:** `backup_host_path` and `backup_borg_remote_path` are
*required* inventory inputs describing the existing environment (v1 servers:
`/mnt` and `/usr/bin/borg`), the backup host's `ssh_port` is required when
its sshd is not on `cs_ports.ssh`, and `backups_key` is the node's borg
passphrase: the environment's value or one of the node's own, never changed
once the node has backups.
