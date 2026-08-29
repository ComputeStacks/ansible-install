# common

**Owner:** Wave 1A

Base system convergence applied to every host (`hosts: all`). Sole owner of
`/etc/update-motd.d/50-computestacks` (docs/contracts.md ownership map).

## What it does

- Removes packages that conflict with the rest of the stack (`ufw`, `ntp`),
  installs a curated base package set (`common_packages`).
- Reboots the host when `/var/run/reboot-required` exists (a kernel/module
  update pulled in by package install), unless `common_allow_reboot` is set
  false or the host is flagged `existing_env: true` — attach mode must never
  reboot a live v1 controller/metrics/backup host. Runs right after package
  install (see "Deviations from v1" for why).
- Hardens sshd: `PasswordAuthentication no` (restarts `ssh` on change).
- Ensures `chrony` is installed, enabled, and running.
- Installs and enables `unattended-upgrades` (periodic + unattended apt
  upgrades on).
- Renders `/etc/update-motd.d/50-computestacks` (executable script) from
  **inventory vars only** — never gathered facts — so a fragment rendered
  under `--limit` still shows every region a shared host serves:
  - a node shows its own `hostname`/`primary_ip`/`region`/`az`;
  - the controller shows `hostname`/`primary_ip` and the literal role
    `controller`;
  - any other (shared) host shows `hostname`/`primary_ip`, a role label
    derived from its inventory group membership, and the full list of
    region/az pairs served, derived by iterating `groups['nodes']` hostvars
    (sorted, unique).
  Also empties the static `/etc/motd` so only the dynamic fragment renders.
- On the controller only: generates a root ed25519 keypair at
  `/root/.ssh/id_ed25519` if absent (`regenerate: never`) — consumed by the
  `ssh_trust` role.
- Ensures `/root/.ssh` exists (mode 0700) on every host.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `common_packages` | see `defaults/main.yml` | Base package set installed on every host. |
| `common_conflicting_packages` | `[ufw, ntp]` | Packages removed before install. |
| `common_allow_reboot` | `true` | Reboot when `/var/run/reboot-required` exists. Never fires on `existing_env` hosts regardless of this setting. |

Consumed, not owned: `hostname`, `primary_ip`, `region`, `az` (node inventory
vars, asserted present by `preflight`).

## Deviations from v1 (`roles/base`)

- **Dropped the `tmux.conf`-from-personal-gist download entirely** (per Wave
  1A instructions) — the `tmux` package is still installed, but no custom
  dotfile is fetched.
- **Package list curated**, dropping: `linux-headers-amd64` (Debian-specific
  package name, no Ubuntu equivalent asserted needed here), `apt-transport-https`
  (no-op since APT 1.5+), `gnupg-agent` (functionality merged into `gnupg`),
  `pass` (a personal password-store tool, unrelated to server operations),
  and `nfs-common` (NFS backend is stripped from v2 — SSH/borg only, per the
  v2 plan). Renamed `gnupg2` -> `gnupg` (the current, non-transitional
  package name).
- **Service names corrected for Debian/Ubuntu**: v1's handlers used `sshd`
  and `chronyd` (RHEL-style unit names); this role uses the correct
  Debian/Ubuntu unit names `ssh` and `chrony`.
- MOTD is rendered as an executable `/etc/update-motd.d/50-computestacks`
  script (dynamic MOTD), not a static `/etc/motd` template — v1 wrote
  `/etc/motd` directly. Static `/etc/motd` is now emptied instead.
- Does not port v1's `/etc/hosts` localhost line or DNS/admin-credential
  preflight checks — out of this role's scope for Wave 1A (see `preflight`
  and, later, `validate`).
- **Replaces v1's unconditional post-docker-install reboot hack** (v1
  rebooted every host, unconditionally, right after installing docker) with
  a targeted, convergent check: reboot only when
  `/var/run/reboot-required` actually exists, and never on `existing_env`
  hosts. Root cause (found by Wave 1B while investigating the docker role):
  the initial package install can pull in a new kernel and remove the
  running kernel's `/lib/modules`, after which `modprobe` of
  `br_netfilter`/`nf_nat`/etc. fails and dockerd cannot build its firewall
  chains — so the fix belongs here, before docker is ever installed, not in
  the docker roles. `common` has no distinct "apt upgrade" task (package
  install uses `state: present`, not `state: latest`), so this task runs
  immediately after package install rather than after a separate upgrade
  step.
