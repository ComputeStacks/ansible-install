# backup_server

The borg backup server a node's cs-agent SSHes into: the `cstacks` account,
the pinned upstream borg release, the repository path, and sshd sized for the
nodes' backup sweep.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Backup server" | `backup` (at most one per **site**) |
| `add-region.yml` | "Existing backup server — account, repository path, firewall appends" | `backup` (`existing_env: true`) |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** only `tasks/account.yml` runs, and its one write is
`~cstacks/.ssh`; sshd is resized only on request (see "Attach mode" below). **Requires:** —.

A fleet may hold several backup servers, one per `site`
(docs/contracts.md §Site scoping). The role itself is site-agnostic — it runs
on each `backup` host and configures that host — but the pairing is not:
`roles/cs_agent` sends a node to `cs_site_backup_hosts[cs_site]`, the server in
that node's own site, and `roles/validate` checks the same one. Put every
`backup` host's `site` in the inventory alongside the nodes that use it. A
single-site inventory sets `site` nowhere, every host lands in the site called
`default`, and the one backup server serves everything exactly as before.

## What it does

1. **`cstacks` system account**, shell `/bin/bash`, password locked (`!`) — key
   auth only (on an `existing_env` server, neither the shell nor the shadow
   entry is written; see below). Its `~/.ssh` is created 0700; the per-node `authorized_keys`
   entries are added by the `cs_agent` role (delegated here), one per node.
   **No forced command**: the agent runs `mkdir -p` / `rm -rf` over this
   connection to create and tear down repositories, so a `borg serve`
   ForceCommand would break repository creation outright. Hardening this needs
   a wrapper with an allowlist covering the agent's exact command shapes — a
   separate piece of work.
2. **borg**, from the upstream GPG-verified release, into
   `/opt/computestacks/borg/<version>/borg`, with a stable wrapper at
   `/opt/computestacks/bin/borg` that sets `TMPDIR=/opt/computestacks/tmp` and
   execs the pinned binary. This is the path nodes call
   (`backups.borg.ssh_borg_remote_path`), so a version switch is a wrapper
   re-render, not a change on every node.
3. **Repository path** (`backup_host_path`, default
   `/var/lib/computestacks/backups`), created `cstacks:cstacks` 0770 — on a
   server these playbooks built. On an `existing_env` server it is only
   `stat`ed.
4. **sshd sized for the backup sweep**, in the drop-in
   `/etc/ssh/sshd_config.d/40-computestacks-backup.conf` (checked with
   `sshd -t` before it is installed, then sshd reloaded; open sessions are
   kept). Every node fires its due backups on the same schedules (the
   controller's default is `@hourly`; cs-agent 3.5.0 and later start each
   project's backups at a fixed offset of up to 2 hours after the cron
   time, which spreads the sweep but does not remove it), each with
   `queue.numworkers` + 1 backups at once (4 by default), and every borg
   invocation is a new connection. Stock `MaxStartups 10:30:100` starts
   dropping new connections at 10 unauthenticated handshakes, sooner when
   internet scanners hold slots or a busy server slows handshakes, failing
   backups at random. The drop-in sets:
   * `MaxStartups`. It is one limit for the whole server — concurrent
     unauthenticated connections from all nodes together — so it grows with
     the fleet. The start is the largest of: 1.5x the simultaneous sessions
     of the nodes in this server's site (each node's own
     `cs_agent_queue_numworkers` + 1; 10 default nodes give 60, 20 give
     120), sshd's own 10, `backup_server_sshd_max_startups_min`, and the
     start already on the server. Full is twice the start (never below 100).
     **A run never lowers it**, so a `--limit` run or an attach cannot shrink
     what a fuller run or the operator set; set
     `backup_server_sshd_max_startups_min` to run it above what the
     inventory implies, and edit or delete the drop-in by hand to lower it.
   * `LoginGraceTime 30` (sshd's default is 120): every connection that has
     not logged in yet holds one of the `MaxStartups` slots, scanners and
     password guessers included, and nodes log in by key in well under a
     second.
   * `PerSourcePenaltyExemptList` with those nodes' `primary_ip` and
     `public_ip`, on OpenSSH 9.8 and later (older sshd has no penalties and
     rejects the option). Those versions refuse a source address for a
     while after a run of failed or aborted connections, which would fail
     that node's whole sweep.

   Nodes reach the server over ssh. `firewall_allow_ssh_from` narrows ssh to
   the listed sources, and `roles/firewall` then still accepts every node, so
   narrowing it never stops their backups. That is
   worth doing on an internet-facing server: scanners then cannot take the
   pre-authentication slots at all.

   The real ceiling as the fleet grows is the server itself: one `borg serve`
   per session, each holding its repository index in memory, all writing to
   one filesystem. Size RAM and disk for the session count above.

There is **no NFS**, no root SSH configuration, and no borg compaction cron:
compaction runs in-agent since cs-agent v3.x.

## Why not the distro borgbackup package

The client half of every operation is the `cs-docker-borg` container image,
which builds borg **1.4.4** from the upstream signed release; Ubuntu 26.04
ships 1.4.3. The two halves must be the same version, so the role installs the
same binary the image does, verified the same way (release key
`6D5BEF9ADD2075805747B70F9F88FB52FAF7B393`, keyservers tried in turn,
`VALIDSIG` checked explicitly against the pinned fingerprint — `gpg --verify`
exits 0 for a good signature by *any* key in the keyring, so the fingerprint
check is what actually pins it). The role then asserts the installed wrapper
reports the pinned version.

The fingerprint compared is the **tenth** field of the `VALIDSIG` line, which
is the primary key's. The first field is whichever key made the signature, and
borg releases are signed by a subkey — so matching the pin against the first
field rejects every genuine release.

`borg_version` (server binary) and `borg_image` (client container) are pinned
together in `versions.yml` and **must be bumped together**. When the real
`computestacks-borg` deb ships it drops into these same paths.

## Attach mode (`existing_env: true`)

Only `tasks/account.yml` runs, and the only thing it **writes** is
`~cstacks/.ssh` (0700) — the directory `cs_agent` then drops the new node's
`authorized_keys` entry into. Everything else on a live server is left alone;
in particular v2's borg is **not** installed over the server's existing one.

Two tasks that a greenfield converge performs are deliberately withheld there,
because on a server these playbooks did not build, "ensure" means "overwrite":

* **The repository root is not chowned or chmodded.** Attach mode requires
  `backup_host_path` to be the existing server's value, which on a v1 server is
  **`/mnt`**. The `file` task is non-recursive, so nothing underneath would be
  touched — but `0770` on `/mnt` itself clears the world execute bit, and world
  execute on a directory is what allows an unrelated process to *traverse* it.
  Every filesystem mounted under `/mnt` becomes unreachable to anything not
  root and not in the `cstacks` group: a monitoring agent stat'ing a share, an
  offsite sync job, a cron reading a mount — all start returning `EACCES`,
  while backups keep working, so nothing connects the outage to this run.
  Instead the path is `stat`ed and the run fails early, with a message naming
  `backup_host_path`, if it is not a directory. `cstacks` must already be able
  to create repositories under it, which it is on any server v1 was backing up
  to.
* **The existing account's shell and shadow entry are not rewritten.**
  `ansible.builtin.user` rewrites `/etc/passwd` when `shell` differs and writes
  the shadow field when `password` is given, so both are omitted on an
  `existing_env` server. `system` and `create_home` stay — the module ignores
  both for an account that already exists.

That makes two inventory inputs **required** on the nodes attaching to such a
server (they describe the existing environment and have no safe default):

- `backup_host_path` — v1 servers use `/mnt`;
- `backup_borg_remote_path` — v1 servers use the distro borg at
  `/usr/bin/borg`. The v2 default (`/opt/computestacks/bin/borg`) does not
  exist there, and every backup on the new node would fail with "command not
  found".

Set `backup_server_install_borg: true` to install v2's pinned borg alongside
the existing one anyway — and then point `backup_borg_remote_path` at it.

Its sshd is left alone too. Set `backup_server_sshd_manage: true` to write
the drop-in there anyway. The attach inventory lists only the nodes being
attached, so the run then requires a size — `backup_server_sshd_fleet_node_count`
(the number of nodes the server serves) or `backup_server_sshd_max_startups_min`
— and `backup_server_sshd_penalty_exempt_extra` (the nodes' subnets, which stay
right as the fleet grows), and fails without them. Keep them in the attach
inventory's `host_vars` for the server. `MaxStartups` never goes down on a
run; the exemption list is rewritten from these values every time.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `backup_server_user` / `backup_server_group` | `cstacks` | The account nodes back up as. |
| `backup_server_shell` | `/bin/bash` | Login shell (the agent runs shell commands over SSH). |
| `backup_server_host_path` | `backup_host_path` or `/var/lib/computestacks/backups` | Repository path. |
| `backup_server_host_path_mode` | `0770` | Mode applied to it — **on a v2-built server only**; never applied on an `existing_env` server. |
| `backup_server_prefix` | `/opt/computestacks` | Install prefix (`borg/<version>/`, `bin/`, `tmp/`). |
| `backup_server_borg_asset` | `borg-linux-glibc231-x86_64` | Upstream release asset (amd64 only — preflight asserts the architecture). |
| `backup_server_borg_keyservers` | 3 keyservers | Tried in turn; all failing is a hard error. |
| `backup_server_install_borg` | `false` | Install borg on an `existing_env` server anyway. |
| `backup_server_sshd_manage` | `true`, `false` on an `existing_env` server | Write the sshd drop-in. |
| `backup_server_sshd_login_grace_time` | `30` | Seconds an unauthenticated connection may hold a slot. |
| `backup_server_sshd_max_startups_min` | `0` | A floor for the `MaxStartups` start, to run it above what the inventory implies. |
| `backup_server_sshd_fleet_node_count` | — | Attach mode with `backup_server_sshd_manage`: the nodes the server serves (or set the floor above). |
| `backup_server_sshd_backups_per_node` | `4` | Concurrent backups per node for `backup_server_sshd_fleet_node_count`. A v2 server reads each node's `cs_agent_queue_numworkers` + 1 instead. |
| `backup_server_sshd_penalty_exempt_extra` | `[]` | More `address[/masklen]` entries for `PerSourcePenaltyExemptList`, such as node subnets. Required in attach mode with `backup_server_sshd_manage`. |

Consumed, not owned: `borg_version`, `borg_release_gpg_fingerprint`,
`borg_binary_path` (versions.yml), `backup_host_path`, `site`, `existing_env`
(inventory). The role reads no site map itself; it reads `cs_site`,
`primary_ip`, `public_ip` and `cs_agent_queue_numworkers` of the nodes in its
own site for sshd, and `cs_agent` and `validate` use `site`
to decide which backup server a given node belongs to.
