# node_upgrade

The maintenance cycle of live nodes: the rolling docker upgrade
(`playbooks/upgrade-docker.yml`, `make upgrade-docker ENV=<env>`) and the
converge of live nodes inside a maintenance hold
(`playbooks/maintain-nodes.yml`, `make maintain-nodes ENV=<env>`), one node
at a time, inside a scheduled customer maintenance window. Never unattended.
Its `detect_live` probe also keeps the converges from disrupting a live node.

| Runs in | Play | Hosts |
| --- | --- | --- |
| `upgrade-docker.yml` | "Upgrade docker, one node at a time" | `node_upgrade_targets` |
| `upgrade-docker.yml` (`tasks_from: preflight`) | "Preflight every targeted node" | `node_upgrade_targets` |
| `maintain-nodes.yml` (`tasks_from: maintain`) | "Maintain the nodes, one at a time" | `maintain_node_targets` |
| `maintain-nodes.yml` (`tasks_from: preflight`, `maintain_preflight`) | "Preflight every targeted node" | `maintain_node_targets` |
| `site.yml` (`tasks_from: detect_live`) | "Docker hosts" | `nodes` |
| `add-region.yml` (`tasks_from: detect_live`) | "New node docker" | `nodes` (the new node) |
| `roles/common` (`tasks_from: detect_live`) | "Base system" / "New node base system", before a reboot | nodes, not `existing_env` |
| `unseal.yml` | not run | — |

**Attach mode:** never touches an `existing_env` host — neither playbook
selects one, and the role's preflight refuses it. **Requires:** a provisioner-built node with the data disk `ready`
(`node_storage`) and the contract dependencies below.

## Purpose

Upgrades docker on a provisioner-built **node** to the pins in
`docker_apt_packages` (versions.yml), applies the pending OS updates, reboots
when one is required, and puts back exactly the containers that were running,
without the controller interfering and without restarting anything that had
been stopped on purpose. It replaces the manual procedure (stop the
controller, unhold, `docker stop $(docker ps -aq)`, `apt upgrade`,
`docker start $(docker ps -aq)`, hold), which started every stopped container
too.

It also provides `tasks_from: detect_live`, the one "is this node live" probe
that keeps `site.yml` from doing the same disruptive things outside a window.

Scope: `groups['nodes']` minus every `existing_env` host. The controller,
metrics and registry docker hosts are not covered.

**Contract dependencies:**

* **cs-agent >= 3.4.0** on every node (`cs-agent maintenance`). Checked by
  preflight. upgrade-docker never installs it (an agent install restarts the
  agent, which is not something to do mid-window); maintain-nodes upgrades it
  to its pin inside the hold, during the converge, and waits for it.
* **controller >= 9.8.0**, which treats an agent hold as maintenance (no
  heartbeat-driven offline/evacuation, no HealthCheck/RecoverContainer, no
  placement, customer power actions canceled) and, on release, kicks
  HealthCheck, the load balancer config push and a changelog poll.
* **The container data disk** (`roles/node_storage` state `ready`) on every
  target. Checked by `playbooks/upgrade-docker.yml`'s preflight play before
  this role's own preflight, never set up here; a node built before the
  data-disk requirement must be rebuilt.

## Flow (`tasks/main.yml`)

| Step | File | Phase written | What happens |
| --- | --- | --- | --- |
| preflight | `preflight.yml` | — | Not `existing_env`; docker-ce installed; cs-agent >= min (`dpkg --compare-versions`); `maintenance status --json` readable; while the package phase is still ahead: `apt-get update` (waits for the dpkg lock), every drifting pin exists on the mirror, no pin is below what is installed. |
| decide | `main.yml` | — | Run when docker drifts from a pin, `/var/run/reboot-required` exists, a `state.json` exists (resume), or `node_upgrade_force`. Otherwise print "nothing to do" and take no hold. `--check` stops here. |
| download | `download.yml` | — | `apt-get update`, then `--download-only` of the OS upgrade and the drifting pins — containers still running, so a mirror problem fails before anything is held. |
| hold | `hold.yml` | `held` | Fresh run only: `cs-agent maintenance on --reason docker-upgrade --wait --timeout 7200s --settle 30s --json` under `async`/`poll`; repeated only while it places no hold (`not_placed`, up to 3 more times, 30s apart). Resume: requires the existing hold, never calls `on`. |
| record | `record.yml` | `recorded` | `docker container inspect` of every container; the running non-system ones go into `state.json` **before** anything stops. |
| stop | `stop.yml` | `stopped` | Hold re-check; `docker stop -t 30` the recorded ones still running, 8 at a time; assert none is running. |
| packages | `packages.yml` | `packages` | Hold re-check; `os_upgrade.yml`: re-hold the docker packages, then `apt-get upgrade --with-new-pkgs` with them **held** (async, so a dropped connection does not kill dpkg); then release the holds → install the pins exactly → re-hold (in `always:`); assert no drift remains. |
| reboot | `reboot.yml` | `rebooted` | Reboot if `/var/run/reboot-required` exists now. |
| services | `services.yml` | — | docker started; docker, cs-agent, cs-firewall active; `docker info` answers; hold re-check. |
| start | `start.yml` | `started` | Start the recorded set, 8 at a time, re-starting what is down every 15s for up to 600s, at most 5 attempts each; fail on what is still down, minus `node_upgrade_accept_down`. |
| release | `release.yml` | `released` | `releasing: true` saved, `cs-agent maintenance off`, then poll `status --json` until `controller_acked` and not `paused` (up to 600s) — else fail. |
| archive | `archive.yml` | — | `state.json` → `state.done.<UTC timestamp>.json`, keep the newest 5. |
| settle | `cycle.yml` | — | Pause 60s so the controller's HealthCheck has seen the node before the next one. |

The playbook then runs `roles/validate` against the same node before the next
node starts.

## The container rule

Recorded = `State.Running` is true **and** label `com.computestacks.role` is
not exactly `system`.

* `system` containers (cadvisor, fluentd) are systemd units running
  `docker run --rm`: their unit brings them back, and docker deletes them on
  stop, so a recorded id would never exist again.
* Everything else is controller-created and has **no docker restart policy**
  — tenant services, SFTP bastions (`role=bastion`), registry containers — so
  after a docker restart or a reboot it stays down until something starts it.
* A container that was not running stays not running.
* Borg backup containers are already drained by `maintenance on --wait`.

The role starts the set itself, before releasing the hold, because the
controller's HealthCheck marks a container inactive after more than three
failed starts in an hour — an app racing its own database would burn through
that.

After a restart or reboot, recorded containers are matched by id, falling back
to name; one with neither is reported as removed.

## The state file

`/var/lib/computestacks/upgrade/state.json` (dir 0700, file 0600, root):

```json
{"version": 1, "host": "node1001", "playbook": "upgrade-docker", "reason": "docker-upgrade",
 "started_at": "...", "updated_at": "...",
 "recorded": [{"id": "<64 hex>", "name": "web-app-1"}],
 "phase": "stopped", "phases_done": ["held", "recorded", "stopped"],
 "accept_down": []}
```

It is written right after the hold succeeds, and `recorded` is filled
**before** the first stop. Each step runs only while its phase is missing from
`phases_done`, so a re-run resumes:

* `recorded` is written once; a resumed run never re-records.
* Once `packages` or `started` is done, nothing is ever stopped again.
* A hold is only ever **taken** on a fresh run. With no state file, a local
  hold already on the node belongs to somebody else and the run stops: the
  role never adopts a hold it did not place.
* `releasing` is set just before `off`, so a run that died between `off` and
  the `released` mark resumes at the confirmation instead of reading the
  missing hold as an external removal.
* `playbook` names the playbook that owns the run; upgrade-docker.yml and
  maintain-nodes.yml each refuse to resume the other's (a file without it is
  upgrade-docker's).

Finished runs are kept as `state.done.<timestamp>.json` (the newest
`node_upgrade_state_keep`). The role also writes `stop.json`/`start.json`
(the helper's inputs) beside it and removes them on success, and installs
`/usr/local/libexec/computestacks/node-upgrade-containers`.

## `cs-agent maintenance on` results

Classified by JSON **shape first**, rc second
(`filter_plugins/node_upgrade.py`, `node_upgrade_classify_on`), because in
cs-agent's `maintenance_cli.go` one rc comes from paths that leave the node in
different states:

| Output | rc | Outcome | Node |
| --- | --- | --- | --- |
| status | 0 | ok | held, quiet, controller acked |
| status | 2 | timeout | held; `running` lists what is still in flight |
| status, `local` null | 4 | not_placed | not held: no maintenance-capable controller poll in 120s. The only outcome retried: `on` again, up to `node_upgrade_hold_not_placed_retries` (3) times, `node_upgrade_hold_not_placed_delay` (30s) apart (filter `node_upgrade_on_retry`) |
| status, `local` set | 4 | unconfirmed | held, the controller never confirmed |
| status, `local` null | 1 | cleared | not held: somebody ran `off` while it waited |
| status, `local` set | 1 | interrupted | held: the CLI got SIGINT/SIGTERM |
| error `{"error","exit_code"}` | any (1, 2, 3, 4) | error | **unknown** — the CLI could not read its own state |
| nothing parseable | any | no_output | unknown |
| status without a hold | 0 | failed | never treated as success |

`local` set after `on` is always this run's hold, because the hold step only
calls `on` when the status read just before it showed no local hold.

## Failures and what to do

Every failure message says whether the node is held and what to do next. The
rule behind them: **`cs-agent maintenance off` is suggested only while no
state file exists**, i.e. before anything was stopped. Once it exists, clearing
the hold by hand would let the controller start recovering the node
mid-upgrade; the fix is to correct the cause and re-run
`make upgrade-docker ENV=<env> LIMIT=<node>`, which resumes.

| Where | Node state | Operator action |
| --- | --- | --- |
| preflight / decide / download | untouched, not held | fix the cause, re-run |
| a foreign local hold, no state file | held by someone else | wait for them, or `off` it if it is a leftover of this playbook (reason `docker-upgrade`), then re-run |
| `on --wait` failed | per the table above | the message says; `off` only when it says held |
| hold removed externally (re-checks before stop, before packages, after reboot / before start; and on resume) | NOT held, part-way | do not re-run blindly: check the admin UI and `docker ps -a` against `recorded`, then re-hold (`cs-agent maintenance on --reason docker-upgrade`) and re-run, or finish by hand and move the state file aside |
| stop / packages / reboot / services | held, containers stopped | fix, re-run (resumes) |
| start: containers still down | held, the rest running | `docker logs` them; re-run, or re-run with `-e node_upgrade_accept_down=<name>[,…]` |
| release not confirmed | released locally, running | check the node in the admin UI (a controller-side hold keeps it paused); re-run to finish |
| ssh lost / unreachable (not rescued by ansible) | whatever phase it reached | re-run; it resumes from `state.json` |

Any other task failure inside the cycle is re-raised with the node's phase
and these instructions appended (`templates/cycle_failed.j2`).

## maintain-nodes (`tasks_from: maintain`)

`playbooks/maintain-nodes.yml` converges LIVE nodes -- the node-side roles of
`add-region.yml`'s node plays -- each inside a cs-agent maintenance hold the
run places and releases itself, and stops the tenant containers only when
the node needs dockerd restarted or a reboot. It reuses this role's hold,
record/stop/packages/reboot/services/start, release and archive steps and its
state file; what is new is in `tasks/maintain*.yml`, `agent_wait.yml`,
`docker_restart.yml` and the filters at the end of
`filter_plugins/node_upgrade.py`. docs/maintain-nodes.md is the operator's
view.

### Flow

| Step | File | Phase written | What happens |
| --- | --- | --- | --- |
| preflight | `preflight.yml` | — | As upgrade-docker (agent >= 3.4.0, status readable, pins on the mirror, no docker downgrade), plus R7: a state.json of the other playbook is refused. |
| converge preflight | `maintain_preflight.yml` | — | `cs_agent_apt_version` not below the installed agent (never downgrade); `cs_agent` `tasks_from: immutable_check` (R4, reads the controller); on a tailnet member (`tailscale_enabled: true`), `roles/tailscale` `tasks_from: status`: the node must be logged in (maintenance never joins a node, so no maintain run needs an auth key) and its `tailscale debug prefs` must match the role's flags (R8). Before ANY hold: fleet-wide in the preflight play, and again per node. |
| download | `download.yml` | — | `apt-get update`, `--download-only` of the OS upgrade and the drifting pins, containers running, no hold. |
| hold | `hold.yml` | `held` | `cs-agent maintenance on --reason maintain-nodes --wait ...`; resume requires the existing hold. state.json gets `playbook: maintain-nodes`. |
| converge | `maintain_converge_phase.yml` → `maintain_converge.yml` | — | `converge_started` saved first. Then, containers RUNNING: common (its reboot off), node_exporter, tailscale (members), ubuntu_pro (token), docker_config, docker_tls (restart deferred), node_kernel, haproxy, node_observability, firewall, cs_agent (pin installed, never downgraded). |
| handlers | `maintain_converge_phase.yml` | — | On a resumed converge, every lost handler is notified again first (see "Resumed converge" below). Then `meta: flush_handlers` inside the hold (R1). |
| agent | `agent_wait.yml` | — | Poll `maintenance status --json` until `agent_active` and a local hold (`node_upgrade_agent_wait`, 300s). Exit 3 and no output are retried; no hold, or another error document, fails at once. `controller_acked` is not read. |
| OS updates, live | `maintain_os_updates.yml`, `os_upgrade.yml` | — | Containers RUNNING (R3). List the pending OS updates (kept for the report and state.json), re-hold the docker packages, `apt-get upgrade --with-new-pkgs` (`node_upgrade_apply_os_updates`; async, needrestart suspended). Then the agent wait again (one poll when nothing restarted it), which also confirms the hold. |
| decide | `maintain_decide.yml` | `converged` | Stop iff docker drifts, dockerd must restart (docker_tls' deferred reason, or a docker_tls file newer than the running dockerd), `/var/run/reboot-required` exists (the live OS updates may just have written it), or `maintain_force_restart`. The decision, its inputs and the OS updates applied live (`os_live`) are written with the phase (R2). |
| report | `maintain_report.yml` | — | "stop containers: yes/no because ...", the tenant count, the OS updates applied live and any still pending (those that usually ask for a reboot named). |
| record, stop, packages | as upgrade-docker | `recorded`, `stopped`, `packages` | Only when the decision says stop. packages' OS upgrade is normally a no-op now; it catches anything new and keeps a resumed run correct. |
| docker restart | `docker_restart.yml` | `docker_restarted` | When the decision requires it and no reboot follows. |
| reboot, services, start | as upgrade-docker | `rebooted`, `started` | |
| release | `release.yml` | `released` | `meta: flush_handlers` first (a no-op: nothing notifies after the converge), then `off` and the controller confirmation. |
| archive, settle | as upgrade-docker | — | |

### `--check --diff`

`tasks/maintain_preview.yml`: both preflights, then the converge in check mode
with its diffs (agent.yml stays `no_log`), the handlers (none acts; docker_tls
records its reason), the decision, the pending OS updates (which a real run
would apply live, without a stop), and per node "would stop containers:
yes/no because ...". The decision there is taken before any OS update, so
when a listed update usually asks for a reboot (kernel, libc: `reboot_hint`)
the report says the run would then stop the containers and reboot. NO hold. A docker certificate due
for renewal is reported, not issued, and counts as "docker restart required".
The tailscale role is not previewed (the collection's own status read cannot
run in check mode); preflight already proved the node logged in with the
role's settings. An
unfinished run whose converge is done is previewed from its recorded
decision.

### The rules that keep tenants safe

* **R1 -- no handler after `off`, no dockerd restart under tenants.**
  `docker_tls_restart_deferred: true` is a play var of the serial play, so
  docker_tls' handlers see it: "Restart docker" does not run, "Record a
  deferred docker restart" does. `docker_config`'s handler is a SIGHUP reload
  and fires inside the hold. The converge's handlers are flushed right after
  it, before the agent wait and the decision, and again before the release;
  no task of this role notifies anything except the resumed converge's
  re-apply task, and that never a dockerd restart. `tests/maintain_nodes.yml` checks
  all of it statically, including that no handler of a converged role
  restarts dockerd without the gate. `tasks/maintain.yml` refuses to run
  without the play var.
* **R2 -- decide once, from state.** The decision and its inputs are written
  with `converged`; a resumed run reads them and never re-decides. The
  docker_tls file check also re-detects a restart a dead run did not record.
* **R3 -- OS updates.** Never a reason to stop on their own. They are
  applied LIVE, inside the hold, after the agent wait and before the
  decision (`tasks/os_upgrade.yml`, the same file the stopped window's
  `packages.yml` runs): docker's packages re-held first (cs-agent,
  node_exporter and haproxy are held by their roles), needrestart's apt hook
  suspended and its pending run cleared (so a later apt run does not restart
  those services with tenants up), async so a dropped connection does not kill dpkg. Because the
  decision reads `/var/run/reboot-required` afterwards, an update that asks
  for a reboot stops the containers and reboots in the same run. No role in
  the converge runs `apt upgrade`; they install at `present`, the agent and
  node_exporter at their pins. A resumed converge applies them again (an
  orphaned apt job may still hold the dpkg lock, and the roles' installs
  wait for it only briefly: a run re-started at once can fail on it, held;
  re-run when the job is done); a dpkg left half-way is not
  repaired automatically -- the failure message says to run
  `dpkg --configure -a` (a half-configured docker package would restart
  dockerd), with the node still held.
* **R5 -- releasing after a failure.** If the run fails after its own hold
  was RECORDED and before the converge's first task (state.json with only
  `held`, no `converge_started`), nothing has changed and the hold is
  released and the state set aside (`state.aborted.<timestamp>.json`). A
  failure while PLACING the hold (`on --wait` timed out, unconfirmed or
  interrupted, or the state write after it failed) leaves no state.json, so
  nothing is released automatically, and a re-run refuses the existing
  local hold: `hold_failed.j2` / `maintain_failed.j2` say whether the node is
  held, and since nothing was stopped or changed, `cs-agent maintenance off`
  (after a look at `cs-agent maintenance status` and `docker ps`) is safe. From the converge on,
  ansible offers no reliable "did any task change something" for included
  roles, so the node stays held and `templates/maintain_failed.j2` says what
  to do: re-run (it resumes inside the hold), or, as long as nothing was
  stopped, `cs-agent maintenance off` is safe for the tenants.
* **Resumed converge -- no lost handler.** A run that died after a role
  changed a file but before the flush lost that role's pending handlers, and
  on resume the same task reports ok. So when the converge phase runs with
  `converge_started` already in state.json (and `converged` not done), it
  notifies, after the converge and before the flush, every handler of the
  converged roles by `role : name`: `common : Restart ssh`,
  `docker_config : Reload docker` (SIGHUP), `node_kernel : Load node kernel
  modules`, `node_kernel : Reload sysctl` (`sysctl --system`),
  `node_kernel : Reload systemd-networkd` (still only when
  `node_kernel_uplink_ra_reload`), `node_observability : Restart cadvisor` /
  `Restart fluentd`, `firewall : Reload systemd for the ComputeStacks
  firewall` / `Apply the ComputeStacks firewall`, and `cs_agent : Restart
  cs-agent` -- the roles' own handlers, so they behave exactly as in a first
  run. They run inside the hold, and the agent wait follows the cs-agent
  restart. docker_tls' handlers are never notified: a lost docker restart is
  caught by the decision's docker_tls file check (a file newer than the
  running dockerd) and goes through the stopped window. `tests/maintain_nodes.yml`
  keeps that list equal to the converged roles' handlers minus docker_tls'.
* **R7 -- one owner per state file.** `playbook` is recorded in state.json;
  each playbook refuses to resume the other's (a file without the field is
  upgrade-docker's). Their hold reasons differ too (`maintain-nodes` /
  `docker-upgrade`), so neither adopts the other's hold.

### Off the node

What a maintain run touches outside the node it is converging, and nothing
else (the playbook refuses a LIMIT that leaves the first two out):

| Host | By | What |
| --- | --- | --- |
| controller | `cs_agent` enroll (preflight and converge) | `cstacks runner` read of the node's agent token hash -- a read |
| controller | `docker_tls` (only when the node's certificate is missing, within `docker_tls_renew_days` of expiry, or its SANs changed) | `vault-bootstrap`: inspect; `docker start` it if stopped; unseal it if sealed; `vault write pki/docker/issue/server` (a new leaf certificate) |
| the node's backup host | `cs_agent` | `authorized_key` entry `cs-agent <hostname>` for `cstacks` -- only if missing |
| the tailnet's coordination server | `tailscale` (members) | nothing beyond the running daemon: the converge never runs `tailscale up` (the node is logged in, `tailscale_join_allowed: false`) |
| Ubuntu Pro | `ubuntu_pro` (token set) | `pro attach` only if not attached; `pro enable` of missing services |
| the registries in `docker_registries` | `docker_config`, `node_observability` | `docker login`; pulls of pinned images not present |
| apt mirrors, the cs-agent repository | several | package lists, packages |
| the control machine | `docker_tls` | `x509_certificate_info` of the node's certificate (localhost) |

Not run: node_storage (only its read-only probe), geerlingguy.docker,
ssh_trust, controller_seed, controller_post_enroll, and every role that
configures the controller, metrics, backup or nameserver hosts.

### State file additions

`playbook` (`maintain-nodes`), `converge_started`, and with `converged` the
`decision`: `{stop, reasons, docker_restart_required, docker_restart_reasons,
drift, reboot_pending, force}`; `docker_restarted` with that phase.

## Variables (`defaults/main.yml`)

| Variable | Default | |
| --- | --- | --- |
| `node_upgrade_state_dir` / `_state_file` / `_state_keep` | `/var/lib/computestacks/upgrade`, `…/state.json`, 5 | |
| `node_upgrade_cs_agent_min_version` | `3.4.0` | |
| `node_upgrade_maintenance_timeout` | 7200 (s) | `--timeout`; must be >= 300 (controller polls every 15s) |
| `node_upgrade_maintenance_settle` | 30 (s) | `--settle` |
| `node_upgrade_hold_not_placed_retries` / `_delay` | 3 / 30 (s) | repeat `on` when it placed no hold (`not_placed`) |
| `node_upgrade_reason` | `docker-upgrade` | the hold's reason |
| `node_upgrade_release_timeout` | 600 (s) | wait for the controller after `off` |
| `node_upgrade_stop_timeout` | 30 (s) | `docker stop -t` |
| `node_upgrade_parallel` | 8 | concurrent stops/starts |
| `node_upgrade_start_wait` / `_interval` / `_retries` | 600 / 15 / 5 | the tolerant start |
| `node_upgrade_accept_down` | `[]` | names or ids (12+ chars) allowed to stay down |
| `node_upgrade_apply_os_updates` | true | apply the pending OS updates: upgrade-docker in its stopped window, maintain-nodes live inside the hold; never a trigger on their own |
| `node_upgrade_apt_lock_timeout` | 900 (s) | dpkg lock wait |
| `node_upgrade_reboot_timeout` | 900 (s) | |
| `node_upgrade_services_timeout` | 300 (s) | docker/cs-agent/cs-firewall after packages/reboot |
| `node_upgrade_force` | false | run the cycle even with nothing to do |
| `node_upgrade_node_settle` | 60 (s) | pause before the next node |
| `node_upgrade_playbook` | `upgrade-docker` | recorded in state.json (R7); `maintain-nodes.yml` sets `maintain-nodes` |
| `node_upgrade_maintain_force_restart` | `maintain_force_restart`, else false | maintain-nodes: stop (and reboot if one is pending) even with no other reason |
| `node_upgrade_agent_wait` / `_interval` | 300 / 5 (s) | maintain-nodes: wait for cs-agent after the converge |

## `tasks_from: detect_live`

Read-only, check-mode safe (`check_mode: false` + `changed_when: false` on
every probe), safe without docker, and self-contained (reads only
`docker_apt_packages`). Sets:

* `node_upgrade_live` — the docker CLI exists and at least one container, in
  any state, is not `system`. **Also true when the CLI exists but dockerd does
  not answer** (`node_upgrade_live_unknown`): an unlistable node cannot be
  shown empty, and the guards' failure mode in that direction is a skipped
  step with a warning, not a reboot under live tenants.
* `node_upgrade_docker_drift` — docker-ce is installed and some pinned package
  is installed at another version or not at all. False without docker-ce.
* `node_upgrade_docker_installed`, `node_upgrade_drift_list`
  (`[{name, pin, installed}]`), `node_upgrade_installed` (`{package: version}`).

## Implementation choices

* **The rules live in a filter plugin** (`filter_plugins/node_upgrade.py`),
  applied by two pure task files (`select.yml`, `classify_on.yml`) that
  `tests/node_upgrade_select.yml` drives with the fixtures in
  `tests/fixtures/node_upgrade/` (`make check`). One spelling of the selection
  rule and of the classification, with the reasoning in the docstrings.
* **Batching** is a small Python helper shipped to the node
  (`files/node-upgrade-containers`, a thread pool over `docker stop`/`start`,
  with the start retry loop), run under `async`/`poll`. One round trip per
  phase instead of one per container, and an ssh drop does not kill it.
* **Version comparisons use `dpkg --compare-versions` on the node**, not
  ansible's `version` test: Debian epochs and `~` pre-releases sort wrongly
  in a loose comparison.
* **The download runs after the decision**, not inside preflight: a node with
  nothing to do downloads nothing, and the fleet-wide preflight play stays
  read-only apart from `apt-get update`.
* **Pending OS updates do not trigger a cycle** — they ride along with one.
  unattended-upgrades already applies the security ones.
* **`--check`** runs preflight and the decision for every node and stops.
