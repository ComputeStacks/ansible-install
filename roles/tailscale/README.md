# tailscale

Joins a host to the tailnet and records its tailnet address (`tailscale_ip`)
for the roles that derive control-plane addresses pairwise. A thin wrapper
around the `machine` role of the pinned community collection
`artis3n.tailscale` (`requirements.yml`, `1.2.1` — the latest release,
verified on 2026-10-14 against galaxy.ansible.com and GitHub
`artis3n/ansible-collection-tailscale`).

| Runs in | Play | Hosts |
| --- | --- | --- |
| `site.yml` | "Tailscale mesh and Ubuntu Pro" | `all` |
| `add-region.yml` | "New node base system" | `nodes` (the new node) |
| `upgrade-docker.yml`, `unseal.yml` | not run | — |

Both plays gate it on `tailscale_authkey` being set in the vaulted secrets
file and on `tailscale_enabled` — a per-host/group opt-out for same-L2
regions, defaulting to `true` in `site.yml` and to `false` in
`add-region.yml`. A joined host with `tailscale_access_only: true` is on the
tailnet for operator access only (see [Access-only mode](#access-only-mode)).

**Attach mode:** new node only, and only with an explicit
`tailscale_enabled: true` (see [Gotchas](#gotchas)). **Requires:** —.

## What it does

1. Asserts an auth key is present (the playbooks' gate, restated so a direct
   role invocation fails clearly).
2. Removes the apt source the standalone `artis3n.tailscale` role wrote
   (`/etc/apt/sources.list.d/tailscale.list` and its keyring), which would
   otherwise conflict with the collection's deb822 `tailscale.sources`.
3. Includes `artis3n.tailscale.machine` with `state: present` and
   `tailscale up --accept-dns=false`.
4. Reads `tailscale ip -4` and **asserts** it returned a `100.x` address — a
   silent non-join would make every consumer fall back to the public path while
   the run stays green.
5. Sets the play fact `tailscale_ip` and merges the same value into
   `/etc/ansible/facts.d/computestacks.fact`, then re-gathers `ansible_local`.

## Decisions

- **`--accept-dns=false`.** MagicDNS rewrites `/etc/resolv.conf` on every host
  it manages. These are servers with their own resolver configuration, and the
  control plane dials addresses rather than names (each az's manifest carries
  an `acme_server` address, not a name), so DNS is left alone. Turning it on would also make the portal domain resolve differently on
  nodes than everywhere else.
- **No exit node, no subnet routes, no tags.** The tailnet carries control-plane
  traffic between ComputeStacks hosts and operator access, nothing else.
  Extra `tailscale up` flags go in `tailscale_extra_args`; tags need the
  collection role's `tailscale_tags` (an OAuth key requires them).
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
((hostvars[h].tailscale_authkey | default('')) | length > 0)
  and (hostvars[h].tailscale_enabled | default(true) | bool)
```

and whether it routes control-plane traffic over it (every pairwise address
derivation asks this one, of both ends):

```jinja
((hostvars[h].tailscale_authkey | default('')) | length > 0)
  and (hostvars[h].tailscale_enabled | default(true) | bool)
  and not (hostvars[h].tailscale_access_only | default(false) | bool)
```

These are the canonical spellings recorded in docs/contracts.md ("Facts
exception", "Tailscale access-only mode"). Membership gates both playbooks,
`preflight` and the firewall's WireGuard and anti-spoof rules; routing gates
`cs_agent`'s `cs_agent_tailnet_routed`, `metrics`' scrape-address
derivation, `controller_seed`'s manifest, the firewall's `tailscale0`
accepts and `validate`. Never `tailscale_authkey is defined` — an
empty-string authkey must read as NOT tailnet everywhere.

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

Consumed, not owned: `tailscale_authkey` (vaulted secret, passed through
under the same name), `tailscale_enabled`, `tailscale_access_only` (read by
the consumers, not by this role).

## Gotchas

- **Opting a joined host out later.** `site.yml` skips this role entirely when
  `tailscale_enabled: false`, so a host that was joined keeps its stale
  `tailscale_ip` fact. Removing a host from the tailnet means `tailscale
  logout` **and** deleting `/etc/ansible/facts.d/computestacks.fact` (or its
  `tailscale_ip` key). The `firewall` role is immune — it derives membership
  from inventory, not from the fact — but a scrape-target template that reads
  the fact would keep pointing at a dead address.
- **Attach mode defaults tailscale OFF** (docs/contracts.md §attach): an
  existing v1 controller and metrics host are not on a tailnet, and joining
  them is an operator action taken before `add-region.yml` runs.
- **Upgrading a checkout from the standalone role.** `make deps` installs the
  collection; the old `galaxy_roles/artis3n.tailscale` directory is no longer
  referenced and can be deleted. The collection's `tailscale up` idempotency
  state is the same file and hash as the standalone role's, so an already
  joined host is not re-authenticated (a spent one-off auth key stays safe).
- **A host joined outside this role.** With no collection state file on the
  host (`artis3n-tailscale/state` under the facts user's `XDG_STATE_HOME`,
  usually `/root/.local/state`), the first converge runs `tailscale up` with this role's flags (`--accept-dns=false`
  plus `tailscale_extra_args`). tailscale refuses an `up` that drops a
  non-default setting the host was joined with ("requires mentioning all
  non-default flags"), and the run fails. Run `tailscale debug prefs` on the
  host first and make `tailscale_extra_args` carry every non-default
  setting it shows (`--ssh`, `--advertise-tags=…`, and so on).
- The collection role installs from `pkgs.tailscale.com` keyed on the Ubuntu
  codename; a brand-new Ubuntu release can briefly have no repo there.
