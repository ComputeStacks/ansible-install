# node_upgrade

**Owner: the rolling docker upgrade.** Run by `playbooks/upgrade-docker.yml`
(`make upgrade-docker ENV=<env>`), one node at a time, inside a scheduled
customer maintenance window. Never unattended.

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
  preflight, never installed: an agent install restarts the agent, which is
  not something to do mid-window.
* **controller >= 9.8.0**, which treats an agent hold as maintenance (no
  heartbeat-driven offline/evacuation, no HealthCheck/RecoverContainer, no
  placement, customer power actions canceled) and, on release, kicks
  HealthCheck, the load balancer config push and a changelog poll.

## Flow (`tasks/main.yml`)

| Step | File | Phase written | What happens |
| --- | --- | --- | --- |
| preflight | `preflight.yml` | — | Not `existing_env`; docker-ce installed; cs-agent >= min (`dpkg --compare-versions`); `maintenance status --json` readable; while the package phase is still ahead: `apt-get update` (waits for the dpkg lock), every drifting pin exists on the mirror, no pin is below what is installed. |
| decide | `main.yml` | — | Run when docker drifts from a pin, `/var/run/reboot-required` exists, a `state.json` exists (resume), or `node_upgrade_force`. Otherwise print "nothing to do" and take no hold. `--check` stops here. |
| download | `download.yml` | — | `apt-get update`, then `--download-only` of the OS upgrade and the drifting pins — containers still running, so a mirror problem fails before anything is held. |
| hold | `hold.yml` | `held` | Fresh run only: `cs-agent maintenance on --reason docker-upgrade --wait --timeout 7200s --settle 30s --json` under `async`/`poll`. Resume: requires the existing hold, never calls `on`. |
| record | `record.yml` | `recorded` | `docker container inspect` of every container; the running non-system ones go into `state.json` **before** anything stops. |
| stop | `stop.yml` | `stopped` | Hold re-check; `docker stop -t 30` the recorded ones still running, 8 at a time; assert none is running. |
| packages | `packages.yml` | `packages` | Hold re-check; `apt-get upgrade --with-new-pkgs` with the docker packages **held**; then release the holds → install the pins exactly → re-hold (in `always:`); assert no drift remains. |
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
{"version": 1, "host": "node1001", "reason": "docker-upgrade",
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
| status, `local` null | 4 | not_placed | not held: no maintenance-capable controller poll in 120s |
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

## Variables (`defaults/main.yml`)

| Variable | Default | |
| --- | --- | --- |
| `node_upgrade_state_dir` / `_state_file` / `_state_keep` | `/var/lib/computestacks/upgrade`, `…/state.json`, 5 | |
| `node_upgrade_cs_agent_min_version` | `3.4.0` | |
| `node_upgrade_maintenance_timeout` | 7200 (s) | `--timeout`; must be >= 300 (controller polls every 15s) |
| `node_upgrade_maintenance_settle` | 30 (s) | `--settle` |
| `node_upgrade_reason` | `docker-upgrade` | the hold's reason |
| `node_upgrade_release_timeout` | 600 (s) | wait for the controller after `off` |
| `node_upgrade_stop_timeout` | 30 (s) | `docker stop -t` |
| `node_upgrade_parallel` | 8 | concurrent stops/starts |
| `node_upgrade_start_wait` / `_interval` / `_retries` | 600 / 15 / 5 | the tolerant start |
| `node_upgrade_accept_down` | `[]` | names or ids (12+ chars) allowed to stay down |
| `node_upgrade_apply_os_updates` | true | OS updates ride along; never a trigger on their own |
| `node_upgrade_apt_lock_timeout` | 900 (s) | dpkg lock wait |
| `node_upgrade_reboot_timeout` | 900 (s) | |
| `node_upgrade_services_timeout` | 300 (s) | docker/cs-agent/cs-firewall after packages/reboot |
| `node_upgrade_force` | false | run the cycle even with nothing to do |
| `node_upgrade_node_settle` | 60 (s) | pause before the next node |

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
