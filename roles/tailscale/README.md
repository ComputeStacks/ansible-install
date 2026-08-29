# tailscale

**Owner:** Wave 2F (with `firewall` and `ubuntu_pro`)

Thin wrapper around the pinned community role `artis3n.tailscale`
(`requirements.yml`, `v5.0.1` — verified to resolve on 2026-08-28 with
`ansible-galaxy role install artis3n.tailscale,v5.0.1`). Runs on `hosts: all`,
gated in `site.yml` on `tailscale_authkey` being set in the vaulted secrets
file, with a per-host/group `tailscale_enabled: false` opt-out for same-L2
regions.

## What it does

1. Asserts an auth key is present (site.yml's gate, restated so a direct role
   invocation fails clearly).
2. Includes `artis3n.tailscale` with `state: present` and
   `tailscale up --accept-dns=false`.
3. Reads `tailscale ip -4` and **asserts** it returned a `100.x` address — a
   silent non-join would make every consumer fall back to the public path while
   the run stays green.
4. Sets the play fact `tailscale_ip` and merges the same value into
   `/etc/ansible/facts.d/computestacks.fact`, then re-gathers `ansible_local`.

## Decisions

- **`--accept-dns=false`.** MagicDNS rewrites `/etc/resolv.conf` on every host
  it manages. These are servers with their own resolver configuration, and the
  control plane dials addresses rather than names (the plan explicitly replaced
  v1's `local_dns` role with per-AZ `acme_server` addresses), so DNS is left
  alone. Turning it on would also make the portal domain resolve differently on
  nodes than everywhere else.
- **No exit node, no subnet routes, no tags.** The tailnet carries control-plane
  traffic between ComputeStacks hosts and nothing else. Extra `tailscale up`
  flags go in `tailscale_extra_args`; tags need the upstream role's
  `tailscale_tags` (an OAuth key requires them).
- **`state: present`, not the upstream default `latest`.** `latest` would
  upgrade the tailscale package on every converge, which contradicts
  docs/contracts.md rule 4. The upstream role has no version-pin variable, so
  `present` (install once, upgrade deliberately) is the closest honest fit.
  Override with `tailscale_package_state: latest` if you want the old
  behaviour.
- **Ownership of `computestacks.fact`.** This role owns the `tailscale_ip` key
  and merges rather than overwrites, so a later role can add its own keys to
  the same file without losing this one. Not currently in the contracts
  ownership map — flagged for the manager.

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
{# Wave 4I — manifest node.agent_host: tailnet address ONLY if the CONTROLLER
   is itself on the tailnet, else omit the key (controller dials primary_ip). #}
{% if controller_on_tailnet and peer_ts %}agent_host: {{ peer_ts }}{% endif %}

{# Wave 2E — prometheus scrape target: tailnet address ONLY if the METRICS host
   is itself on the tailnet, else primary_ip. #}
{{ peer_ts if (metrics_host_on_tailnet and peer_ts) else hostvars[h].primary_ip }}
```

For the *membership* half of those conditions, prefer the pure-inventory
predicate — it needs no facts and stays correct under `--limit`:

```jinja
hostvars[h].tailscale_authkey | default('') | length > 0
  and hostvars[h].tailscale_enabled | default(true) | bool
```

That is exactly what the `firewall` role uses (it never needs an address at
all) and what `cs_agent` uses for `cs_agent_tailnet_joined`.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `tailscale_package_state` | `present` | Passed as the upstream role's `state`. |
| `tailscale_accept_dns` | `false` | `--accept-dns` value. |
| `tailscale_extra_args` | `""` | Extra `tailscale up` flags. |
| `tailscale_up_args` | `--accept-dns=… <extra>` | Composed argument string. |
| `tailscale_up_timeout` | `"120"` | Upstream `tailscale up` timeout, seconds. |
| `tailscale_fact_file` | `/etc/ansible/facts.d/computestacks.fact` | Persisted local fact. |

Consumed, not owned: `tailscale_authkey` (vaulted secret), `tailscale_enabled`.

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
- The upstream role has migrated to a collection
  (`artis3n.tailscale` → `artis3n.tailscale` collection) and prints a
  deprecation warning on every run. Migrating is a separate, deliberate bump.
- The upstream role installs from `pkgs.tailscale.com` keyed on the Ubuntu
  codename; a brand-new Ubuntu release can briefly have no repo there.
