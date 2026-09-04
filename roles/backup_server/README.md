# backup_server

**Owner:** Wave 2D. **Hosts:** `backup` (one shared server for the whole fleet).

The borg backup server every node's cs-agent SSHes into: the `cstacks` account,
the pinned upstream borg release, and the repository path.

## What it does

1. **`cstacks` system account**, shell `/bin/bash`, password locked (`!`) — key
   auth only. Its `~/.ssh` is created 0700; the per-node `authorized_keys`
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
   `/var/lib/computestacks/backups`), owned `cstacks:cstacks`.

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

Only `tasks/account.yml` runs: the `cstacks` account, its `authorized_keys`
directory, and the repository path's ownership. Everything else on a live
server is left alone — in particular v2's borg is **not** installed over the
server's existing one.

That makes two inventory inputs **required** on the nodes attaching to such a
server (they describe the existing environment and have no safe default):

- `backup_host_path` — v1 servers use `/mnt`;
- `backup_borg_remote_path` — v1 servers use the distro borg at
  `/usr/bin/borg`. The v2 default (`/opt/computestacks/bin/borg`) does not
  exist there, and every backup on the new node would fail with "command not
  found".

Set `backup_server_install_borg: true` to install v2's pinned borg alongside
the existing one anyway — and then point `backup_borg_remote_path` at it.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `backup_server_user` / `backup_server_group` | `cstacks` | The account nodes back up as. |
| `backup_server_shell` | `/bin/bash` | Login shell (the agent runs shell commands over SSH). |
| `backup_server_host_path` | `backup_host_path` or `/var/lib/computestacks/backups` | Repository path. |
| `backup_server_host_path_mode` | `0770` | Mode applied to it. |
| `backup_server_prefix` | `/opt/computestacks` | Install prefix (`borg/<version>/`, `bin/`, `tmp/`). |
| `backup_server_borg_asset` | `borg-linux-glibc231-x86_64` | Upstream release asset (amd64 only — preflight asserts the architecture). |
| `backup_server_borg_keyservers` | 3 keyservers | Tried in turn; all failing is a hard error. |
| `backup_server_install_borg` | `false` | Install borg on an `existing_env` server anyway. |

Consumed, not owned: `borg_version`, `borg_release_gpg_fingerprint`,
`borg_binary_path` (versions.yml), `backup_host_path`, `existing_env`
(inventory).

## Deviations from v1 (`roles/backup`)

- Upstream GPG-verified borg release instead of `apt install borgbackup`
  (`state: latest`, which floated the version out from under the client image).
- No NFS export path, no `/etc/exports`, no `exportfs`.
- No root SSH key distribution (v1 pushed every node's **root** key into the
  backup server's `root` `authorized_keys` — v2 nodes only ever authenticate as
  `cstacks`, with a dedicated per-node key).
- No borg compaction cron or logrotate for it: compaction is in-agent now.
- Not behind `tags: ['never', 'bootstrap']` — the role is convergent and runs
  in a normal converge.
