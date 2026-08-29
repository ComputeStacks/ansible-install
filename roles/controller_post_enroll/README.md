# controller_post_enroll

**Owner: Wave 4I.** `hosts: controller`.

## Purpose

The controller-side rake tasks that can only run **after** the nodes have
enrolled. Second controller play in `playbooks/site.yml`:

```
controller_seed (creates Node rows, mints tokens)
  -> cs_agent on the nodes (installs, ENROLS, restarts)
    -> controller_post_enroll        <- this role
      -> validate
```

That order is a frozen contract (docs/contracts.md): the agent coordination
tables start empty, and `agent:datachannel_backfill` calls every node's agent,
which 401s until the node holds its admin token. Running it before enrolment
always fails.

## What it runs

| Task | Why |
| --- | --- |
| `agent:datachannel_backfill` | Pushes each node's full desired state (firewall rules + every volume) to its cs-agent and latches `nodes.datachannel_backfilled_at`. **Mandatory on a first boot**: until a node latches, `Agent::Client#create_task` refuses it and backup, restore, export and delete dispatch stay blocked. |
| `metadata:agent_backfill` | Provisions each project's tenant on the node agent and pushes its managed blobs. Mints any missing per-node `agent_token` first. |
| `test_connection:all` | Controller → node SSH, docker mTLS and cs-agent probes, plus the DNS provider check. |

Both backfills are idempotent and resumable, and since Wave 1C they **exit
non-zero on any failure** — which is what makes them usable from a play. They
are included one at a time (`tasks/backfill.yml`) rather than looped inside a
single task, so a failure stops the sequence instead of running the next
backfill against a fleet that just failed and burying the real error.

`test_connection:all` prints `[FAILED] …` lines for a leg it could not dial but
**exits 0**, so it cannot fail this play. Read its output; the `validate` role
(Wave 4J) owns the hard assertions.

## How the tasks are invoked

`cstacks` has no general rake passthrough — its `seed` and `test` subcommands
each wrap one specific task — so this role runs them the same way the CLI does:

```
docker exec [--env NODE=<hostname>] portal bundle exec rake <task>
```

If a `cstacks rake <task>` subcommand is ever added, this role is its only
caller.

## Greenfield vs attach mode

* **Greenfield** (`existing_env` unset): one unscoped, fleet-wide pass per
  backfill.
* **Attach** (`existing_env: true`): each backfill runs **once per new node**
  with `NODE=<hostname>` (`NODE_ID=<id>` is the other accepted form). In an
  attach inventory `groups['nodes']` holds only the new nodes. A fleet-wide
  pass against a live controller would re-PUT desired state to every existing
  node for no reason, and one unrelated failure would mask the result for the
  node being attached; the scoped form additionally fails when the node it was
  asked about does not latch.

Ordering is task-major: `agent:datachannel_backfill` completes for every new
node before `metadata:agent_backfill` starts.

## Output and failure

Every invocation is registered and printed — in an `always`, so the output
appears whether the pass succeeded or failed. It is operational and carries no
credentials: `[ok]`/`[fail]`/`[skip]`/`[latched]` lines and the "NOT
backfilled" pending list. A non-zero exit fails the play.

The role first checks that the `portal` container is running and fails with
instructions if it is not.

## Variables

| Var | Default | Notes |
| --- | --- | --- |
| `controller_post_enroll_container` | `portal` | Must match `CS_CONTAINER_NAME` in `/etc/default/computestacks`. |
| `controller_post_enroll_backfills` | `[agent:datachannel_backfill, metadata:agent_backfill]` | In order. |
| `controller_post_enroll_test_connection` | `true` | Set false to skip the connectivity report. |
| `controller_post_enroll_test_connection_task` | `test_connection:all` | |
| `controller_post_enroll_scope_env` | `NODE` | The env var the scoped form sets (`NODE_ID` also works). |
| `controller_post_enroll_scoped_hosts` | `groups['nodes']` | The hosts an attach run scopes to. |

Consumed, not owned: `existing_env`, `groups['nodes']`, each node's `hostname`
host var (inventory, never `ansible_hostname`).

## Requirements

The portal container running (roles/controller) and every node enrolled
(roles/cs_agent). No collections beyond ansible-core.
