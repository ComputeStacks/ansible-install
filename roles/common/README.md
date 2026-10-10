# common

Base system convergence for every host: packages, a reboot when one is
pending, sshd hardening, chrony, unattended-upgrades, the MOTD fragment and
root's SSH directory. Sole owner of `/etc/update-motd.d/50-computestacks`
(docs/contracts.md §Ownership).

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Base system" | `all` |
| `add-region.yml` | "New node base system" | `nodes` (the new node) |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

**Attach mode:** new node only; never runs on an existing shared host, and
never reboots an `existing_env` host even if pointed at one. **Requires:**
`preflight` first; calls `roles/node_upgrade` (`tasks_from: detect_live`)
itself when a node has a reboot pending.

## What it does

- Removes packages that conflict with the rest of the stack (`ufw`, `ntp`),
  installs a curated base package set (`common_packages`).
- Reboots the host when `/var/run/reboot-required` exists (a kernel/module
  update pulled in by package install), unless `common_allow_reboot` is set
  false or the host is flagged `existing_env: true` — attach mode must never
  reboot a live v1 controller/metrics/backup host. Runs right after package
  install (see "Reboot right after package install" for why).
- Never reboots a **live node** either: a host in `nodes` that already carries
  tenant containers, per `roles/node_upgrade`'s `tasks_from: detect_live`
  (probed only when a reboot is pending). It prints a warning instead —
  "reboot pending on live node" — and the reboot is taken in a maintenance
  window by `make upgrade-docker ENV=<env>`, which reboots when one is
  required. A node with no tenants yet (a first build) and every non-node
  host reboot as before.
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
| `common_allow_reboot` | `true` | Reboot when `/var/run/reboot-required` exists. Never fires on `existing_env` hosts or on live nodes regardless of this setting. |

Consumed, not owned: `hostname`, `primary_ip`, `region`, `az` (node inventory
vars, asserted present by `preflight`).

## Reboot right after package install

The initial package install can pull in a new kernel and remove the running
kernel's `/lib/modules`, after which `modprobe` of `br_netfilter`/`nf_nat`/etc.
fails and dockerd cannot build its firewall chains. The reboot check therefore
lives here, before docker is ever installed, rather than in the docker roles,
and it is targeted and convergent: reboot only when `/var/run/reboot-required`
actually exists, never on `existing_env` hosts, never on a live node. `common`
has no distinct "apt upgrade" task (package install uses `state: present`, not
`state: latest`), so the check runs immediately after package install rather
than after a separate upgrade step.
