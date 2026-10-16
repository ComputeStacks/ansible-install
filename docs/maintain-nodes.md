# Maintaining live nodes (`maintain-nodes.yml`)

`make maintain-nodes ENV=<env>` brings nodes that already carry customers up
to date with the inventory and `versions.yml`: the node-side roles of a node
build (base system, node_exporter, tailscale, Ubuntu Pro, docker's daemon
configuration and TLS listener, kernel settings, haproxy's host preparation,
cadvisor and fluentd, the firewall, and cs-agent — upgraded to its pin), the
pending OS updates, the docker packages and a pending reboot. One node at a
time, each inside a cs-agent maintenance hold that the run places and
releases itself.

It is the way to change a live node. `site.yml` and `add-region.yml` stay the
way to build one: on a live node they deliberately skip docker's packages
and the reboot, and they restart dockerd when a docker certificate changes.

## Customer impact

**Every node the run touches goes into maintenance for its turn**, whether or
not anything else happens to it. While a node is held the controller does not
place new containers on it, does not health-check or recover its containers,
and refuses customer power actions on them; the agent runs no backups. The
hold waits out the backups and tasks already in flight (`--wait`, up to two
hours), so a node's turn starts when it is quiet.

**Tenant containers keep running** through the converge. They are stopped —
the running ones recorded first, and exactly those started again before the
hold is released — only when one of these is true for the node:

| Reason | Why it needs the containers stopped |
| --- | --- |
| docker drifts from its pins in `versions.yml` | the package install restarts dockerd |
| the converge changed what dockerd reads only at start: its TLS certificate (renewed within 30 days of expiry, or new SANs) or its ExecStart drop-in | dockerd must restart to load it |
| `/var/run/reboot-required` exists (often: a kernel, installed by unattended-upgrades or by this run's OS updates) | the node reboots |
| `-e maintain_force_restart=true` | asked for |

Tenant containers have no docker restart policy, so a dockerd restart or a
reboot would leave them down; that is why the run stops and starts them
itself, inside the hold.

**Pending OS updates are applied live, inside the hold, with the containers
running** — after the converge and the agent, before the run decides whether
to stop. docker's packages are held for it (so are cs-agent, node_exporter
and haproxy), and needrestart is told not to restart services (now or at
the next apt run), but a
package's own upgrade can still restart its service (sshd, for one, which
keeps open sessions). OS updates never stop containers on their own; when
one of them asks for a reboot (a kernel, often libc), the reboot does — in
the same run. A run interrupted part-way through them re-holds docker and
runs apt again; if dpkg itself was left half-way, the run stops with the node
held and says to run `dpkg --configure -a` first.

The cs-agent upgrade happens inside the hold with the containers running: the
agent restarts, keeps its hold, and the run waits until it is back.

A run resumed part-way through its converge restarts cs-agent, cadvisor,
fluentd and sshd, re-applies the firewall, sysctl and kernel modules, reloads
docker's configuration (a SIGHUP, which stops nothing) and networkd, all
inside the hold: whatever the interrupted run changed is then live before the
node is released.

## Running it

Preview first. `--check --diff` takes no hold and changes nothing: it runs
every preflight, shows the converge's diffs, and prints per node

    <node>: would stop containers: no -- docker matches its pins, ... .
    Tenant containers running now: 412. Pending OS updates: 23 (may ask for a reboot:
    libc6, linux-image-...) -- would be applied live inside the hold, with
    the containers running, without a stop. BUT some of these updates
    usually ask for a reboot: the run would then STOP the containers and
    reboot the node.

The preview cannot know for certain whether the updates will ask for a
reboot; it flags the packages that usually do (kernel, firmware,
microcode, libc, dbus, PAM, systemd), and the run itself decides from
`/var/run/reboot-required`. A real run
reports the updates it applied live and, after them, its decision.

```sh
make maintain-nodes ENV=prod ARGS='--check --diff'
make maintain-nodes ENV=prod
```

**`LIMIT`.** The converge reads the controller (each node's agent token hash,
and its vault when a docker certificate is due) and may add the node's borg
key on its site's backup host, so those hosts must be in the run. A `LIMIT`
that leaves them out is refused before anything happens, and the refusal
names them:

```sh
make maintain-nodes ENV=prod LIMIT='node1001:ctl1:backup1'
```

That is also how a single node is resumed after a failure.

After each node the run validates it (`roles/validate`) and pauses 60s so the
controller's health check has seen it before the next node is held.

## Before the first hold

The run refuses — before it holds any node, and again just before each
node's hold — when:

* a node's cs-agent is older than 3.4.0, or `cs_agent_apt_version` is below
  the version installed (the agent is never downgraded);
* a node's docker and containerd state is not on its data disk;
* a docker pin is missing from the mirror or below what is installed;
* the agent.yml the converge would write changes `store.data_dir`,
  `metadata.admin_token_hash`, `metadata.listen_addr`, `backups.key` or where
  the borg repositories live. Values are never printed. A per-node
  `backups_key` must be byte for byte the node's passphrase, without a
  trailing newline. Encrypt it without echoing it or leaving it in shell
  history — paste the passphrase at the silent prompt:

  ```sh
  IFS= read -rs K && printf %s "$K" | ansible-vault encrypt_string --stdin-name backups_key; unset K
  ```

* a tailnet member (`tailscale_enabled: true`) is not logged in to the
  tailnet. Maintenance never joins a node, so a maintain run needs no
  `tailscale_authkey` at all; join the node outside a maintenance run (by
  hand, or at attach with a short-lived key for that run), or set
  `tailscale_enabled: false` if it is not meant to be on the tailnet;
* a tailnet member's tailscale settings differ from the inventory's flags —
  typically a node joined by hand with `--ssh` or tags the inventory does not
  carry. The message names each setting and the `tailscale set …` that makes
  the node match; or describe the node in `tailscale_extra_args` /
  `tailscale_tags` (`tailscale debug prefs` on the node shows its settings).
  The run never re-runs `tailscale up` on a logged-in node. Such nodes are
  expected to be `tailscale_access_only: true` (docs/contracts.md §Tailscale
  access-only mode);
* the controller already holds the node, or someone else holds it locally;
* the node has an unfinished `upgrade-docker` run (finish that first).

The controller must be >= 9.8.0 (it confirms agent holds).

## When it fails

The first failure stops the whole run; the message says whether the node is
held, whether its containers are running, and what to do. The state of a
node part-way through is in `/var/lib/computestacks/upgrade/state.json`,
and re-running the same command resumes it — inside the same hold, from the
last completed step, never stopping a container twice and never re-deciding
whether to stop.

* Failed before the hold (preflight, the download): nothing is held and
  nothing changed. Fix the cause and re-run.
* Failed while placing the hold (`cs-agent maintenance on --wait` timed out
  waiting for in-flight work, the controller never confirmed it, it was
  interrupted, or the state file could not be written): no state file exists,
  so the hold is NOT released automatically, and a re-run refuses because
  the node already has a local hold. The message says whether the node is
  held. To recover, on the node run `cs-agent maintenance status` (a local
  hold with reason `maintain-nodes` is this run's) and `docker ps` (the
  tenants are still running); then `cs-agent maintenance off` is safe —
  nothing was stopped or changed — and re-run. (`on` failing only because
  the controller has not polled the agent with maintenance support in the
  last 120s places no hold; the run retries that up to 3 times, 30s apart,
  before it gives up.)
* Failed after the hold was recorded and before the converge started: the
  hold is released automatically and the state file set aside; nothing
  changed.
* Failed during the converge or before anything was stopped: the node stays
  held with its containers running. Re-run to resume, or, to abandon, run
  `cs-agent maintenance off` on the node (safe: nothing was stopped) and move
  the state file aside.
* Failed after containers were stopped: re-run. Do NOT clear the hold by
  hand — the controller would start recovering the node mid-window.

`roles/node_upgrade/README.md` ("maintain-nodes") has the step-by-step
account, the state file and the complete list of what the run touches off
the node.

## maintain-nodes and upgrade-docker

`upgrade-docker.yml` still exists and still only moves docker (and the OS
updates and reboot riding with it), always with the containers stopped.
`maintain-nodes.yml` does that too when docker drifts, plus the converge and
the live OS updates. They share the state file and refuse to
resume each other's unfinished run.
