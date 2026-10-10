# Contributing

Changes are welcome. Before you start, read
[`docs/contracts.md`](docs/contracts.md): the rules every role follows and
the interfaces between roles. Most of them exist because breaking them fails
silently.

## Setup

```bash
make deps     # galaxy roles and collections into galaxy_roles/ and collections/
make check    # syntax checks, render tests and the pure filter tests
make lint     # ansible-lint, production profile
```

Both `make check` and `make lint` must pass. Neither touches a real host.

## Testing against hosts

Anything only a real host can show — disks, reboots, firewalls, attach mode —
should be run end to end on throwaway Ubuntu 26.04 VMs before it merges.
Every directory under `inventories/` except `example` is gitignored, so a
test inventory with real addresses can live there safely.

## What a change carries

* The role's `README.md` updated: what it owns, its variables, why.
* A dated entry in `CHANGELOG.md` for anything an operator would notice.
* No real hostnames, addresses or credentials anywhere in the diff
  (`docs/contracts.md` rule 6).
* Version bumps in `playbooks/group_vars/all/versions.yml`, with the date and
  source you verified them against.
* Version bumps pass `make check-pins` (network; confirms every pin exists
  upstream).
