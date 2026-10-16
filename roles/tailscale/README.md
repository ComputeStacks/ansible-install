# tailscale

Keeps a tailnet member installed and joined — joining it once, with a
short-lived auth key, while it is not logged in — and records its tailnet
address (`tailscale_ip`) for the roles that derive control-plane addresses
pairwise. A thin wrapper
around the `machine` role of the pinned community collection
`artis3n.tailscale` (`requirements.yml`, `1.2.1` — the latest release,
verified on 2026-10-14 against galaxy.ansible.com and GitHub
`artis3n/ansible-collection-tailscale`).

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Tailscale mesh and Ubuntu Pro" | `all` |
| `add-region.yml` | "New node base system" | `nodes` (the new node) |
| `maintain-nodes.yml` (via `node_upgrade` `tasks_from: maintain`, with `tailscale_join_allowed: false`) | "Maintain the nodes, one at a time" | `maintain_node_targets` (live nodes, inside a maintenance hold) |
| `maintain-nodes.yml`, `tasks_from: status` (via `node_upgrade` `tasks_from: maintain_preflight`; read-only) | "Preflight every targeted node" and "Maintain the nodes, one at a time" | the nodes to maintain, before any hold |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

Every play gates it on membership, `tailscale_enabled: true` for the host
(docs/contracts.md §Tailnet membership) — nothing else, and in every
playbook the default is `false`. A member with `tailscale_access_only: true`
is on the tailnet for operator access only (see
[Access-only mode](#access-only-mode)).

**Attach mode:** new node only, and only with `tailscale_enabled: true` on
it; its first join takes a short-lived `tailscale_authkey` for that run
(docs/attach-mode.md). **Requires:** —.

## What it does

1. Asserts the host is a member (the playbooks' gate, restated so a direct
   role invocation fails clearly).
2. Starts an installed `tailscaled`, then reads the login state
   (`tasks/status.yml`, read-only): `tailscale status --json`'s
   `BackendState`, or `NotInstalled` when there is no `tailscale` binary.
   `Running` is **logged in**; `NeedsLogin` and `NotInstalled` are **not
   logged in**; `Starting` and `NoState`, which a just-started daemon reports
   whether or not it is logged in, are waited out (up to a minute); anything
   else (`Stopped`, `NeedsMachineAuth`, an unreadable status) fails with a
   message naming the state.
3. **Logged in:** reads `tailscale debug prefs` (never logged) and compares
   it with the flags below; any difference fails the run before anything
   changes, naming each setting and the `tailscale set …` that makes the
   node match (see [Joining once](#joining-once)).
4. **Not logged in:** refuses unless the run may join
   (`tailscale_join_allowed`, false in maintain-nodes) and carries a
   `tailscale_authkey`.
5. Removes the apt source the standalone `artis3n.tailscale` role wrote
   (`/etc/apt/sources.list.d/tailscale.list` and its keyring), which would
   otherwise conflict with the collection's deb822 `tailscale.sources`.
6. Includes `artis3n.tailscale.machine` with `state: present`. Not logged
   in: with the key and `tailscale up --accept-dns=false …` — the first
   join. Logged in: with `tailscale_up_skip: true` and an empty key, so it
   only installs or keeps the package and the service.
7. Reads `tailscale ip -4` and **asserts** it returned a `100.x` address — a
   silent non-join would make every consumer fall back to the public path while
   the run stays green.
8. Sets the play fact `tailscale_ip` and merges the same value into
   `/etc/ansible/facts.d/computestacks.fact`, then re-gathers `ansible_local`
   — on every run, so the persisted address follows the node.

## Joining once

`tailscale_authkey` is used **only** to join a member that is not logged in
to the tailnet yet — its first join, typically at attach. Issue a
short-lived (or one-off) key for that run, for example in a vaulted
`host_vars/<node>/tailscale.yml`; it expires, and nothing needs it again.

A logged-in member is never joined again: no run needs a key for it, and
no run executes `tailscale up` (with or without `--authkey`) on it. The
collection role decides to run `up` from its own state file and the node's
online status; on a logged-in node this role turns that task off outright
with `tailscale_up_skip`, so neither can trigger it.

What the flags would have changed is checked instead. A node joined by hand
(`--ssh`, tags, a different `--accept-dns`) or changed since its join keeps
its settings; the run fails with the differing prefs and either a
`tailscale set …` for the node or the inventory change
(`tailscale_extra_args`, `tailscale_tags`) that describes it. The comparison
is the `tailscale_prefs_problems` / `tailscale_set_args` filters
(`filter_plugins/tailscale.py`, tested in `tests/tailscale_membership.yml`).
Tags and a non-default login server cannot be changed with `tailscale set`;
the message lists those as manual.

| Run meets a member that is | First join (site.yml, add-region.yml) | maintain-nodes |
| --- | --- | --- |
| not installed, or installed and not logged in | joins with the run's `tailscale_authkey`; fails without one | refused in preflight, before the hold |
| logged in, settings match | keeps the package, refreshes `tailscale_ip` | the same |
| logged in, settings differ | fails before changing anything, with the fix | refused in preflight, before the hold |
| stopped (`tailscale down`), awaiting approval | fails with the state | refused in preflight |

A **non-member** (`tailscale_enabled` false or unset) is never touched, even
with tailscale installed and logged in on it (a host someone joined by
hand): this role does not run, its firewall carries no tailnet rules, and
no pairwise address uses its tailnet address.

## Decisions

- **`--accept-dns=false`.** MagicDNS rewrites `/etc/resolv.conf` on every host
  it manages. These are servers with their own resolver configuration, and the
  control plane dials addresses rather than names (each az's manifest carries
  an `acme_server` address, not a name), so DNS is left alone. Turning it on would also make the portal domain resolve differently on
  nodes than everywhere else.
- **No exit node, no subnet routes, no tags.** The tailnet carries control-plane
  traffic between ComputeStacks hosts and operator access, nothing else.
  Extra `tailscale up` flags go in `tailscale_extra_args`; tags need the
  collection role's `tailscale_tags` (an OAuth key requires them). Both are
  applied at the first join and compared on every later run.
- **`state: present`, not the upstream default `latest`.** `latest` would
  upgrade the tailscale package on every converge, which contradicts
  docs/contracts.md rule 4. The collection role has no version-pin variable, so
  `present` (install once, upgrade deliberately) is the closest honest fit.
  Override with `tailscale_package_state: latest` if you want the upstream
  behavior.
- **Ownership of `computestacks.fact`.** This role owns the `tailscale_ip` key
  and merges rather than overwrites, so a later role can add its own keys to
  the same file without losing this one (`docs/contracts.md` §Ownership).

## Access-only mode

`tailscale_access_only: true` on a host or group keeps that host on the
tailnet (this role still joins it; its firewall still opens UDP 41641 and
drops tailnet sources arriving off `tailscale0`) but routes **no**
control-plane traffic over it: the controller dials its agent on
`primary_ip`, prometheus scrapes it on `primary_ip`, its acme backend address
is the controller's `primary_ip` (or `controller_acme_address`), its agent
binds `primary_ip:8500`, and its firewall carries no `tailscale0` control-plane
accepts. The pairing is two-sided, so an access-only controller or metrics
host moves *every* leg it is an end of onto the private addresses. Default
`false`, which keeps the behavior from before the variable existed.

It is for operators who want Tailscale SSH or ad hoc tailnet access to hosts
whose control plane belongs on the LAN. Tailscale SSH itself still needs
`--ssh` in `tailscale_extra_args` (a tailnet ACL decision this role does not
make for you). Same-region peers do not need it for performance — tailscale
already takes a direct LAN path between peers that share one. What it changes
is which address the control plane is configured to dial, so the control
plane no longer depends on the tailnet (its coordination server, keys and
ACLs) being up.

Turning it on for an already-seeded host changes addresses the controller
already holds (`node.agent_host`, `acme_server`). Those are create-only, so
the run that flips it must also pass `controller_seed_update_addresses: true`
(a full run, no `--limit`), or the controller keeps dialing the tailnet
address that the node's firewall and agent no longer accept.

## Consumers: how to read another host's tailnet address

`tailscale_ip` is the one value in this system that cannot be an inventory var
— it is assigned by the tailnet. docs/contracts.md rule 1 (inventory vars only)
therefore gets exactly one sanctioned exception, **for the address value only**,
and only with a default:

```jinja
{% set peer_ts = hostvars[h].tailscale_ip
                 | default(hostvars[h].ansible_local.computestacks.tailscale_ip
                           | default('')) %}
```

`hostvars[h].tailscale_ip` is set by this role during the current run;
`ansible_local.computestacks.tailscale_ip` is the persisted value a later run
(or a later play, after a fact gather) reads back. Both halves must keep the
`default('')` — under `--limit`, un-targeted hosts have neither.

The derivation is **pairwise**, never per-node (docs/contracts.md §Tailscale
address derivation). Both consumers look like this:

```jinja
{# controller_seed — manifest node.agent_host: tailnet address ONLY if the CONTROLLER
   is itself on the tailnet, else omit the key (controller dials primary_ip). #}
{% if controller_on_tailnet and peer_ts %}agent_host: {{ peer_ts }}{% endif %}

{# metrics — prometheus scrape target: tailnet address ONLY if the METRICS host
   is itself on the tailnet, else primary_ip. #}
{{ peer_ts if (metrics_host_on_tailnet and peer_ts) else hostvars[h].primary_ip }}
```

For the *membership* half of those conditions, use the pure-inventory
predicates — they need no facts and stay correct under `--limit`. Whether a
host is on the tailnet at all:

```jinja
(hostvars[h].tailscale_enabled | default(false) | bool)
```

and whether it routes control-plane traffic over it (every pairwise address
derivation asks this one, of both ends):

```jinja
(hostvars[h].tailscale_enabled | default(false) | bool)
  and not (hostvars[h].tailscale_access_only | default(false) | bool)
```

These are the canonical spellings recorded in docs/contracts.md ("Tailnet
membership", "Tailscale access-only mode"). Membership gates every playbook
that runs this role, `preflight`, maintain-nodes' tailscale checks and the
firewall's WireGuard and anti-spoof rules; routing gates `cs_agent`'s
`cs_agent_tailnet_routed`, `metrics`' scrape-address derivation,
`controller_seed`'s manifest, the firewall's `tailscale0` accepts and
`validate`. Never the auth key: it says only that a run can join a host,
not that the host is a member.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `tailscale_package_state` | `present` | Passed as the collection role's `state`. |
| `tailscale_accept_dns` | `false` | `--accept-dns` value. |
| `tailscale_extra_args` | `""` | Extra `tailscale up` flags. |
| `tailscale_up_args` | `--accept-dns=… <extra>` | Composed argument string. |
| `tailscale_up_timeout` | `"120"` | Collection role's `tailscale up` timeout, seconds (same name upstream). |
| `tailscale_legacy_apt_files` | `tailscale.list` + its keyring | Apt files of the standalone role, removed before the collection writes its own. |
| `tailscale_fact_file` | `/etc/ansible/facts.d/computestacks.fact` | Persisted local fact. |
| `tailscale_join_allowed` | `true` | `false` makes a member that is not logged in fail instead of joining (maintain-nodes sets it). |
| `tailscale_binary` | `/usr/bin/tailscale` | Its absence reads as "not installed". |

Consumed, not owned: `tailscale_enabled` (membership; the playbooks gate
this role on it), `tailscale_authkey` (vaulted, short-lived; read only to
join a member that is not logged in, and passed through under the same
name), `tailscale_tags` (the collection's; applied at the join, compared
afterwards), `tailscale_access_only` (read by the consumers, not by this
role).

## Gotchas

- **Opting a joined host out later.** Every play skips this role entirely when
  `tailscale_enabled` is not true, so a host that was joined stays logged in
  and keeps its stale `tailscale_ip` fact; the firewall drops its tailnet
  rules on the next converge. Removing a host from the tailnet means `tailscale
  logout` **and** deleting `/etc/ansible/facts.d/computestacks.fact` (or its
  `tailscale_ip` key). The `firewall` role is immune — it derives membership
  from inventory, not from the fact — but a scrape-target template that reads
  the fact would keep pointing at a dead address.
- **Attach mode defaults tailscale OFF** (docs/contracts.md §attach): an
  existing v1 controller and metrics host are not on a tailnet, and joining
  them is an operator action taken before `add-region.yml` runs.
- **Upgrading a checkout from the standalone role.** `make deps` installs the
  collection; the old `galaxy_roles/artis3n.tailscale` directory is no longer
  referenced and can be deleted. Its state file no longer matters: a
  logged-in host is never re-authenticated, whatever the file says.
- **A host joined outside this role.** It is logged in, so it is never
  joined again; its settings are compared with this role's flags
  (`--accept-dns=false` plus `tailscale_extra_args`, and `tailscale_tags`).
  A host joined with `--ssh`, tags or MagicDNS fails the run until the
  inventory describes it (`tailscale debug prefs` on the host shows its
  settings) or `tailscale set` changes it — the message gives both.
- **Changing a member's flags later.** Editing `tailscale_extra_args` does
  not re-run `tailscale up`; the next run fails and prints the `tailscale
  set …` to apply on the node (tags need a re-join by hand).
- The collection role installs from `pkgs.tailscale.com` keyed on the Ubuntu
  codename; a brand-new Ubuntu release can briefly have no repo there.
